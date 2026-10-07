"""Shared test sandbox: a temporary ARC_ROOT with a governance.json and agent homes.

Every path is resolved up front (macOS puts temp dirs under /var, which is a
symlink to /private/var), so string comparisons of paths stay stable.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from arcturion_governance import config as config_mod

ENV_KEYS = ("ARC_ROOT", "ARC_GOVERNANCE_CONFIG", "ARC_REMEDIATE_POLICY", "GOVERNANCE_NOTIFY",
            "GOVERNANCE_WEBHOOK_URL", "ADVISOR_API_KEY", "ADVISOR_ENDPOINT")


def make_workspace(agents=("steward", "builder", "research"), extra: dict | None = None) -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="arcgov-test-")).resolve()
    spec = {"self": "steward", "human": "operator",
            "state_dir": "$ARC_ROOT/.governance/state",
            "ledger_dir": "$ARC_ROOT/governance/ledger",
            "drift_queue": "$ARC_ROOT/governance/drift-backlog.md",
            "agents": {a: {"home": f"$ARC_ROOT/agents/{a}"} for a in agents},
            "notify": {"kind": "none"}}
    spec.update(extra or {})
    for a in agents:
        (root / "agents" / a).mkdir(parents=True)
    cfg_path = root / "governance.json"
    cfg_path.write_text(json.dumps(spec, indent=2))
    return root, cfg_path


class Sandbox(unittest.TestCase):
    """Points ARC_ROOT / ARC_GOVERNANCE_CONFIG at a fresh temp workspace."""

    agents = ("steward", "builder", "research")
    extra: dict | None = None

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.root, self.cfg_path = make_workspace(self.agents, self.extra)
        os.environ["ARC_ROOT"] = str(self.root)
        os.environ["ARC_GOVERNANCE_CONFIG"] = str(self.cfg_path)
        self.cfg = config_mod.load()

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)

    def home(self, agent="steward") -> Path:
        return self.root / "agents" / agent

    @staticmethod
    def age(p: Path, days: int) -> None:
        t = (datetime.datetime.now() - datetime.timedelta(days=days)).timestamp()
        os.utime(p, (t, t), follow_symlinks=False) if p.is_symlink() else os.utime(p, (t, t))
