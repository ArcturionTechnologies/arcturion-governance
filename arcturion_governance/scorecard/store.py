"""Scorecard settings and storage.

Settings live under ``"scorecard"`` in governance.json:

  transcript_dirs   folders of session transcripts   (default ["~/.claude/projects"])
  db_path           SQLite file                       (default <state_dir>/scorecard/scorecard.db)
  project_map       {"path substring": "AGENT"} for transcript folders the roster can't name
  knowledge_tools   regex for tool names that count as a lookup
  min_turns         turns needed before a composite is published   (default 5)
  min_faculties     measured faculties needed for a composite       (default 3)
  speed_target_s    first-response time that scores 100             (default 60)
  idle_cap_s        longest gap counted as active time              (default 600)
  review_threshold  faculty score below which a review adds a mitigation (default 70)
  reviews_per_day   agents reviewed per weekday in the rotation     (default 2)
  card_dir          where a card is saved if it can't be sent       (default <state_dir>/scorecard/cards)

Only numbers and labels are stored. No prompt or response text is kept.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .. import config as config_mod

DEFAULT_KNOWLEDGE_TOOLS = r"(?i)(memory|knowledge|recall|notes_search|search_notes|kb_)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    key               TEXT PRIMARY KEY,
    agent             TEXT NOT NULL,
    session           TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    week              TEXT NOT NULL,
    first_response_s  REAL,
    active_s          REAL,
    steps             INTEGER NOT NULL DEFAULT 0,
    tool_calls        INTEGER NOT NULL DEFAULT 0,
    tool_errors       INTEGER NOT NULL DEFAULT 0,
    skill_calls       INTEGER NOT NULL DEFAULT 0,
    knowledge_calls   INTEGER NOT NULL DEFAULT 0,
    followup          TEXT,
    rework            INTEGER
);
CREATE INDEX IF NOT EXISTS turns_agent_week ON turns(agent, week);

CREATE TABLE IF NOT EXISTS weekly_index (
    agent        TEXT NOT NULL,
    week         TEXT NOT NULL,
    turns        INTEGER NOT NULL,
    economy      REAL,
    precision    REAL,
    speed        REAL,
    recall       REAL,
    composite    REAL,
    trajectory   REAL,
    status       TEXT NOT NULL,
    reasons      TEXT,
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (agent, week)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    component   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    notes       TEXT
);
"""


class Settings:
    def __init__(self, cfg: config_mod.Config | None = None):
        self.cfg = cfg or config_mod.load()
        sec = self.cfg.section("scorecard")
        dirs = sec.get("transcript_dirs") or ["~/.claude/projects"]
        self.transcript_dirs = [config_mod.expand(d, self.cfg.base) for d in dirs]
        self.db_path = config_mod.expand(sec.get("db_path"), self.cfg.base) or (
            self.cfg.state_dir / "scorecard" / "scorecard.db")
        self.project_map = {str(k): str(v).upper() for k, v in (sec.get("project_map") or {}).items()}
        self.knowledge_tools = str(sec.get("knowledge_tools") or DEFAULT_KNOWLEDGE_TOOLS)
        self.min_turns = int(sec.get("min_turns", 5))
        self.min_faculties = int(sec.get("min_faculties", 3))
        self.speed_target_s = float(sec.get("speed_target_s", 60))
        self.idle_cap_s = float(sec.get("idle_cap_s", 600))
        self.review_threshold = float(sec.get("review_threshold", 70))
        self.reviews_per_day = max(1, int(sec.get("reviews_per_day", 2)))
        self.card_dir = config_mod.expand(sec.get("card_dir"), self.cfg.base) or (
            self.cfg.state_dir / "scorecard" / "cards")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def log_run(conn: sqlite3.Connection, component: str, ok: bool, notes: str = "") -> None:
    conn.execute("INSERT INTO runs (component, started_at, ok, notes) VALUES (?, ?, ?, ?)",
                 (component, now_iso(), 1 if ok else 0, notes[:2000]))
    conn.commit()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_ts(value) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(v)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def week_start(d: dt.date | dt.datetime) -> str:
    if isinstance(d, dt.datetime):
        d = d.date()
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def last_full_week(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    return (dt.date.fromisoformat(week_start(today)) - dt.timedelta(days=7)).isoformat()


def median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return float(vals[mid]) if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
