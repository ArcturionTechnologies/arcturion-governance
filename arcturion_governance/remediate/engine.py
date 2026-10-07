"""safe-remediate: bounded, reversible auto-remediation.

Turns detection into action, but ONLY for safe classes: reversible or purely
additive, with a per-class policy gate, caps, a never-auto list, a per-action
ledger and a restore path. Lossy operations (merges, retirements, canon or
identity edits) are never auto-applied; they stay with a human.

Every finding carries an owner and an SLA, and at breach it takes exactly one
of three routes:

    auto-close   reversible + ledgered + restorable, inside the governing agent's area
    delegate     another agent's area -> a dated line in their inbox
    human        lossy / canonical / ambiguous -> surfaced as one decision

Two kinds of class:

  * built-in hygiene classes run in-process (``hygiene.py``): drift-debris,
    stray-root, alarm-dedup;
  * command classes wrap an engine you already trust. The policy gives a dry
    and an apply argv, and ``{limit}`` is substituted from the policy:

        "frontmatter-stamp": {"enabled": true, "type": "command", "limit": 200,
          "dry":   ["python3", "tools/stamp.py", "--json"],
          "apply": ["python3", "tools/stamp.py", "--apply", "--limit", "{limit}"],
          "cwd": "$ARC_ROOT", "timeout": 300}

Gating: a SCOPED opt-in policy file. ``apply`` requires policy.enabled AND the
class enabled. ``plan`` never writes.

Commands:
  plan [--only a,b] [--json]      dry preview (no writes)
  apply [--only a,b] [--json]     execute enabled classes (writes + ledger + notice)
  status [--json]                 policy, enabled classes, never-auto list
  routes [--json]                 open findings, their age, and the route each takes
  undo <action_id>                reverse one ledgered action
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

from .. import config as config_mod
from .. import notify as notify_mod
from . import hygiene, sla
from .ledger import Ledger

BUILTIN = dict(hygiene.CLASS_FN)


# ── policy ───────────────────────────────────────────────────────────────────
def policy_path(cfg: config_mod.Config) -> Path:
    env = os.environ.get("ARC_REMEDIATE_POLICY")
    if env:
        return Path(env).expanduser()
    configured = cfg.section("remediate").get("policy")
    if configured:
        return config_mod.expand(configured, cfg.base)
    return cfg.base / "remediate_policy.json"


def load_policy(path: Path | None) -> dict:
    """Read the policy at call time. A missing or broken policy is DISABLED."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
        return data if isinstance(data, dict) else {"enabled": False, "classes": {}}
    except (OSError, ValueError):
        return {"enabled": False, "classes": {}}


def known_classes(policy: dict) -> list[str]:
    """Built-ins first (fixed order), then command classes the policy defines."""
    cmd = [k for k, v in policy.get("classes", {}).items()
           if isinstance(v, dict) and v.get("type") == "command" and k not in BUILTIN]
    return list(BUILTIN) + cmd


def enabled_classes(policy: dict) -> list[str]:
    if not policy.get("enabled"):
        return []
    cls = policy.get("classes", {})
    return [k for k in known_classes(policy) if cls.get(k, {}).get("enabled")]


def argv_for(name: str, apply: bool, policy: dict) -> list[str]:
    spec = policy.get("classes", {}).get(name, {})
    argv = list(spec.get("apply" if apply else "dry") or [])
    limit = str(spec.get("limit", 0))
    return [str(a).replace("{limit}", limit) for a in argv]


# ── run ──────────────────────────────────────────────────────────────────────
def _hygiene_summary(res: dict) -> str:
    bits = [f"auto={res['closed']}", f"in-sla={res['open']}"]
    if res["delegated"]:
        overdue = sum(1 for d in res["delegated"] if d.get("overdue"))
        bits.append(f"delegated={len(res['delegated'])}" + (f" ({overdue} overdue)" if overdue else ""))
    if res["human"]:
        bits.append(f"human={len(res['human'])}")
    if res.get("stable"):
        bits.append(f"stable={res['stable']}")
    if res["deferred"]:
        bits.append(f"deferred={len(res['deferred'])}")
    if res["errors"]:
        bits.append(f"errors={len(res['errors'])}")
    for label, st in (res.get("stats") or {}).items():
        bits.append(f"{label}: {st['before_lines']}->{st['distinct']}")
    return "; ".join(bits)


