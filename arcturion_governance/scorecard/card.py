"""Card: the weekly scorecard, rendered for people and sent through the notifier.

Rows are sorted by trajectory first (who is improving), composite second.
Incomplete rows show why they were withheld. If the notifier fails, the card is
written to ``card_dir`` so a week is never lost.
"""
from __future__ import annotations

from pathlib import Path

from .. import notify as notify_mod
from . import index as idx
from . import store


def _fmt(v, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.1f}" if signed else f"{v:.0f}"


def render(rows: list[dict], week: str) -> str:
    lines = [f"Agent scorecard, week of {week}", ""]
    if not rows:
        return "\n".join(lines + ["No turns were collected for this week."]) + "\n"
    ranked = sorted(rows, key=lambda r: (r["trajectory"] is None, -(r["trajectory"] or 0),
                                         r["composite"] is None, -(r["composite"] or 0), r["agent"]))
    lines += ["| Agent | Turns | Economy | Precision | Speed | Recall | Composite | vs prior 4 wk |",
              "|---|---|---|---|---|---|---|---|"]
    for r in ranked:
        comp = _fmt(r["composite"]) if r["status"] == "ok" else "withheld"
        lines.append(f"| {r['agent']} | {r['turns']} | {_fmt(r['economy'])} | {_fmt(r['precision'])} | "
                     f"{_fmt(r['speed'])} | {_fmt(r['recall'])} | {comp} | {_fmt(r['trajectory'], True)} |")
    withheld = [r for r in ranked if r["status"] != "ok"]
    if withheld:
        lines += ["", "Withheld (not enough evidence for a fair number):"]
        lines += [f"- {r['agent']}: {'; '.join(r['reasons'])}" for r in withheld]
    if any(r["agent"] == "UNKNOWN" for r in rows):
        lines += ["", "UNKNOWN: transcripts that could not be matched to a roster agent "
                      "(add a `project_map` entry)."]
    lines += ["", "Expression and presence need a judge and are not scored here. "
                  "n/a = not measured this week (never treated as zero)."]
    return "\n".join(lines) + "\n"


def send(settings: store.Settings, week: str, *, notifier=None) -> dict:
    text = render(idx.load_week(settings, week), week)
    n = notifier or notify_mod.get_notifier(settings.cfg)
    ok = bool(n.send(text, title=f"Agent scorecard {week}", priority="low"))
    saved = None
    if not ok:
        settings.card_dir.mkdir(parents=True, exist_ok=True)
        saved = Path(settings.card_dir) / f"card-{week}.md"
        saved.write_text(text, encoding="utf-8")
    return {"sent": ok, "fallback": str(saved) if saved else None, "text": text}
