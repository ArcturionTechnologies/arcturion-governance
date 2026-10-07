"""drift-sentinel: detect and SURFACE drift across a workspace of agents.

The sentinel orchestrates probes you configure. It never reimplements your
checkers and it never mutates the things it inspects. Every remediation it
emits is a PROPOSAL. The only files it ever writes are its own report and the
drift backlog queue (which ``safe-remediate``'s alarm-dedup class then keeps
tidy).

Each probe is fail-soft: a broken probe is reported as amber, never a crash.
Every result is coerced into one finding shape, and a section's status is the
worst of its findings.

Probe kinds (configured under ``"drift": {"probes": [...]}`` in governance.json):

  command              run an argv; count from JSON (``count_keys``), from labelled
                       numbers in the text (``count_patterns``), or rc != 0 -> 1
  stale-files          markdown files whose ``updated:``/``last_reviewed:`` date
                       (else mtime) is older than ``days``
  missing-frontmatter  markdown files missing any of ``required`` frontmatter keys
  leftovers            leftover copies (*.bak, *.old, ...) under ``paths``

Common probe keys: ``name``, ``section`` (default "scan"), ``amber_at`` (1),
``red_at`` (never), ``paths`` (default: every agent home), ``glob`` ("**/*.md").

Commands:
  run [section ...] [--json]       run probes, print findings (no writes)
  report [--apply] [--json]        run all probes, then (with --apply) write the
                                   report and append open items to the backlog
  probes                           list configured probes
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

from .. import config as config_mod
from .. import notify as notify_mod
from ..remediate import hygiene

G, Y, R, U = "🟢", "🟡", "🔴", "⚪"  # green / amber / red / unknown
WORST = {G: 0, U: 1, Y: 1, R: 2}
_ICON = {0: G, 1: Y, 2: R}


# ── finding shape ────────────────────────────────────────────────────────────
def finding(engine: str, status: str, detail: str, *, count: int = 0, data=None, error: str = "") -> dict:
    return {"engine": engine, "status": status,
            "detail": (detail or "").replace("\n", " ")[:300],
            "count": int(count or 0), "data": data if data is not None else {}, "error": error}


def merge_findings(*findings: dict) -> dict:
    """Pure aggregation: worst status wins, counts sum, details kept per probe."""
    items = [f for f in findings if f]
    overall = max((WORST.get(f["status"], 1) for f in items), default=0)
    return {"status": _ICON[overall], "total": sum(f.get("count", 0) for f in items), "findings": items}


def status_for(count: int, *, amber_at: int = 1, red_at: int | None = None, errored: bool = False) -> str:
    if errored:
        return Y
    if red_at is not None and count >= red_at:
        return R
    if count >= amber_at:
        return Y
    return G


def safe_json(text: str):
    """Best-effort JSON parse, tolerating leading banner/log lines."""
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                return None
        return None


def count_from(data, keys) -> int:
    """A count from a probe's JSON whatever its shape. Never raises."""
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return len(v)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
    return 0


def labelled_count(text: str, patterns: list[str]) -> int:
    """Sum the first group of each labelled-number pattern (e.g. ``flags:\\s*(\\d+)``)."""
    total = 0
    for pat in patterns:
        m = re.search(pat, text or "", re.IGNORECASE)
        if m:
            try:
                total += int(m.group(1))
            except (IndexError, ValueError):
                continue
    return total


# ── probes ───────────────────────────────────────────────────────────────────
def _md_files(cfg: config_mod.Config, probe: dict) -> list[Path]:
    paths = [config_mod.expand(p, cfg.base) for p in probe.get("paths", [])]
    roots = [p for p in paths if p is not None] or [a.home for a in cfg.agents.values()]
    glob = probe.get("glob", "**/*.md")
    out: list[Path] = []
    for root in roots:
        if root.is_dir():
            out += [p for p in sorted(root.glob(glob)) if p.is_file() and "/." not in str(p.relative_to(root.parent))]
    return out


def _probe_date(p: Path, keys: list[str]) -> datetime.date:
    fm = hygiene.frontmatter(p)
    for k in keys:
        v = str(fm.get(k, ""))[:10]
        try:
            return datetime.date.fromisoformat(v)
        except ValueError:
            continue
    return datetime.date.fromtimestamp(p.stat().st_mtime)


def probe_stale_files(cfg, probe: dict, today: datetime.date) -> tuple[int, str, dict]:
    days = int(probe.get("days", 90))
    keys = probe.get("date_keys", ["updated", "last_reviewed", "last_modified"])
    stale = [p for p in _md_files(cfg, probe) if (today - _probe_date(p, keys)).days > days]
    sample = ", ".join(p.name for p in stale[:3])
    return len(stale), f"files not reviewed in {days}+ days: {len(stale)}" + (f" (e.g. {sample})" if sample else ""), \
        {"stale": [str(p) for p in stale[:50]]}