def summarize_output(out: str, err: str) -> str:
    out = (out or "").strip()
    if out:
        try:
            obj = json.loads(out)
            if isinstance(obj, dict):
                parts = []
                for k, v in obj.items():
                    if isinstance(v, (int, float, str)) and k not in ("ts", "dry_run"):
                        parts.append(f"{k}={v}")
                    elif isinstance(v, dict):
                        parts.append(f"{k}={{{','.join(f'{kk}:{vv}' for kk, vv in list(v.items())[:4])}}}")
                if parts:
                    return "; ".join(parts)[:300]
        except ValueError:
            pass
        return out.splitlines()[-1][:300]
    e = (err or "").strip()
    return e.splitlines()[-1][:200] if e else "(no output)"


def run_class(name: str, apply: bool, policy: dict, ctx: hygiene.Ctx) -> dict:
    """Fail-soft execution of one class."""
    mode = "apply" if apply else "dry"
    if name in BUILTIN:
        try:
            res = BUILTIN[name](ctx)
        except Exception as e:  # noqa: BLE001
            return {"class": name, "mode": mode, "rc": -1, "ok": False, "summary": f"{type(e).__name__}: {e}"}
        return {"class": name, "mode": mode, "rc": 1 if res["errors"] else 0,
                "ok": not res["errors"], "summary": _hygiene_summary(res), "detail": res}
    spec = policy.get("classes", {}).get(name, {})
    argv = argv_for(name, apply, policy)
    if not argv:
        return {"class": name, "mode": mode, "rc": -1, "ok": False, "summary": "no argv configured"}
    cwd = config_mod.expand(spec.get("cwd"), ctx.cfg.base) or ctx.cfg.base
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                           timeout=int(spec.get("timeout", 300)))
        return {"class": name, "mode": mode, "rc": r.returncode, "ok": r.returncode == 0,
                "summary": summarize_output(r.stdout, r.stderr)}
    except Exception as e:  # noqa: BLE001
        return {"class": name, "mode": mode, "rc": -1, "ok": False, "summary": f"{type(e).__name__}: {e}"}


def _collect(results: list[dict], key: str) -> list[dict]:
    out = []
    for r in results:
        for item in (r.get("detail") or {}).get(key, []):
            out.append({**item, "class": item.get("class", r["class"])})
    return out


def write_report(cfg: config_mod.Config, results: list[dict], policy: dict, trend: dict | None) -> str:
    """Append a human-readable markdown entry to today's remediation report."""
    day = datetime.date.today().isoformat()
    try:
        cfg.ledger_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.ledger_dir / f"remediation-{day}.md"
        actions = [a for a in _collect(results, "actions") if a.get("op") != "would-close"]
        human = _collect(results, "human")
        delegated = _collect(results, "delegated")
        deferred = _collect(results, "deferred")
        lines = [
            f"# Safe remediation, {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
            "> Acting only on safe, reversible or additive classes. Lossy operations stay with "
            "a human. Every action below is reversible: `arcgov remediate undo <id>`.", "",
            "| Class | Mode | rc | Summary |", "|---|---|---|---|",
        ]
        for r in results:
            lines.append(f"| `{r['class']}` | {r['mode']} | {r['rc']} | {str(r['summary'])[:160]} |")
        if trend:
            lines += ["", "## Backlog trend", "",
                      "| Open | Change | Closed today | Closed all-time | Closure rate |",
                      "|---|---|---|---|---|",
                      f"| {trend['open']} | {trend['delta_open']:+d} | {trend['closed_today']} | "
                      f"{trend['closed_total']} | {trend['closure_rate']:.0%} |"]
        if actions:
            lines += ["", "## Auto-closed (each reversible)", "",
                      "| id | Class | Op | Target | Diff | Restore |", "|---|---|---|---|---|---|"]
            for a in actions:
                n = a.get("diff_lines") or 0
                diff = (a.get("diff_kind") or "-") + (f" ({n} line(s))" if n else "")
                lines.append(f"| `{a.get('id', '-')}` | `{a.get('class', '')}` | {a.get('op', '')} | "
                             f"`{a.get('src', '')}` | {diff} | {str(a.get('restore', ''))[:80]} |")
        if delegated:
            lines += ["", "## Delegated (aged in place, not re-logged)", "",
                      "| Agent | Finding | Due | Result |", "|---|---|---|---|"]
            for d in delegated:
                lines.append(f"| {d['agent']} | `{d['target']}` | {d['due']} | {d['msg']} |")
        if human:
            lines += ["", "## Needs a human decision (open on purpose)", "",
                      "| Class | Item | Age | Why it is not auto-closable |", "|---|---|---|---|"]
            for f in human:
                lines.append(f"| `{f['class']}` | `{f['target']}` | {f['age_days']}d | {f['reason']} |")
        if deferred:
            lines += ["", "## Deferred (owner named, not open-ended)", "", "| Item | Age | Note |", "|---|---|---|"]
            for d in deferred:
                lines.append(f"| `{d['target']}` | {d['age_days']}d | {d['reason']} |")
        lines += ["", f"_policy: enabled={policy.get('enabled')}, "
                  f"classes={','.join(enabled_classes(policy)) or 'none'}_", ""]
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")
        return str(path)
    except OSError as e:
        return f"report error: {e}"


