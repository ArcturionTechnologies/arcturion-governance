"""Index: roll a week of turns up into faculty scores per agent.

Four faculties are measured deterministically from the turn rows (0-100):

  economy    100 x (1 - rework rate)            rework judged by the person's next message
  precision  100 x (1 - failed tool calls / tool calls)
  speed      100 x min(1, target / median first-response time)
  recall     100 x share of turns that consulted a knowledge or memory tool

Two more (expression, presence: did it say the right thing, did it sound
human) need a judge reading the text. This version keeps no text, so they are
not scored, and the card says so rather than inventing a number.

Rules:
  * a missing measurement is NULL, never 0 (no tool calls means precision is
    unknown, not perfect and not zero);
  * the composite is the mean of the measured faculties, and it is WITHHELD
    (status "incomplete", reasons listed) when there are too few turns or too
    few measured faculties. A thin week never produces a confident number;
  * trajectory (this week minus the mean of up to four earlier weeks) is the
    headline, because an agent improving from 60 to 70 says more than a rank;
  * re-running a week replaces its rows: idempotent.
"""
from __future__ import annotations

import datetime as dt
import json

from . import store

FACULTIES = ("economy", "precision", "speed", "recall")


def _pct(x: float | None) -> float | None:
    return None if x is None else round(max(0.0, min(100.0, x)), 1)


def score_rows(rows: list, settings: store.Settings) -> dict:
    n = len(rows)
    classified = [r for r in rows if r["rework"] is not None]
    tools = sum(r["tool_calls"] for r in rows)
    errors = sum(r["tool_errors"] for r in rows)
    med = store.median([r["first_response_s"] for r in rows])
    economy = (100.0 * (1 - sum(r["rework"] for r in classified) / len(classified))) if classified else None
    precision = (100.0 * (1 - errors / tools)) if tools else None
    if med is None:
        speed = None
    else:
        speed = 100.0 if med <= 0 else 100.0 * min(1.0, settings.speed_target_s / med)
    recall = (100.0 * sum(1 for r in rows if r["knowledge_calls"] > 0) / n) if n else None
    scores = {"economy": _pct(economy), "precision": _pct(precision), "speed": _pct(speed), "recall": _pct(recall)}
    measured = [v for v in scores.values() if v is not None]
    reasons = []
    if n < settings.min_turns:
        reasons.append(f"only {n} turn(s); {settings.min_turns} needed")
    if len(measured) < settings.min_faculties:
        reasons.append(f"only {len(measured)} faculty(ies) measurable; {settings.min_faculties} needed")
    composite = round(sum(measured) / len(measured), 1) if measured and not reasons else None
    return {"turns": n, **scores, "composite": composite,
            "status": "ok" if composite is not None else "incomplete", "reasons": reasons}


def compute_week(settings: store.Settings, week: str) -> list[dict]:
    """Compute and store the index for one week (a Monday, YYYY-MM-DD)."""
    dt.date.fromisoformat(week)
    conn = store.connect(settings.db_path)
    try:
        agents = [r["agent"] for r in conn.execute(
            "SELECT DISTINCT agent FROM turns WHERE week = ? ORDER BY agent", (week,))]
        out = []
        for agent in agents:
            rows = conn.execute("SELECT * FROM turns WHERE agent = ? AND week = ?", (agent, week)).fetchall()
            s = score_rows(rows, settings)
            prior = [r["composite"] for r in conn.execute(
                "SELECT composite FROM weekly_index WHERE agent = ? AND week < ? AND composite IS NOT NULL "
                "ORDER BY week DESC LIMIT 4", (agent, week))]
            trajectory = (round(s["composite"] - sum(prior) / len(prior), 1)
                          if s["composite"] is not None and prior else None)
            row = {"agent": agent, "week": week, **s, "trajectory": trajectory}
            conn.execute(
                "INSERT OR REPLACE INTO weekly_index (agent, week, turns, economy, precision, speed, recall, "
                "composite, trajectory, status, reasons, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (agent, week, s["turns"], s["economy"], s["precision"], s["speed"], s["recall"],
                 s["composite"], trajectory, s["status"], json.dumps(s["reasons"]), store.now_iso()))
            out.append(row)
        conn.commit()
        store.log_run(conn, "index", True, f"week={week} agents={len(out)}")
        return out
    finally:
        conn.close()


def load_week(settings: store.Settings, week: str) -> list[dict]:
    conn = store.connect(settings.db_path)
    try:
        rows = conn.execute("SELECT * FROM weekly_index WHERE week = ? ORDER BY agent", (week,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d.get("reasons") or "[]")
            out.append(d)
        return out
    finally:
        conn.close()
