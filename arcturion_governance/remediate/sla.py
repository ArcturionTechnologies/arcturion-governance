"""Owner + SLA + the three closing routes.

A detector without a closer is a backlog generator. Every finding here carries
an **owner** and an **SLA**, and once the SLA is breached it takes exactly one
of three routes. It never simply stays open.

    auto      reversible + ledgered + restorable, and the governing agent owns it
    delegate  the finding sits in another agent's area -> a dated line in that
              agent's inbox
    human     lossy / canonical / ambiguous / a real judgment call -> surfaced
              to a person as one decision

Every finding gets a stable fingerprint and a state entry holding first_seen /
last_seen / occurrences. Re-observing a finding AGES the existing entry; it
never creates a second one. Delegated inbox lines carry the fingerprint as an
HTML comment, so re-delegation rewrites the same line in place instead of
re-logging the same alarm every day.

``trend()`` reports the first derivative (open count and closure rate), because
a backlog rising at a steady rate proves zero closure whatever its size.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path

AUTO, DELEGATE, HUMAN, OPEN = "auto", "delegate", "human", "open"
ROUTE_ICON = {AUTO: "🤖", DELEGATE: "📬", HUMAN: "🙋", OPEN: "⏳"}

DEFAULT_SLA_DAYS = {
    "drift-debris": 7,
    "stray-root": 7,
    "alarm-dedup": 3,
}

MARKER_PREFIX = "gov-sla"
_MARKER = re.compile(r"<!--\s*gov-sla:([0-9a-f]{12})\s*-->")
AGED_HEADER = "## 🔔 Aged alarms (aged, not recounted)"


def _day(today: str | None) -> str:
    return today or datetime.date.today().isoformat()


# ── fingerprints & state ─────────────────────────────────────────────────────
def fingerprint(klass: str, key: str) -> str:
    """Stable identity of a finding. ``key`` must be the finding's INVARIANT (a
    path, an engine + the shape of a message), never a count or a timestamp."""
    return hashlib.sha1(f"{klass}\x00{key}".encode("utf-8")).hexdigest()[:12]


def empty_state() -> dict:
    return {"findings": {}, "closed": {}, "history": []}


def load_state(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()


def save_state(state: dict, path: Path) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str),
                              encoding="utf-8")
    except OSError:
        pass


def observe(state: dict, klass: str, key: str, *, owner: str, detail: str = "",
            first_seen: str | None = None, today: str | None = None) -> dict:
    """Record one sighting. AGES an existing finding; never duplicates it.

    ``first_seen`` seeds the clock from real evidence (a file's mtime), so a
    leftover that has sat for 33 days is 33 days old on the very first run.
    """
    day = _day(today)
    fp = fingerprint(klass, key)
    rec = state.setdefault("findings", {}).get(fp)
    if rec is None:
        rec = {"fp": fp, "class": klass, "key": key, "owner": owner,
               "first_seen": first_seen or day, "last_seen": day,
               "occurrences": 1, "detail": detail}
        state["findings"][fp] = rec
    else:
        if first_seen and first_seen < rec["first_seen"]:
            rec["first_seen"] = first_seen  # earlier evidence wins
        if rec["last_seen"] != day:
            rec["occurrences"] = rec.get("occurrences", 1) + 1
        rec["last_seen"] = day
        rec["owner"] = owner
        rec["detail"] = detail or rec.get("detail", "")
    return rec


def close(state: dict, fp: str, route: str, note: str = "", today: str | None = None) -> None:
    """Move a finding from ``findings`` to ``closed``.

    Delegation deliberately does NOT call this: an assigned finding is not a
    resolved one, and closing on hand-off would let the same item be "closed"
    again every day.
    """
    rec = state.get("findings", {}).pop(fp, None)
    if rec is None:
        return
    rec.update({"closed_on": _day(today), "closed_route": route, "closed_note": note})
    state.setdefault("closed", {})[fp] = rec


def mark_delegated(rec: dict, agent: str, due: str, today: str | None = None) -> None:
    """Stamp a finding as assigned. It stays OPEN and keeps aging."""
    rec["delegated_to"] = agent
    rec["due"] = due
    rec["delegated_on"] = _day(today)


def reap(state: dict, scanned_classes: set[str], today: str | None = None) -> int:
    """Close every finding of a SCANNED class that was not seen this run.

    The only source of genuine closure for delegated and externally fixed
    items: the finding is gone because the condition is gone. Classes that were
    not scanned are left alone, so an ``--only`` run never mass-closes what it
    did not look at.
    """
    day = _day(today)
    gone = [fp for fp, r in state.get("findings", {}).items()
            if r.get("class") in scanned_classes and r.get("last_seen") != day]
    for fp in gone:
        close(state, fp, "resolved", note="no longer detected", today=day)
    return len(gone)


def age_days(rec: dict, today: str | None = None) -> int:
    day = datetime.date.fromisoformat(_day(today))
    try:
        first = datetime.date.fromisoformat(str(rec.get("first_seen"))[:10])
    except ValueError:
        return 0
    return max(0, (day - first).days)


def sla_for(klass: str, policy: dict) -> int:
    cfg = policy.get("classes", {}).get(klass, {})
    return int(cfg.get("sla_days", DEFAULT_SLA_DAYS.get(klass, 7)))


# ── the three routes ─────────────────────────────────────────────────────────
def route(klass: str, rec: dict, policy: dict, *, auto_ok: bool, owner: str,
          self_name: str, human_name: str = "HUMAN", today: str | None = None) -> str:
    """Exactly one route per finding. Inside the SLA it is legitimately OPEN; at
    breach it must move.

    ``auto_ok`` is the class's own reversibility verdict. Age NEVER promotes a
    finding past that verdict.
    """
    if age_days(rec, today) < sla_for(klass, policy):
        return OPEN
    me = self_name.upper()
    who = (owner or "").upper()
    if auto_ok and who == me:
        return AUTO
    if who and who not in (me, human_name.upper(), "?"):
        return DELEGATE
    return HUMAN


# ── delegate route: idempotent by fingerprint ────────────────────────────────
def delegate_line(rec: dict, *, due: str, sender: str, today: str | None = None) -> str:
    day = _day(today)
    return (f"- 🟡 **{rec['class']}** — {rec.get('detail') or rec['key']} · "
            f"first seen **{rec['first_seen']}**, last **{day}**, "
            f"**age {age_days(rec, day)} days** ({rec.get('occurrences', 1)} sighting(s)) · "
            f"**due {due}** · owner: {rec['owner']} · from {sender} "
            f"<!-- {MARKER_PREFIX}:{rec['fp']} -->")


def write_delegation(inbox: Path | None, rec: dict, *, due: str, sender: str,
                     today: str | None = None, dry: bool = False) -> tuple[bool, str]:
    """Append OR update the aged line for this finding in an agent's inbox.

    Idempotent: an existing line with the same fingerprint is REPLACED with the
    freshly aged one. Re-delegation ages an item; it never recounts it.
    """
    if inbox is None or not inbox.parent.exists():
        return False, "no inbox for that agent"
    line = delegate_line(rec, due=due, sender=sender, today=today)
    text = inbox.read_text(encoding="utf-8") if inbox.exists() else ""
    lines = text.splitlines()
    name = inbox.parent.name + "/" + inbox.name
    for i, existing in enumerate(lines):
        m = _MARKER.search(existing)
        if m and m.group(1) == rec["fp"]:
            if dry:
                return True, f"would age existing line in {name}"
            lines[i] = line
            inbox.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True, f"aged existing line in {name}"
    if dry:
        return True, f"would append aged line to {name}"
    if AGED_HEADER in lines:
        at = lines.index(AGED_HEADER) + 1
        if at < len(lines) and not lines[at].strip():
            at += 1
        lines.insert(at, line)
        body = "\n".join(lines) + "\n"
    elif text.strip():
        body = text.rstrip("\n") + f"\n\n{AGED_HEADER}\n\n{line}\n"
    else:
        body = f"{AGED_HEADER}\n\n{line}\n"
    inbox.write_text(body, encoding="utf-8")
    return True, f"appended aged line to {name}"


def due_date(klass: str, policy: dict, today: str | None = None) -> str:
    day = datetime.date.fromisoformat(_day(today))
    return (day + datetime.timedelta(days=sla_for(klass, policy))).isoformat()


# ── the first derivative ─────────────────────────────────────────────────────
def trend(state: dict, today: str | None = None) -> dict:
    """Open count, closed count, and closure rate, with the day-over-day delta."""
    day = _day(today)
    findings = state.get("findings", {})
    closed = state.get("closed", {})
    closed_today = sum(1 for r in closed.values() if r.get("closed_on") == day)
    hist = state.setdefault("history", [])
    snap = {"date": day, "open": len(findings), "closed_total": len(closed),
            "closed_today": closed_today}
    hist[:] = [h for h in hist if h.get("date") != day][-90:] + [snap]
    prev = hist[-2] if len(hist) > 1 else None
    snap["delta_open"] = (snap["open"] - prev["open"]) if prev else 0
    total_seen = len(findings) + len(closed)
    snap["closure_rate"] = round(len(closed) / total_seen, 3) if total_seen else 0.0
    return snap
