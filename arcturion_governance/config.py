"""Shared configuration: where things live, who the agents are, how to notify.

One JSON file describes a workspace of agents. Every path in it may use
``$ARC_ROOT``, ``$HOME`` or ``~``; nothing is hard-coded. Secrets never live in
this file: anything sensitive (a webhook URL, an API key) is named here by the
environment variable that holds it.

Lookup order for the file:

  1. ``ARC_GOVERNANCE_CONFIG`` (a path)
  2. ``$ARC_ROOT/governance.json``
  3. ``~/.config/arcturion-governance/governance.json``

A missing file is not an error: every tool runs on defaults (a single agent
named ``steward`` rooted at ``$ARC_ROOT`` or the current folder), which is
enough for a dry run.

Example::

    {
      "self": "steward",
      "human": "operator",
      "state_dir": "$ARC_ROOT/.governance/state",
      "ledger_dir": "$ARC_ROOT/governance/ledger",
      "agents": {
        "steward":  {"home": "$ARC_ROOT/agents/steward"},
        "builder":  {"home": "$ARC_ROOT/agents/builder", "inbox": "inbox.md"},
        "research": {"home": "$ARC_ROOT/agents/research", "aliases": ["scout"]}
      },
      "retired": {"oldbot": "builder"},
      "notify": {"kind": "webhook", "url_env": "GOVERNANCE_WEBHOOK_URL"}
    }
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def arc_root() -> Path:
    raw = os.environ.get("ARC_ROOT")
    return Path(raw).expanduser() if raw else Path.cwd()


def expand(value: str | os.PathLike | None, base: Path | None = None) -> Path | None:
    """Expand $ARC_ROOT / $HOME / ~ in a configured path. Relative paths resolve
    against ``base`` (the config file's folder) or $ARC_ROOT."""
    if value is None or value == "":
        return None
    text = str(value)
    root = str(arc_root())
    text = text.replace("${ARC_ROOT}", root).replace("$ARC_ROOT", root)
    text = os.path.expanduser(os.path.expandvars(text))
    p = Path(text)
    if not p.is_absolute():
        p = (base or arc_root()) / p
    return p


def config_path() -> Path:
    env = os.environ.get("ARC_GOVERNANCE_CONFIG")
    if env:
        return Path(env).expanduser()
    candidate = arc_root() / "governance.json"
    if candidate.exists():
        return candidate
    return Path.home() / ".config" / "arcturion-governance" / "governance.json"


@dataclass
class Agent:
    name: str
    home: Path
    inbox_name: str = "inbox.md"
    aliases: tuple[str, ...] = ()

    @property
    def inbox(self) -> Path:
        return self.home / self.inbox_name


@dataclass
class Config:
    raw: dict = field(default_factory=dict)
    base: Path = field(default_factory=arc_root)

    # ── identity ────────────────────────────────────────────────────────────
    @property
    def self_name(self) -> str:
        """The governing agent: findings it owns may be auto-closed."""
        return str(self.raw.get("self") or "steward").upper()

    @property
    def human_name(self) -> str:
        """Label for the human decision route."""
        return str(self.raw.get("human") or "human").upper()

    # ── roster ──────────────────────────────────────────────────────────────
    @property
    def agents(self) -> dict[str, Agent]:
        out: dict[str, Agent] = {}
        for name, spec in (self.raw.get("agents") or {}).items():
            spec = spec or {}
            home = expand(spec.get("home"), self.base) or (arc_root() / name)
            out[name.upper()] = Agent(name.upper(), home, str(spec.get("inbox") or "inbox.md"),
                                      tuple(a.upper() for a in spec.get("aliases") or ()))
        if self.self_name not in out:
            out[self.self_name] = Agent(self.self_name, arc_root())
        return out

    @property
    def retired(self) -> dict[str, str]:
        return {k.upper(): v.upper() for k, v in (self.raw.get("retired") or {}).items()}

    def agent(self, name: str | None) -> Agent | None:
        if not name:
            return None
        key = name.upper()
        agents = self.agents
        if key in agents:
            return agents[key]
        for a in agents.values():
            if key in a.aliases:
                return a
        return None

    def agent_names(self) -> list[str]:
        return sorted(self.agents)

    # ── folders ─────────────────────────────────────────────────────────────
    def path(self, key: str, default: str) -> Path:
        p = expand(self.raw.get(key), self.base)
        return p if p is not None else (arc_root() / default)

    @property
    def state_dir(self) -> Path:
        return self.path("state_dir", ".governance/state")

    @property
    def ledger_dir(self) -> Path:
        return self.path("ledger_dir", "governance/ledger")

    def section(self, name: str) -> dict[str, Any]:
        sec = self.raw.get(name)
        return sec if isinstance(sec, dict) else {}


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path).expanduser() if path else config_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
        base = p.parent
    except (OSError, ValueError):
        raw, base = {}, arc_root()
    return Config(raw=raw, base=base)
