"""Collect: mine session transcripts already on disk into one row per turn.

Passive by design: it reads transcripts the agent harness already writes (JSON
Lines, one record per message, as Claude Code stores them), so it adds no
hooks and no latency to a live agent.

A turn is one human message plus everything the agent did before the next
human message. Tool results stored as user-role rows are not human messages.

Rework is measured by what the person says NEXT, not by what the agent says
about itself. The next human message is classified as:

  new_request    moving on
  clarification  asking what the agent meant
  correction     "that's not what I asked", "still broken", "try again"
  failed_repeat  essentially the same request again

Only correction and failed_repeat count as rework. The last turn of a file has
no follow-up yet, so its rework is NULL (unknown), never 0.

Re-running over the same files changes nothing: each turn has a stable key.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import store

_CORRECTION = re.compile(
    r"\b(that'?s not (what|it|right)|not what i (asked|meant|wanted)|you (didn'?t|forgot|missed|ignored)"
    r"|still (broken|failing|wrong|not working)|(doesn'?t|does not|didn'?t|did not) work"
    r"|wrong|undo that|revert that|try again)\b"
    r"|^\s*(no|nope)\b[,.! ]",
    re.IGNORECASE)
_CLARIFICATION = re.compile(
    r"^\s*(what do you mean|what does .{1,60} mean|can you (explain|clarify)|which one|why did you"
    r"|i don'?t understand)", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")


def _norm(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def classify_followup(prompt: str, nxt: str) -> str:
    a, b = _norm(prompt), _norm(nxt)
    if len(b) >= 10 and a and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
        return "failed_repeat"
    if _CORRECTION.search(nxt or ""):
        return "correction"
    if _CLARIFICATION.search(nxt or ""):
        return "clarification"
    return "new_request"


# ── agent attribution ────────────────────────────────────────────────────────
def derive_agent(project_dir: str, settings: store.Settings) -> str:
    """Name the agent behind a transcript folder.

    1. an explicit ``project_map`` substring match;
    2. the LAST path token naming a roster agent (or alias), so the most specific
       folder wins; a retired name maps to its successor;
    3. otherwise UNKNOWN (reported, never discarded).
    """
    for needle, agent in settings.project_map.items():
        if needle and needle in project_dir:
            return agent
    cfg = settings.cfg
    tokens = [t.upper() for t in re.split(r"[-/_. ]+", project_dir or "") if t]
    for tok in reversed(tokens):
        agent = cfg.agent(tok)
        if agent is not None:
            return agent.name
        if tok in cfg.retired:
            return cfg.retired[tok]
    return "UNKNOWN"


# ── transcript parsing ───────────────────────────────────────────────────────
def _rows(path: Path) -> list[dict]:
    out = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def _role(rec: dict) -> str:
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    return rec.get("role") or msg.get("role") or rec.get("type") or ""


def _blocks(rec: dict) -> list:
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    content = msg.get("content", rec.get("content"))
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(rec: dict) -> str:
    return "\n".join(str(b.get("text", "")) for b in _blocks(rec) if b.get("type") == "text")


def is_human(rec: dict) -> bool:
    return _role(rec) == "user" and any(b.get("type") == "text" and str(b.get("text", "")).strip()
                                        for b in _blocks(rec))


@dataclass
class Turn:
    key: str
    agent: str
    session: str
    started: dt.datetime
    prompt: str
    rows: list = field(default_factory=list)
    followup: str | None = None

    def metrics(self, settings: store.Settings, knowledge_re: re.Pattern) -> dict:
        stamps = [store.parse_ts(r.get("timestamp")) for r in self.rows]
        first_resp = None
        for r, ts in zip(self.rows, stamps):
            if _role(r) == "assistant" and ts is not None:
                first_resp = max(0.0, (ts - self.started).total_seconds())
                break
        times = [self.started] + [t for t in stamps if t is not None]
        active = sum(min(settings.idle_cap_s, max(0.0, (b - a).total_seconds()))
                     for a, b in zip(times, times[1:])) if len(times) > 1 else None
        steps = tools = errors = skills = knowledge = 0
        for r in self.rows:
            blocks = _blocks(r)
            if _role(r) == "assistant":
                steps += 1
            for b in blocks:
                if b.get("type") == "tool_use":
                    tools += 1
                    name = str(b.get("name", ""))
                    if name == "Skill":
                        skills += 1
                    if knowledge_re.search(name):
                        knowledge += 1
                elif b.get("type") == "tool_result" and (b.get("is_error") or b.get("isError")):
                    errors += 1
        rework = None if self.followup is None else int(self.followup in ("correction", "failed_repeat"))
        return {"key": self.key, "agent": self.agent, "session": self.session,
                "started_at": self.started.isoformat(timespec="seconds"),
                "week": store.week_start(self.started), "first_response_s": first_resp,
                "active_s": active, "steps": steps, "tool_calls": tools, "tool_errors": errors,
                "skill_calls": skills, "knowledge_calls": knowledge, "followup": self.followup,
                "rework": rework}


def turns_from_file(path: Path, agent: str) -> list[Turn]:
    turns: list[Turn] = []
    current: Turn | None = None
    for rec in _rows(path):
        if is_human(rec):
            ts = store.parse_ts(rec.get("timestamp"))
            if ts is None:
                current = None  # an undated message cannot be placed in a week
                continue
            if turns and turns[-1].followup is None:
                turns[-1].followup = classify_followup(turns[-1].prompt, _text(rec))
            current = Turn(f"{path.stem}:{len(turns)}", agent, str(rec.get("sessionId") or path.stem),
                           ts, _text(rec))
            turns.append(current)
        elif current is not None:
            current.rows.append(rec)
    return turns


def iter_transcripts(settings: store.Settings) -> Iterator[tuple[str, Path]]:
    for root in settings.transcript_dirs:
        if root is None or not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            for f in sorted(d.glob("*.jsonl")):
                yield d.name, f


def collect(settings: store.Settings, *, since: dt.date | None = None) -> dict:
    knowledge_re = re.compile(settings.knowledge_tools)
    conn = store.connect(settings.db_path)
    files = written = 0
    by_agent: dict[str, int] = {}
    try:
        for project, path in iter_transcripts(settings):
            files += 1
            agent = derive_agent(project, settings)
            for turn in turns_from_file(path, agent):
                if since and turn.started.date() < since:
                    continue
                m = turn.metrics(settings, knowledge_re)
                conn.execute(
                    "INSERT OR REPLACE INTO turns (key, agent, session, started_at, week, first_response_s, "
                    "active_s, steps, tool_calls, tool_errors, skill_calls, knowledge_calls, followup, rework) "
                    "VALUES (:key, :agent, :session, :started_at, :week, :first_response_s, :active_s, :steps, "
                    ":tool_calls, :tool_errors, :skill_calls, :knowledge_calls, :followup, :rework)", m)
                written += 1
                by_agent[agent] = by_agent.get(agent, 0) + 1
        conn.commit()
        store.log_run(conn, "collect", True, f"files={files} turns={written}")
    finally:
        conn.close()
    return {"files": files, "turns": written, "by_agent": by_agent}
