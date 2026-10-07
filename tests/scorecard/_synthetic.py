"""Hand-written synthetic transcripts. Nothing here comes from a real session.

``Session`` builds Claude-Code-style JSON Lines: one record per message with a
role, a timestamp and content blocks. Tool results are stored as user-role
rows, exactly the case the collector must not mistake for a human message.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from tests._helpers import Sandbox


class Session:
    def __init__(self, start: str = "2026-07-06T09:00:00+00:00", session_id: str = "s-0001"):
        self.t = dt.datetime.fromisoformat(start)
        self.session_id = session_id
        self.rows: list[dict] = []
        self._ids = 0

    def _stamp(self, seconds: float) -> str:
        self.t += dt.timedelta(seconds=seconds)
        return self.t.isoformat()

    def user(self, text: str, after: float = 30) -> "Session":
        self.rows.append({"type": "user", "sessionId": self.session_id, "timestamp": self._stamp(after),
                          "message": {"role": "user", "content": text}})
        return self

    def say(self, text: str, after: float = 5) -> "Session":
        self.rows.append({"type": "assistant", "sessionId": self.session_id, "timestamp": self._stamp(after),
                          "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
        return self

    def tool(self, name: str, ok: bool = True, after: float = 3) -> "Session":
        self._ids += 1
        tid = f"tool-{self._ids}"
        self.rows.append({"type": "assistant", "sessionId": self.session_id, "timestamp": self._stamp(after),
                          "message": {"role": "assistant", "content": [
                              {"type": "tool_use", "id": tid, "name": name, "input": {}}]}})
        result = {"type": "tool_result", "tool_use_id": tid, "content": "ok" if ok else "error"}
        if not ok:
            result["is_error"] = True
        self.rows.append({"type": "user", "sessionId": self.session_id, "timestamp": self._stamp(1),
                          "message": {"role": "user", "content": [result]}})
        return self

    def write(self, transcripts: Path, project: str, name: str | None = None) -> Path:
        d = transcripts / project
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name or self.session_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        return path


class ScoreSandbox(Sandbox):
    """A workspace whose scorecard reads transcripts from a temp folder."""

    def setUp(self):
        self.transcripts = None
        super().setUp()
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        raw = json.loads(self.cfg_path.read_text())
        raw["scorecard"] = {"transcript_dirs": ["$ARC_ROOT/transcripts"], "min_turns": 3,
                            "project_map": {"-legacy-helper": "BUILDER"}}
        raw["retired"] = {"oldbot": "research"}
        self.cfg_path.write_text(json.dumps(raw))
        from arcturion_governance import config as config_mod
        from arcturion_governance.scorecard import store
        self.cfg = config_mod.load()
        self.settings = store.Settings(self.cfg)


def good_week(start: str, sid: str) -> Session:
    """Five clean turns: lookups, working tools, the person moves on each time."""
    s = Session(start, sid)
    for i, topic in enumerate(("release notes", "test flake", "docs index", "dependency bump", "lint config")):
        s.user(f"Please handle the {topic} task number {i}.", after=600)
        s.tool("notes_search").tool("Bash").say(f"Done with {topic}; tests pass.", after=10)
    s.user("Thanks, that is everything for today.", after=120)
    return s


def rough_week(start: str, sid: str) -> Session:
    """Corrections, a repeated request, failing tools, slow responses, no lookups."""
    s = Session(start, sid)
    s.user("Rename the config loader and update its callers.", after=600)
    s.tool("Bash", ok=False, after=200).tool("Bash", ok=False).say("Renamed it.")
    s.user("That's not what I asked, keep the old name as an alias.", after=60)
    s.tool("Edit", after=180).say("Added the alias.")
    s.user("It still doesn't work when I import the old name.", after=60)
    s.tool("Bash", ok=False, after=240).say("Fixed the import.")
    s.user("It still doesn't work when I import the old name.", after=60)
    s.tool("Bash", after=200).say("Now it imports.")
    s.user("What do you mean by the shim layer?", after=60)
    s.say("The small module that re-exports the old name.", after=150)
    s.user("OK, next: add a changelog entry for this.", after=60)
    return s