class Engine:
    def __init__(self, cfg: config_mod.Config | None = None, policy_file: Path | None = None,
                 notifier=None):
        self.cfg = cfg or config_mod.load()
        self.policy_file = policy_file or policy_path(self.cfg)
        self.ledger = Ledger(self.cfg.ledger_dir)
        self.state_path = self.cfg.state_dir / "remediation_state.json"
        self.notifier = notifier

    def policy(self) -> dict:
        return load_policy(self.policy_file)

    def run_all(self, classes: list[str], *, apply: bool, policy: dict) -> tuple[list[dict], dict]:
        """Run every class against ONE shared aging state."""
        state = sla.load_state(self.state_path)
        ctx = hygiene.Ctx(self.cfg, policy, state, dry=not apply, ledger=self.ledger)
        results = [run_class(c, apply, policy, ctx) for c in classes]
        scanned = {c for c in classes if c in BUILTIN}
        resolved = sla.reap(state, scanned, ctx.today) if apply else 0
        trend = sla.trend(state)
        trend["resolved_this_run"] = resolved
        if apply:
            sla.save_state(state, self.state_path)
        return results, trend

    def plan(self, only: set[str] | None = None) -> dict:
        policy = self.policy()
        classes = enabled_classes(policy) or known_classes(policy)
        if only:
            classes = [c for c in classes if c in only]
        results, trend = self.run_all(classes, apply=False, policy=policy)
        return {"mode": "PLAN (dry)", "policy_enabled": bool(policy.get("enabled")),
                "enabled_classes": enabled_classes(policy), "trend": trend, "results": results}

    def apply(self, only: set[str] | None = None, *, notify: bool = True) -> dict:
        policy = self.policy()
        if not policy.get("enabled"):
            return {"mode": "APPLY", "applied": False, "reason": "policy disabled", "results": []}
        classes = enabled_classes(policy)
        if only:
            classes = [c for c in classes if c in only]
        if not classes:
            return {"mode": "APPLY", "applied": False, "reason": "no enabled classes", "results": []}
        results, trend = self.run_all(classes, apply=True, policy=policy)
        report = write_report(self.cfg, results, policy, trend)
        notified = False
        applied_ok = [r["class"] for r in results if r["ok"]]
        if notify and applied_ok:
            notifier = self.notifier or notify_mod.get_notifier(self.cfg)
            human = _collect(results, "human")
            text = (f"Safe remediation applied: {', '.join(applied_ok)} (all reversible). "
                    f"Backlog {trend['open']} open ({trend['delta_open']:+d}). Report: {report}")
            if human:
                text += f"\n{len(human)} item(s) need a human decision."
            notified = bool(notifier.send(text, title="safe-remediate", priority="low"))
        return {"mode": "APPLY", "applied": True, "classes": classes, "results": results,
                "trend": trend, "report": report, "notified": notified}

    def routes(self) -> dict:
        policy = self.policy()
        state = sla.load_state(self.state_path)
        rows = []
        for rec in state.get("findings", {}).values():
            klass = rec.get("class", "?")
            age = sla.age_days(rec)
            limit = sla.sla_for(klass, policy)
            rows.append({"class": klass, "owner": rec.get("owner", "?"), "age_days": age,
                         "sla_days": limit, "breached": age >= limit, "first_seen": rec.get("first_seen"),
                         "key": rec.get("key"), "occurrences": rec.get("occurrences", 1)})
        rows.sort(key=lambda r: (-r["age_days"], r["class"]))
        return {"trend": sla.trend(state), "open": rows}

    def status(self) -> dict:
        policy = self.policy()
        return {"policy_file": str(self.policy_file), "policy_enabled": bool(policy.get("enabled")),
                "enabled_classes": enabled_classes(policy), "all_classes": known_classes(policy),
                "never_auto": policy.get("never_auto", []), "self": self.cfg.self_name}


