"""Workspace hygiene classes that can close their own findings.

    drift-debris   leftover copies (*.bak, *.old, *.orig, *.tmp, *~) and dangling
                   symlinks: diff against the live file, ledger the diff, then
                   really delete
    stray-root     a loose file at the governing agent's root is filed into the
                   folder its OWN frontmatter names (domain: + type:)
    alarm-dedup    a repeated alarm in the drift backlog or the governing agent's
                   inbox collapses into ONE line that ages, instead of one line
                   per day

Every class follows the same contract:

  * it only CLOSES what the governing agent owns; a finding in another agent's
    area is delegated to that agent, never fixed on their behalf;
  * an ambiguous destination goes to a human, never a guess;
  * every action goes through the ledger, so it is reversible and its restore
    path is named;
  * ``never_auto`` is checked before reversibility, and age never promotes
    anything past it.

The classes DETECT; ``sla.py`` decides the route. Keeping those apart is why
an old finding can never talk its way into an auto-close.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

from .. import config as config_mod
from . import sla
from .ledger import Ledger, is_text, sha256

DEBRIS_SUFFIXES = (".bak", ".old", ".orig", ".tmp", "~")
DEBRIS_SUBSTRINGS = (".bak-", ".bak.", "_old", ".stale_root")

# Never walked, never touched: tooling state, frozen archives, dependencies.
SKIP_TOKENS = ("/.git/", "/__pycache__/", "/node_modules/", "/.venv/", "/venv/",
               "/.obsidian/", "/Archive/")

DEFAULT_ROOT_FILES = {
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", "README.md", "approvals.json",
    "inbox.md", "MEMORY.md", ".gitignore",
}
ROOT_IGNORE = {".obsidian", ".git", ".gitignore", ".DS_Store", "__pycache__"}

# A stray root file is filed by what it DECLARES, never by what its name suggests.
DEFAULT_TYPE_SURFACE = {
    "ledger-entry": "Ledger", "ledger": "Ledger",
    "policy": "Policies", "scorecard": "Checks", "check": "Checks",
    "runbook": "Runbooks", "reference": "Reference", "queue": "Queue",
}

_FM = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_BAK = re.compile(r"^(?P<live>.+?)\.bak(?:[-.].*)?$")


def _today() -> str:
    return datetime.date.today().isoformat()


def _mtime_day(p: Path) -> str:
    try:
        return datetime.date.fromtimestamp(p.lstat().st_mtime).isoformat()
    except OSError:
        return _today()


def frontmatter(p: Path) -> dict:
    try:
        m = _FM.match(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-", "#")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def _skipped(p: Path) -> bool:
    s = str(p) + ("/" if p.is_dir() else "")
    return any(tok in s for tok in SKIP_TOKENS)


def is_debris(p: Path) -> bool:
    n = p.name
    return n.endswith(DEBRIS_SUFFIXES) or any(sub in n for sub in DEBRIS_SUBSTRINGS)


def live_counterpart(p: Path) -> Path | None:
    """The live file a leftover was a copy of (whether or not it still exists)."""
    n = p.name
    m = _BAK.match(n)
    if m:
        cand = m.group("live")
    elif n.endswith((".old", ".orig", ".tmp")):
        cand = n.rsplit(".", 1)[0]
    elif n.endswith("~"):
        cand = n[:-1]
    elif "_old" in n:
        cand = n.replace("_old", "", 1)
    elif ".stale_root" in n:
        cand = n.split(".stale_root", 1)[0]
    else:
        return None
    q = p.with_name(cand)
    return None if q == p or not cand else q


def walk_debris(root: Path, cap: int = 1000):
    if not root.exists():
        return
    hits = 0
    for p in sorted(root.rglob("*")):
        if hits >= cap:
            return
        if _skipped(p):
            continue
        if p.is_symlink() and not p.exists():
            yield p
            hits += 1
            continue
        if is_debris(p):
            yield p
            hits += 1


def _result(klass: str) -> dict:
    return {"class": klass, "actions": [], "closed": 0, "human": [],
            "delegated": [], "deferred": [], "open": 0, "errors": []}


class Ctx:
    """Run context: config, policy, aging state, ledger and the action budget."""

    def __init__(self, cfg: config_mod.Config, policy: dict, state: dict, *, dry: bool,
                 ledger: Ledger | None = None, today: str | None = None):
        self.cfg = cfg
        self.policy = policy
        self.state = state
        self.dry = dry
        self.today = today or _today()
        self.ledger = ledger or Ledger(cfg.ledger_dir)
        self.budget = int(policy.get("max_actions_per_run", 500))
        self._used: dict[str, int] = {}

    @property
    def me(self) -> str:
        return self.cfg.self_name

    @property
    def home(self) -> Path:
        agent = self.cfg.agent(self.me)
        return agent.home if agent else config_mod.arc_root()

    def klass(self, name: str) -> dict:
        return self.policy.get("classes", {}).get(name, {})

    def take(self, klass: str) -> bool:
        """Consume one unit of the global cap and the per-class limit."""
        limit = self.klass(klass).get("limit")
        used = self._used.get(klass, 0)
        if limit is not None and used >= int(limit):
            return False
        if self.budget <= 0:
            return False
        self.budget -= 1
        self._used[klass] = used + 1
        return True

    def never_auto(self, p: Path) -> str | None:
        """Paths automation may never act on. Checked BEFORE reversibility."""
        s = str(p)
        for prefix in self.policy.get("read_only_prefixes", []):
            pre = config_mod.expand(prefix, self.cfg.base)
            if pre is not None and (s == str(pre) or s.startswith(str(pre).rstrip("/") + "/")):
                return f"read-only prefix ({prefix})"
        for part in self.policy.get("protected_names", []):
            if part and part in s:
                return f"protected surface ({part})"
        return None

    def owned_roots(self) -> list[Path]:
        roots = [config_mod.expand(r, self.cfg.base) for r in self.policy.get("owned_roots", [])]
        roots = [r for r in roots if r is not None]
        return roots or [self.home]

    def route(self, klass: str, rec: dict, *, auto_ok: bool, owner: str) -> str:
        return sla.route(klass, rec, self.policy, auto_ok=auto_ok, owner=owner,
                         self_name=self.me, human_name=self.cfg.human_name, today=self.today)


def dispatch(ctx: Ctx, res: dict, klass: str, rec: dict, decision: str, *,
             owner: str, reason: str = "", do_auto=None) -> None:
    """Apply one routing decision. Exactly one route fires; OPEN only inside SLA."""
    if decision == sla.OPEN:
        res["open"] += 1
        return
    if decision == sla.AUTO:
        if not ctx.take(klass):
            res["open"] += 1
            res["errors"].append(f"cap reached before {rec['key']}")
            return
        if ctx.dry:
            res["actions"].append({"op": "would-close", "target": rec["key"],
                                   "detail": rec.get("detail", "")})
            return
        try:
            action = do_auto()
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"{rec['key']}: {type(e).__name__}: {e}")
            return
        res["actions"].append(action)
        sla.close(ctx.state, rec["fp"], sla.AUTO, note=str(action.get("op")), today=ctx.today)
        res["closed"] += 1
        return
    if decision == sla.DELEGATE:
        # Keep an existing due date: a deadline that renews itself daily is not one.
        due = rec.get("due") or sla.due_date(klass, ctx.policy, ctx.today)
        agent = ctx.cfg.agent(owner)
        ok, msg = sla.write_delegation(agent.inbox if agent else None, rec, due=due,
                                       sender=ctx.me, today=ctx.today, dry=ctx.dry)
        if ok:
            res["delegated"].append({"agent": owner, "target": rec["key"], "due": due,
                                     "msg": msg, "overdue": due < ctx.today})
            if not ctx.dry:
                sla.mark_delegated(rec, owner, due, ctx.today)
        else:
            res["errors"].append(f"delegate {owner}: {msg}")
        return
    # Human route: surfaced as one decision. It STAYS open in state on purpose;
    # a human decision is not closed until a human makes it.
    res["human"].append({"target": rec["key"], "detail": rec.get("detail", ""),
                         "age_days": sla.age_days(rec, ctx.today), "reason": reason,
                         "class": klass, "fp": rec["fp"]})


def _fleet_summary(ctx: Ctx, res: dict, klass: str, find, describe) -> None:
    """One AGED finding per agent per class, never one per file."""
    for name, agent in sorted(ctx.cfg.agents.items()):
        if name == ctx.me or not agent.home.exists():
            continue
        try:
            hits = find(agent.home)
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"{name}: {type(e).__name__}: {e}")
            continue
        if not hits:
            continue
        oldest = min((_mtime_day(h) for h in hits), default=ctx.today)
        rec = sla.observe(ctx.state, klass, f"{name}::{klass}", owner=name,
                          detail=describe(hits), first_seen=oldest, today=ctx.today)
        decision = ctx.route(klass, rec, auto_ok=False, owner=name)
        dispatch(ctx, res, klass, rec, decision, owner=name,
                 reason="another agent's area; theirs to close")


# ── drift-debris ─────────────────────────────────────────────────────────────
def _debris_auto_ok(ctx: Ctx, p: Path, live: Path | None, cfg: dict) -> tuple[bool, str]:
    blocked = ctx.never_auto(p)
    if blocked:
        return False, blocked
    has_live = live is not None and live.exists()
    if p.is_dir():
        if not (live is not None and live.is_dir()):
            return False, "leftover directory with no live counterpart directory: judgment call"
        return True, "directory diffed against its live counterpart, then removed"
    if not is_text(p):
        if has_live and sha256(p) == sha256(live):
            return True, "binary but byte-identical to live: restorable by copy"
        return False, "binary leftover that differs (or has no counterpart): cannot be diffed reversibly"
    if not has_live:
        if str(cfg.get("no_counterpart_action", "delete")).lower() == "delete":
            return True, "no live counterpart: the recorded diff is the full pre-image"
        return False, "no live counterpart: orphan leftover held for a human"
    return True, "diffed against live counterpart, then removed"


def c_drift_debris(ctx: Ctx) -> dict:
    res = _result("drift-debris")
    cfg = ctx.klass("drift-debris")
    cap = int(cfg.get("scan_cap", 1000))

    for root in ctx.owned_roots():
        for p in walk_debris(root, cap=cap):
            live = live_counterpart(p)
            dangling = p.is_symlink() and not p.exists()
            detail = (f"dangling symlink `{p.name}`" if dangling
                      else f"leftover `{p.name}`"
                           f"{' (live counterpart present)' if live and live.exists() else ' (no live counterpart)'}")
            rec = sla.observe(ctx.state, "drift-debris", str(p), owner=ctx.me,
                              detail=detail, first_seen=_mtime_day(p), today=ctx.today)
            if dangling:
                blocked = ctx.never_auto(p)
                auto_ok, reason = blocked is None, blocked or "dead link: removal is its own repair"
            else:
                auto_ok, reason = _debris_auto_ok(ctx, p, live, cfg)
            decision = ctx.route("drift-debris", rec, auto_ok=auto_ok, owner=ctx.me)

            def _close(p=p, live=live, dangling=dangling, reason=reason):
                if dangling:
                    target = str(p.readlink()) if hasattr(p, "readlink") else "?"
                    p.unlink()
                    return ctx.ledger.record({
                        "class": "drift-debris", "op": "delete", "src": str(p), "live": None,
                        "diff_kind": "symlink", "diff_lines": 0, "diff_path": None, "sha256": "-",
                        "reversible": True, "note": "dangling symlink",
                        "restore": f"ln -s {target} {p}"})
                if p.is_dir():
                    return ctx.ledger.delete_tree(p, live, "drift-debris", note=reason)
                return ctx.ledger.delete_file(p, live if (live and live.exists()) else None,
                                              "drift-debris", note=reason)

            dispatch(ctx, res, "drift-debris", rec, decision, owner=ctx.me,
                     reason=reason, do_auto=_close)

    if cfg.get("fleet_delegate", True):
        _fleet_summary(ctx, res, "drift-debris",
                       lambda home: list(walk_debris(home, cap=15)),
                       lambda hits: f"{len(hits)} leftover file(s) in your area "
                                    f"(e.g. `{hits[0].name}`): diff against live, then delete")
    return res


# ── stray-root ───────────────────────────────────────────────────────────────
def _stray_destination(p: Path, home: Path, surfaces: dict) -> tuple[Path | None, str]:
    fm = frontmatter(p)
    domain = fm.get("domain", "")
    if not domain:
        return None, "no `domain:` frontmatter: the file does not say where it belongs"
    d = home / domain
    if not d.is_dir():
        hits = [x for x in home.iterdir()
                if x.is_dir() and domain.split(" ")[0] == x.name.split(" ")[0]]
        if len(hits) != 1:
            return None, f"`domain: {domain}` does not resolve to one existing folder"
        d = hits[0]
    surface = surfaces.get(str(fm.get("type", "")).lower())
    if surface is None:
        return None, f"`type: {fm.get('type', '-')}` has no known surface in {d.name}"
    dest = d / surface
    if not dest.is_dir():
        return None, f"surface `{d.name}/{surface}` does not exist (folders are never created)"
    if (dest / p.name).exists():
        return None, f"a file named `{p.name}` already sits in {d.name}/{surface}"
    return dest / p.name, f"filed by its own frontmatter into {d.name}/{surface}"


def _root_files(ctx: Ctx) -> set[str]:
    extra = ctx.klass("stray-root").get("root_files")
    return set(extra) if extra else set(DEFAULT_ROOT_FILES)


def c_stray_root(ctx: Ctx) -> dict:
    res = _result("stray-root")
    cfg = ctx.klass("stray-root")
    defer = {k.lower(): v for k, v in (cfg.get("defer") or {}).items()}
    surfaces = {**DEFAULT_TYPE_SURFACE, **(cfg.get("type_surfaces") or {})}
    root_files = _root_files(ctx)
    home = ctx.home
    if not home.is_dir():
        return res

    for p in sorted(home.iterdir()):
        if (p.is_dir() or p.name.startswith(".") or p.name in ROOT_IGNORE or p.name in root_files
                or is_debris(p)):
            continue  # leftovers belong to drift-debris, not stray-root
        rec = sla.observe(ctx.state, "stray-root", str(p), owner=ctx.me,
                          detail=f"stray file at the agent root: `{p.name}`",
                          first_seen=_mtime_day(p), today=ctx.today)
        if p.name.lower() in defer:
            res["deferred"].append({"target": str(p), "reason": defer[p.name.lower()],
                                    "age_days": sla.age_days(rec, ctx.today)})
            continue
        dest, why = _stray_destination(p, home, surfaces)
        blocked = ctx.never_auto(p)
        auto_ok = dest is not None and blocked is None
        decision = ctx.route("stray-root", rec, auto_ok=auto_ok, owner=ctx.me)
        dispatch(ctx, res, "stray-root", rec, decision, owner=ctx.me, reason=blocked or why,
                 do_auto=lambda p=p, dest=dest, why=why: ctx.ledger.move_file(p, dest, "stray-root", note=why))

    if cfg.get("fleet_delegate", True):
        _fleet_summary(ctx, res, "stray-root",
                       lambda h: [x for x in h.iterdir()
                                  if x.is_file() and not x.name.startswith(".") and not is_debris(x)
                                  and x.name not in root_files and x.name not in ROOT_IGNORE],
                       lambda hits: f"{len(hits)} stray file(s) at your root "
                                    f"(e.g. `{hits[0].name}`): file them into a folder")
    return res


# ── alarm-dedup ──────────────────────────────────────────────────────────────
_QUEUE_RAW = re.compile(
    r"^- \[ \] (?P<icon>[^\s]+) (?P<date>\d{4}-\d{2}-\d{2}) `(?P<engine>[^`]+)` — (?P<detail>.*)$")
_QUEUE_AGED = re.compile(r"^- \[ \] .*<!--\s*gov-sla:(?P<fp>[0-9a-f]{12})\s*-->\s*$")
_INBOX_RAW = re.compile(
    r"^- (?P<icon>🔴|🟡|🟠) (?P<date>\d{4}-\d{2}-\d{2}) (?P<title>[^—]+?) — (?P<detail>.*)$")
_INBOX_AGED = re.compile(
    r"^- (?P<icon>\S+) \*\*(?P<title>[^*]+)\*\* — .*?first seen \*\*(?P<first>\d{4}-\d{2}-\d{2})\*\*")
_NUM = re.compile(r"\d+")

# Recognised on re-read and dropped before re-emitting, or every run would append
# another copy: a closer that grows the file it is meant to shrink.
QUEUE_BANNER = [
    "> One line per distinct finding, **aged, never recounted**.",
    "> Sighting counts are evidence of persistence, not extra items.",
]

_WORST = {"🟢": 0, "🟡": 1, "🟠": 1, "🔴": 2}
_ICON_BY_RANK = {0: "🟢", 1: "🟡", 2: "🔴"}


def alarm_shape(detail: str) -> str:
    """The invariant of an alarm: its wording with every number blanked."""
    return _NUM.sub("N", detail).strip()


def _merge(bucket: dict, *, icon: str, date: str, detail: str) -> None:
    bucket["first"] = min(bucket["first"], date)
    if date > bucket["last"]:
        bucket["last"], bucket["detail"] = date, detail
    bucket["n"] += 1
    bucket["rank"] = max(bucket["rank"], _WORST.get(icon, 1))


def collapse_queue(text: str, today: str, owner: str = "STEWARD") -> tuple[str, dict]:
    """Rewrite the drift backlog so each distinct alarm appears ONCE, aging."""
    head, closed, buckets, order = [], [], {}, []
    seen_item = False
    for raw in text.splitlines():
        aged = _QUEUE_AGED.match(raw)
        m = _QUEUE_RAW.match(raw)
        if raw.startswith("- [x]"):
            closed.append(raw)
            seen_item = True
            continue
        if aged or m:
            seen_item = True
            if aged:
                mm = re.search(r"`(?P<engine>[^`]+)` — (?P<detail>.*?) · first seen \*\*(?P<first>\d{4}-\d{2}-\d{2})\*\*"
                               r", last \*\*(?P<last>\d{4}-\d{2}-\d{2})\*\*.*?\((?P<n>\d+) sighting", raw)
                if not mm:
                    head.append(raw)
                    continue
                key = (mm.group("engine"), alarm_shape(mm.group("detail")))
                parts = raw.split(" ")
                icon = parts[3] if len(parts) > 3 else "🟡"
                b = buckets.get(key)
                if b is None:
                    buckets[key] = {"first": mm.group("first"), "last": mm.group("last"),
                                    "detail": mm.group("detail"), "n": int(mm.group("n")),
                                    "rank": _WORST.get(icon, 1), "engine": mm.group("engine")}
                    order.append(key)
                else:
                    b["first"] = min(b["first"], mm.group("first"))
                    if mm.group("last") > b["last"]:
                        b["last"], b["detail"] = mm.group("last"), mm.group("detail")
                    b["n"] += int(mm.group("n"))
                continue
            key = (m.group("engine"), alarm_shape(m.group("detail")))
            if key not in buckets:
                buckets[key] = {"first": m.group("date"), "last": m.group("date"),
                                "detail": m.group("detail"), "n": 0,
                                "rank": _WORST.get(m.group("icon"), 1), "engine": m.group("engine")}
                order.append(key)
            _merge(buckets[key], icon=m.group("icon"), date=m.group("date"), detail=m.group("detail"))
            continue
        if not seen_item and not raw.startswith("## ") and raw.strip() not in QUEUE_BANNER:
            head.append(raw)
    if not head:
        head = ["# Drift backlog", ""]

    out = [ln.rstrip() for ln in head]
    while out and not out[-1]:
        out.pop()
    out += ["", *QUEUE_BANNER, "", f"## 🔔 Open, aged (rebuilt {today})", ""]
    day = datetime.date.fromisoformat(today)
    for key in sorted(order, key=lambda k: (-buckets[k]["rank"], buckets[k]["first"])):
        b = buckets[key]
        age = (day - datetime.date.fromisoformat(b["first"])).days
        fp = sla.fingerprint("drift-backlog", f"{b['engine']}|{key[1]}")
        out.append(
            f"- [ ] {_ICON_BY_RANK[b['rank']]} `{b['engine']}` — {b['detail']} · "
            f"first seen **{b['first']}**, last **{b['last']}**, **age {age} days** "
            f"({b['n']} sighting(s)) · owner: {owner} <!-- gov-sla:{fp} -->")
    if closed:
        out += ["", "## ✅ Closed", ""] + closed
    return "\n".join(out) + "\n", {
        "distinct": len(order), "collapsed_from": sum(b["n"] for b in buckets.values()),
        "earliest": min((b["first"] for b in buckets.values()), default=today),
    }


def collapse_inbox(text: str, today: str, owner: str = "STEWARD") -> tuple[str, dict]:
    """Collapse raw alarm re-logs in an inbox, ABSORBING any aged line already
    present for the same alarm rather than adding a second one."""
    keep, buckets, order = [], {}, []
    for raw in text.splitlines():
        aged = _INBOX_AGED.match(raw)
        if aged:
            title = " ".join(aged.group("title").split())
            b = buckets.get(title)
            if b is None:
                buckets[title] = {"first": aged.group("first"), "last": aged.group("first"),
                                  "detail": raw.split("—", 1)[1].split("·")[0].strip(),
                                  "n": 0, "rank": _WORST.get(aged.group("icon"), 1)}
                order.append(title)
            else:
                b["first"] = min(b["first"], aged.group("first"))
            continue
        m = _INBOX_RAW.match(raw)
        if m:
            title = " ".join(m.group("title").split())
            if title not in buckets:
                buckets[title] = {"first": m.group("date"), "last": m.group("date"),
                                  "detail": m.group("detail"), "n": 0,
                                  "rank": _WORST.get(m.group("icon"), 1)}
                order.append(title)
            _merge(buckets[title], icon=m.group("icon"), date=m.group("date"), detail=m.group("detail"))
            continue
        if raw.strip() == sla.AGED_HEADER or raw.strip().startswith("## 🔔 Aged alarms"):
            continue
        keep.append(raw)
    if not order:
        return text, {"distinct": 0, "collapsed_from": 0, "earliest": today}
    body = "\n".join(keep).rstrip("\n")
    day = datetime.date.fromisoformat(today)
    out = [body, "", sla.AGED_HEADER, ""]
    for title in sorted(order, key=lambda t: (-buckets[t]["rank"], buckets[t]["first"])):
        b = buckets[title]
        age = (day - datetime.date.fromisoformat(b["first"])).days
        fp = sla.fingerprint("inbox-alarm", title)
        out.append(
            f"- {_ICON_BY_RANK[b['rank']]} **{title}** — {b['detail']} · "
            f"first seen **{b['first']}**, last **{max(b['last'], b['first'])}**, "
            f"**age {age} days** ({b['n']} new sighting(s) this cycle) · "
            f"owner: {owner} <!-- gov-sla:{fp} -->")
    return "\n".join(out).lstrip("\n") + "\n", {
        "distinct": len(order), "collapsed_from": sum(b["n"] for b in buckets.values()),
        "earliest": min((b["first"] for b in buckets.values()), default=today),
    }


def drift_queue_path(cfg: config_mod.Config) -> Path:
    return cfg.path("drift_queue", "governance/drift-backlog.md")


def c_alarm_dedup(ctx: Ctx) -> dict:
    res = _result("alarm-dedup")
    me_agent = ctx.cfg.agent(ctx.me)
    surfaces = [(drift_queue_path(ctx.cfg), collapse_queue, "drift backlog")]
    if me_agent is not None:
        surfaces.append((me_agent.inbox, collapse_inbox, "governing inbox"))
    for path, collapse, label in surfaces:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        before_open = len([ln for ln in text.splitlines()
                           if ln.startswith("- [ ]") or _INBOX_RAW.match(ln) or _INBOX_AGED.match(ln)])
        try:
            new_text, stats = collapse(text, ctx.today, ctx.me)
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"{label}: {type(e).__name__}: {e}")
            continue
        if new_text == text or stats["distinct"] == 0:
            res["stable"] = res.get("stable", 0) + stats.get("distinct", 0)
            continue
        # The clock runs from the OLDEST alarm in the file, not its mtime: a file
        # rewritten daily by the re-logger would otherwise never age.
        rec = sla.observe(ctx.state, "alarm-dedup", str(path), owner=ctx.me,
                          detail=f"{label}: {before_open} line(s) -> {stats['distinct']} aged item(s)",
                          first_seen=stats.get("earliest") or _mtime_day(path), today=ctx.today)
        decision = ctx.route("alarm-dedup", rec, auto_ok=ctx.never_auto(path) is None, owner=ctx.me)
        dispatch(ctx, res, "alarm-dedup", rec, decision, owner=ctx.me,
                 reason=f"{label}: aged in place, not recounted",
                 do_auto=lambda path=path, new_text=new_text, stats=stats, label=label, n=before_open:
                     ctx.ledger.rewrite_file(path, new_text, "alarm-dedup",
                                             note=f"{label}: {n} line(s) -> {stats['distinct']} aged item(s)"))
        res.setdefault("stats", {})[label] = {"before_lines": before_open, **stats}
    return res


CLASS_FN = {
    "drift-debris": c_drift_debris,
    "stray-root": c_stray_root,
    "alarm-dedup": c_alarm_dedup,
}
