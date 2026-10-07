"""Decision journal with retro discipline.

Log a consequential decision together with the outcome you expect and a date
to review it. When the date comes, the journal says the retro is due; the
retro records what actually happened and a verdict (good-call / bad-call /
mixed). Over time that is calibration data: how often did the reasoning hold?

The log is append-only JSON Lines. Decision rows are never rewritten; a retro
is a separate row that references the decision id. Rows of any other ``type``
already in the file are left untouched and ignored.

    arcgov journal log "Move CI to the faster runner" --expect "builds under 5 min" --review 2026-11-01
    arcgov journal due [--notify]
    arcgov journal retro dec_1a2b3c4d --actual "builds take 4 min" --verdict good-call

Log location: ``--log-file``, else ``ARC_DECISION_LOG``, else
``journal.log_file`` in governance.json, else ``<state_dir>/decisions-log.jsonl``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

from . import config as config_mod
from . import notify as notify_mod

VERDICTS = ("good-call", "bad-call", "mixed")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rows(log: Path) -> list[dict]:
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append(log: Path, row: dict) -> dict:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def log_decision(log: Path, decision: str, expected_outcome: str | None = None,
                 review_date: str | None = None, agent: str = "agent") -> dict:
    if not decision or not decision.strip():
        raise ValueError("decision text is required")
    if review_date:
        date.fromisoformat(review_date)  # raises on a malformed date
    row = {"type": "decision", "id": f"dec_{uuid.uuid4().hex[:8]}",
           "decision": decision, "agent": agent, "ts": _now()}
    if expected_outcome:
        row["expected_outcome"] = expected_outcome
    if review_date:
        row["review_date"] = review_date
    return _append(log, row)


def due(log: Path, today: str | None = None) -> list[dict]:
    today = today or date.today().isoformat()
    all_rows = rows(log)
    retroed = {r.get("decision_id") for r in all_rows if r.get("type") == "retro"}
    return [r for r in all_rows
            if r.get("type") == "decision" and r.get("review_date")
            and r["review_date"] <= today and r["id"] not in retroed]


def retro(log: Path, decision_id: str, actual: str, verdict: str) -> dict:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    known = {r["id"] for r in rows(log) if r.get("type") == "decision"}
    if decision_id not in known:
        raise KeyError(f"no decision with id {decision_id}")
    return _append(log, {"type": "retro", "decision_id": decision_id,
                         "actual": actual, "verdict": verdict, "ts": _now()})


def calibration(log: Path) -> dict:
    """Verdict counts across every retro so far."""
    counts = {v: 0 for v in VERDICTS}
    for r in rows(log):
        if r.get("type") == "retro" and r.get("verdict") in counts:
            counts[r["verdict"]] += 1
    total = sum(counts.values())
    counts["total"] = total
    counts["good_call_rate"] = round(counts["good-call"] / total, 3) if total else None
    return counts


def default_log(cfg: config_mod.Config) -> Path:
    env = os.environ.get("ARC_DECISION_LOG")
    if env:
        return Path(env).expanduser()
    configured = cfg.section("journal").get("log_file")
    if configured:
        return config_mod.expand(configured, cfg.base)
    return cfg.state_dir / "decisions-log.jsonl"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="arcgov journal", description="Decision journal with retros.")
    ap.add_argument("cmd", choices=["log", "due", "retro", "stats"])
    ap.add_argument("arg", nargs="?", help="decision text (log) or id (retro)")
    ap.add_argument("--expect")
    ap.add_argument("--review", help="review date, YYYY-MM-DD")
    ap.add_argument("--actual")
    ap.add_argument("--verdict", choices=VERDICTS)
    ap.add_argument("--agent", default=os.environ.get("ARC_AGENT", "agent"))
    ap.add_argument("--notify", action="store_true", help="send one notice when retros are due")
    ap.add_argument("--log-file", type=Path)
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = config_mod.load(a.config)
    log = a.log_file or default_log(cfg)

    try:
        if a.cmd == "log":
            print(json.dumps(log_decision(log, a.arg or "", a.expect, a.review, a.agent), indent=2))
        elif a.cmd == "due":
            items = due(log)
            for r in items:
                print(f"[{r['id']}] {r['decision']} (expected: {r.get('expected_outcome', '-')}; "
                      f"review {r['review_date']})")
            if not items:
                print("no retros due")
            elif a.notify:
                notify_mod.get_notifier(cfg).send(
                    f"{len(items)} decision retro(s) due. Run `arcgov journal due` for the list.",
                    title="decision-journal")
        elif a.cmd == "retro":
            if not a.arg or not a.verdict:
                ap.error("retro needs a decision id and --verdict")
            print(json.dumps(retro(log, a.arg, a.actual or "", a.verdict), indent=2))
        else:
            print(json.dumps(calibration(log), indent=2))
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