def probe_missing_frontmatter(cfg, probe: dict, today: datetime.date) -> tuple[int, str, dict]:
    required = probe.get("required", ["created"])
    bad = [p for p in _md_files(cfg, probe) if any(k not in hygiene.frontmatter(p) for k in required)]
    return len(bad), f"files missing {', '.join(required)}: {len(bad)}", {"files": [str(p) for p in bad[:50]]}


def probe_leftovers(cfg, probe: dict, today: datetime.date) -> tuple[int, str, dict]:
    paths = [config_mod.expand(p, cfg.base) for p in probe.get("paths", [])]
    roots = [p for p in paths if p is not None] or [a.home for a in cfg.agents.values()]
    hits = [p for r in roots for p in hygiene.walk_debris(r, cap=int(probe.get("cap", 500)))]
    return len(hits), f"leftover copies / dead links: {len(hits)} (PROPOSE: diff, then remove)", \
        {"files": [str(p) for p in hits[:50]]}


def probe_command(cfg, probe: dict, today: datetime.date) -> tuple[int, str, dict]:
    argv = [str(a) for a in probe.get("argv") or []]
    if not argv:
        raise ValueError("command probe needs argv")
    cwd = config_mod.expand(probe.get("cwd"), cfg.base) or cfg.base
    p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                       timeout=int(probe.get("timeout", 180)))
    out, err = (p.stdout or "").strip(), (p.stderr or "").strip()
    data = safe_json(out)
    if probe.get("count_keys") and data is not None:
        n = count_from(data, probe["count_keys"])
    elif probe.get("count_patterns"):
        n = labelled_count(out + "\n" + err, probe["count_patterns"])
    else:
        n = 0 if p.returncode == 0 else 1
    last = out.splitlines()[-1] if out else (err.splitlines()[-1] if err else f"rc={p.returncode}")
    return n, last[:200], {"rc": p.returncode}


PROBES = {
    "command": probe_command,
    "stale-files": probe_stale_files,
    "missing-frontmatter": probe_missing_frontmatter,
    "leftovers": probe_leftovers,
}


def run_probe(cfg: config_mod.Config, probe: dict, today: datetime.date | None = None) -> dict:
    """Run one probe, fail-soft."""
    name = str(probe.get("name") or probe.get("kind") or "probe")
    fn = PROBES.get(str(probe.get("kind", "")))
    if fn is None:
        return finding(name, Y, f"unknown probe kind {probe.get('kind')!r}", error="unknown kind")
    try:
        n, detail, data = fn(cfg, probe, today or datetime.date.today())
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        return finding(name, Y, "probe errored", error=f"{type(e).__name__}: {e}"[:300])
    red_at = probe.get("red_at")
    status = status_for(n, amber_at=int(probe.get("amber_at", 1)),
                        red_at=int(red_at) if red_at is not None else None)
    return finding(name, status, detail, count=n, data=data)


def configured_probes(cfg: config_mod.Config) -> list[dict]:
    probes = cfg.section("drift").get("probes")
    if isinstance(probes, list) and probes:
        return [p for p in probes if isinstance(p, dict)]
    # Safe defaults: two read-only checks over every agent home.
    return [{"name": "leftovers", "kind": "leftovers", "section": "scan", "red_at": 50},
            {"name": "stale-notes", "kind": "stale-files", "section": "memory", "days": 120, "red_at": 100}]


def run_sections(cfg: config_mod.Config, only: list[str] | None = None,
                 today: datetime.date | None = None) -> dict[str, dict]:
    by_section: dict[str, list[dict]] = {}
    for probe in configured_probes(cfg):
        sec = str(probe.get("section") or "scan")
        if only and sec not in only:
            continue
        by_section.setdefault(sec, []).append(run_probe(cfg, probe, today))
    return {sec: merge_findings(*fs) for sec, fs in by_section.items()}


# ── report: the report file + backlog lines (PROPOSALS only) ─────────────────
def overall_icon(sections: dict[str, dict]) -> str:
    worst = max((WORST.get(s.get("status", G), 1) for s in sections.values()), default=0)
    return _ICON[worst]