# ── CLI ──────────────────────────────────────────────────────────────────────
def _emit(obj, as_json: bool, lines: list[str]) -> None:
    print(json.dumps(obj, indent=2, default=str) if as_json else "\n".join(lines))


def _human_block(results: list[dict]) -> list[str]:
    human = _collect(results, "human")
    if not human:
        return []
    out = ["", "NEEDS A HUMAN DECISION (not closed):"]
    for f in human:
        out.append(f"   - [{f['class']}] {f['target']}  ({f['age_days']}d)")
        out.append(f"     because: {f['reason']}")
    return out


def _only(args) -> set[str] | None:
    return set(args.only.split(",")) if getattr(args, "only", None) else None


def cmd_plan(args, engine: Engine) -> int:
    p = engine.plan(_only(args))
    t = p["trend"]
    _emit(p, args.json, [
        f"Remediation PLAN (dry), policy_enabled={p['policy_enabled']}",
        *[f"  - {r['class']:18s} would run -> {str(r['summary'])[:90]}" for r in p["results"]],
        f"  backlog: {t['open']} open ({t['delta_open']:+d}), {t['closed_total']} closed all-time, "
        f"closure rate {t['closure_rate']:.0%}",
        *_human_block(p["results"]),
        "  (no writes; run `apply` to act on enabled classes)",
    ])
    return 0


def cmd_apply(args, engine: Engine) -> int:
    p = engine.apply(_only(args), notify=not args.no_notify)
    if not p["applied"]:
        print(f"safe-remediate: {p['reason']}; nothing applied.")
        return 0
    t = p["trend"]
    _emit(p, args.json, [
        f"Remediation APPLIED: {', '.join(p['classes'])}",
        *[f"  - {r['class']:18s} rc={r['rc']} {str(r['summary'])[:90]}" for r in p["results"]],
        f"  backlog: {t['open']} open ({t['delta_open']:+d}), {t['closed_total']} closed all-time, "
        f"closure rate {t['closure_rate']:.0%}",
        *_human_block(p["results"]),
        f"  report: {p['report']}",
    ])
    return 0


def cmd_routes(args, engine: Engine) -> int:
    p = engine.routes()
    t, rows = p["trend"], p["open"]
    _emit(p, args.json, [
        f"Open findings: {t['open']}, closed all-time {t['closed_total']}, closure rate {t['closure_rate']:.0%}",
        *[f"  {'!' if r['breached'] else ' '} {r['class']:14s} {r['owner']:10s} "
          f"age {r['age_days']:>3}d/{r['sla_days']}d  {str(r['key'])[-70:]}" for r in rows[:60]],
        *([f"  ... {len(rows) - 60} more"] if len(rows) > 60 else []),
    ])
    return 0


def cmd_status(args, engine: Engine) -> int:
    s = engine.status()
    _emit(s, args.json, [
        "safe-remediate status",
        f"  policy file      : {s['policy_file']}",
        f"  policy enabled   : {s['policy_enabled']}",
        f"  enabled classes  : {', '.join(s['enabled_classes']) or 'none'}",
        f"  available        : {', '.join(s['all_classes'])}",
        f"  never auto       : {len(s['never_auto'])} item(s) held for a human",
    ])
    return 0


def cmd_undo(args, engine: Engine) -> int:
    ok, msg = engine.ledger.undo(args.action_id)
    print(("undone: " if ok else "failed: ") + msg)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arcgov remediate", description="Bounded, reversible auto-remediation.")
    p.add_argument("--config", help="governance.json (default: ARC_GOVERNANCE_CONFIG or $ARC_ROOT/governance.json)")
    p.add_argument("--policy", help="remediate_policy.json (default: next to the config)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (("plan", cmd_plan, "dry preview (no writes)"),
                               ("apply", cmd_apply, "execute enabled classes (writes)"),
                               ("status", cmd_status, "policy and enabled classes"),
                               ("routes", cmd_routes, "open findings and their routes")):
        sp = sub.add_parser(name, help=helptext)
        if name in ("plan", "apply"):
            sp.add_argument("--only", help="comma list of classes")
        if name == "apply":
            sp.add_argument("--no-notify", action="store_true", help="apply without sending a notice")
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=fn)
    sp = sub.add_parser("undo", help="reverse one ledgered action by id")
    sp.add_argument("action_id")
    sp.set_defaults(func=cmd_undo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_mod.load(args.config)
    engine = Engine(cfg, Path(args.policy).expanduser() if args.policy else None)
    return args.func(args, engine)


if __name__ == "__main__":
    sys.exit(main())
