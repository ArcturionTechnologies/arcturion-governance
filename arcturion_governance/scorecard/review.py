"""Review rotation: one short, evidence-backed review per agent per week.

The roster is spread over the weekdays (``reviews_per_day`` agents a day, in a
fixed order), so reviews arrive steadily instead of all on Monday. Each review
looks at the agent's last full week and turns every weak faculty into a
mitigation that cites its evidence (the score, the counts behind it, the week).
A withheld week becomes a single "not enough evidence" note, not a guess.

The review lands in the agent's own inbox as one block, marked with the agent
and week, so re-running replaces the block instead of adding another.
"""
from __future__ import annotations

import datetime as dt
import re

from .. import notify as notify_mod
from . import index as idx
from . import store

ADVICE = {
    "economy": "rework is high: restate the request and the success check before acting, "
               "and verify the result before saying it is done",
    "precision": "many tool calls fail: read the tool's usage or the error before retrying, "
                 "and stop repeating a failing call",
    "speed": "slow first responses: acknowledge and plan briefly before long investigations",
    "recall": "few lookups: search existing notes and decisions before answering or asking",
}


def plan(settings: store.Settings, week: str) -> dict[str, list[str]]:
    """weekday date -> agents reviewed that day, for the week starting ``week``."""
    agents = settings.cfg.agent_names()
    start = dt.date.fromisoformat(week)
    k = settings.reviews_per_day
    out: dict[str, list[str]] = {}
    for i in range(0, len(agents), k):
        day = start + dt.timedelta(days=min(i // k, 4))
        out.setdefault(day.isoformat(), []).extend(agents[i:i + k])
    return out


def build(settings: store.Settings, agent: str, week: str) -> dict:
    rows = {r["agent"]: r for r in idx.load_week(settings, week)}
    row = rows.get(agent.upper())
    mitigations = []
    if row is None:
        mitigations.append({"faculty": None, "text": f"no turns collected for week of {week}",
                            "evidence": "0 turns"})
        status = "no-data"
    elif row["status"] != "ok":
        mitigations.append({"faculty": None, "text": "not enough evidence for a fair score",
                            "evidence": "; ".join(row["reasons"])})
        status = "incomplete"
    else:
        status = "ok"
        for f in idx.FACULTIES:
            v = row.get(f)
            if v is not None and v < settings.review_threshold:
                mitigations.append({"faculty": f, "text": ADVICE[f],
                                    "evidence": f"{f} {v:.0f}/100 over {row['turns']} turn(s), week of {week}"})
    return {"agent": agent.upper(), "week": week, "status": status, "row": row, "mitigations": mitigations}


def render(review: dict) -> str:
    a, w = review["agent"], review["week"]
    lines = [f"<!-- gov-review:{a}:{w} -->", f"## Weekly review, week of {w}"]
    row = review["row"]
    if row and row["status"] == "ok":
        lines.append(f"Composite {row['composite']:.0f}"
                     + (f" ({row['trajectory']:+.1f} vs prior weeks)" if row["trajectory"] is not None else ""))
    if not review["mitigations"]:
        lines.append("- No faculty below threshold. Keep doing what you're doing.")
    for m in review["mitigations"]:
        lines.append(f"- {m['text']} (evidence: {m['evidence']})")
    lines.append(f"<!-- /gov-review:{a}:{w} -->")
    return "\n".join(lines) + "\n"


def write(settings: store.Settings, review: dict, *, dry: bool = False) -> str:
    agent = settings.cfg.agent(review["agent"])
    if agent is None or not agent.home.exists():
        return "no inbox for that agent"
    block = render(review)
    inbox = agent.inbox
    text = inbox.read_text(encoding="utf-8") if inbox.exists() else ""
    a, w = review["agent"], review["week"]
    pattern = re.compile(rf"<!-- gov-review:{re.escape(a)}:{re.escape(w)} -->.*?<!-- /gov-review:"
                         rf"{re.escape(a)}:{re.escape(w)} -->\n?", re.DOTALL)
    if pattern.search(text):
        new = pattern.sub(lambda _m: block, text, count=1)
        verb = "replaced"
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
        verb = "appended"
    if dry:
        return f"would have {verb} review in {inbox.name}"
    inbox.write_text(new, encoding="utf-8")
    return f"{verb} review in {inbox.name}"


def run(settings: store.Settings, *, today: dt.date | None = None, agent: str | None = None,
        week: str | None = None, dry: bool = False, notifier=None) -> list[dict]:
    today = today or dt.date.today()
    week = week or store.last_full_week(today)
    if agent:
        targets = [agent.upper()]
    else:
        targets = plan(settings, store.week_start(today)).get(today.isoformat(), [])
    results = []
    for name in targets:
        review = build(settings, name, week)
        review["result"] = write(settings, review, dry=dry)
        results.append(review)
    if results and not dry:
        n = notifier or notify_mod.get_notifier(settings.cfg)
        summary = "; ".join(f"{r['agent']}: {len(r['mitigations'])} mitigation(s)" for r in results)
        n.send(f"Weekly reviews for week of {week}: {summary}", title="scorecard review", priority="low")
    return results
