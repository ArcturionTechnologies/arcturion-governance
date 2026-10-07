"""Config loading and the pluggable notifier."""
import io
import json
import os
import unittest

from arcturion_governance import config as config_mod
from arcturion_governance import notify
from tests._helpers import Sandbox


class TestConfig(Sandbox):
    def test_roster_comes_from_config(self):
        self.assertEqual(self.cfg.agent_names(), ["BUILDER", "RESEARCH", "STEWARD"])
        self.assertEqual(self.cfg.self_name, "STEWARD")
        self.assertEqual(self.cfg.agent("builder").home, self.root / "agents" / "builder")
        self.assertEqual(self.cfg.agent("builder").inbox, self.root / "agents" / "builder" / "inbox.md")

    def test_arc_root_is_expanded(self):
        self.assertEqual(self.cfg.ledger_dir, self.root / "governance" / "ledger")
        self.assertEqual(self.cfg.state_dir, self.root / ".governance" / "state")

    def test_aliases_and_unknown_agents(self):
        raw = json.loads(self.cfg_path.read_text())
        raw["agents"]["research"]["aliases"] = ["scout"]
        self.cfg_path.write_text(json.dumps(raw))
        cfg = config_mod.load()
        self.assertEqual(cfg.agent("Scout").name, "RESEARCH")
        self.assertIsNone(cfg.agent("nobody"))

    def test_missing_config_falls_back_to_defaults(self):
        os.environ["ARC_GOVERNANCE_CONFIG"] = str(self.root / "missing.json")
        cfg = config_mod.load()
        self.assertEqual(cfg.self_name, "STEWARD")
        self.assertIn("STEWARD", cfg.agents)
        self.assertEqual(cfg.ledger_dir, self.root / "governance" / "ledger")

    def test_relative_paths_resolve_against_the_config_folder(self):
        self.assertEqual(config_mod.expand("x/y", self.root), self.root / "x" / "y")
        self.assertEqual(config_mod.expand("~/z"), config_mod.Path.home() / "z")


class _Resp:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestNotifier(Sandbox):
    def test_stdout_notifier(self):
        buf = io.StringIO()
        self.assertTrue(notify.StdoutNotifier(buf).send("hello", title="t"))
        self.assertIn("hello", buf.getvalue())

    def test_kind_none_from_config(self):
        self.assertIsInstance(notify.get_notifier(self.cfg), notify.NullNotifier)

    def test_env_override_to_file(self):
        os.environ["GOVERNANCE_NOTIFY"] = "file"
        n = notify.get_notifier(self.cfg)
        self.assertIsInstance(n, notify.FileNotifier)
        self.assertTrue(n.send("note", title="x"))
        row = json.loads((self.cfg.state_dir / "notices.log").read_text().splitlines()[0])
        self.assertEqual(row["text"], "note")

    def test_webhook_reads_url_from_env_and_posts(self):
        seen = {}

        def opener(req, timeout):
            seen["url"], seen["body"], seen["timeout"] = req.full_url, req.data, timeout
            return _Resp()

        n = notify.WebhookNotifier("TEST_HOOK_URL", fmt="slack", opener=opener)
        self.assertFalse(n.send("x"), "no URL in env -> no send")
        os.environ["TEST_HOOK_URL"] = "https://hooks.example.invalid/abc"
        try:
            self.assertTrue(n.send("body", title="Title"))
        finally:
            del os.environ["TEST_HOOK_URL"]
        self.assertEqual(seen["url"], "https://hooks.example.invalid/abc")
        self.assertEqual(json.loads(seen["body"]), {"text": "*Title*\nbody"})

    def test_webhook_failure_returns_false(self):
        def opener(req, timeout):
            raise OSError("down")

        os.environ["TEST_HOOK_URL"] = "https://hooks.example.invalid/abc"
        try:
            self.assertFalse(notify.WebhookNotifier("TEST_HOOK_URL", opener=opener).send("x"))
        finally:
            del os.environ["TEST_HOOK_URL"]

    def test_webhook_payload_formats(self):
        n = notify.WebhookNotifier("X")
        body, ctype = n.payload("t", "T", "low")
        self.assertEqual(json.loads(body), {"title": "T", "text": "t", "priority": "low"})
        n.fmt = "text"
        body, ctype = n.payload("t", "T", "low")
        self.assertTrue(ctype.startswith("text/plain"))


if __name__ == "__main__":
    unittest.main()