def report_body(ts: datetime.datetime, sections: dict[str, dict]) -> str:
    icon = overall_icon(sections)
    lines = [
        f"# Drift sweep, {ts.strftime('%Y-%m-%d %H:%M')}", "",
        f"> {icon} Every remediation below is a **PROPOSAL** with the change described. The "
        "sentinel never absorbs, deletes or rewrites what it inspects (diff before delete).", "",
        "| Section | Probe | Status | Detail | Proposed remediation |",
        "|---|---|---|---|---|",
    ]
    for name, sec in sections.items():
        for f in sec.get("findings", []):
            remediation = ("none, clean" if f["status"] == G else
                           f"PROPOSE: review {f['count']} item(s); diff before any change. No auto-action.")
            lines.append(f"| {name} | `{f['engine']}` | {f['status']} | {f['detail']} | {remediation} |")
    lines += ["", "## Next",
              "- Triage each amber/red row, resolve it, then note the correction.",
              "- This sweep changed nothing it inspected.", ""]
    return "\n".join(lines) + "\n"


def queue_lines(day: str, sections: dict[str, dict]) -> list[str]:
    """Backlog lines in the format alarm-dedup understands."""
    out = []
    for sec in sections.values():
        for f in sec.get("findings", []):
            if f["status"] in (Y, R):
                out.append(f"- [ ] {f['status']} {day} `{f['engine']}` — {f['detail']} (PROPOSAL only)")
    return out


def report(cfg: config_mod.Config, sections: dict[str, dict], *, apply: bool,
           notifier=None, now: datetime.datetime | None = None) -> dict:
    """Write the report and append the backlog. apply=False is a preview."""
    ts = now or datetime.datetime.now().astimezone()
    day = ts.date().isoformat()
    report_file = cfg.ledger_dir / "drift" / f"drift-{day}.md"
    queue = hygiene.drift_queue_path(cfg)
    body = report_body(ts, sections)
    lines = queue_lines(day, sections)
    written: list[str] = []
    notified = False
    if apply:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(body, encoding="utf-8")
        written.append(str(report_file))
        if lines:
            queue.parent.mkdir(parents=True, exist_ok=True)
            header = "" if queue.exists() else (
                "# Drift backlog\n\n> Each line is a PROPOSAL from drift-sentinel. "
                "Nothing here is auto-applied.\n\n")
            with queue.open("a", encoding="utf-8") as fh:
                fh.write(header + f"\n## {day} sweep\n" + "\n".join(lines) + "\n")
            written.append(str(queue))
        if overall_icon(sections) == R:
            n = notifier or notify_mod.get_notifier(cfg)
            reds = [f"{f['engine']}: {f['detail']}" for s in sections.values()
                    for f in s["findings"] if f["status"] == R]
            notified = bool(n.send("Drift sentinel found red items:\n" + "\n".join(reds)
                                   + f"\nReport: {report_file}", title="drift-sentinel", priority="high"))
    return {"applied": apply, "report": str(report_file), "queue": str(queue),
            "queued_items": len(lines), "written": written, "preview": body, "notified": notified}


# ── CLI ──────────────────────────────────────────────────────────────────────
def _print_sections(sections: dict[str, dict]) -> None:
    for name, sec in sections.items():
        print(f"\n{sec['status']} {name.upper()}: total flagged {sec['total']}")
        for f in sec.get("findings", []):
            print(f"  {f['status']} {f['engine']}: {f['detail']}" + (f"  [{f['error']}]" if f["error"] else ""))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arcgov drift",
                                description="Detect and surface drift; proposals only, never mutates.")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("run", help="run probes and print findings (no writes)")
    sp.add_argument("sections", nargs="*")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("report", help="run every probe, then write the report + backlog with --apply")
    sp.add_argument("--apply", action="store_true", help="write the report and backlog (default: preview)")
    sp.add_argument("--json", action="store_true")
    sub.add_parser("probes", help="list configured probes")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_mod.load(args.config)
    if args.cmd == "probes":
        for probe in configured_probes(cfg):
            print(f"{probe.get('section', 'scan'):8} {probe.get('kind'):20} {probe.get('name', '')}")
        return 0
    sections = run_sections(cfg, args.sections if args.cmd == "run" else None)
    worst = WORST.get(overall_icon(sections), 0)
    if args.cmd == "run":
        if args.json:
            print(json.dumps(sections, indent=2, default=str))
        else:
            _print_sections(sections)
        return 0 if worst < 2 else 2
    result = report(cfg, sections, apply=args.apply)
    if args.json:
        print(json.dumps({"sections": sections, "report": {k: v for k, v in result.items() if k != "preview"}},
                         indent=2, default=str))
    else:
        _print_sections(sections)
        print(f"\n{'WROTE' if result['applied'] else 'PREVIEW (use --apply to write)'}")
        print(f"  report: {result['report']}")
        print(f"  queue:  {result['queue']} (+{result['queued_items']} items)")
    return 0 if worst < 2 else 2


if __name__ == "__main__":
    sys.exit(main())
