"""safe-remediate engine: the policy gate, command classes, plan/apply/undo."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from arcturion_governance.remediate import engine as E
from arcturion_governance.remediate import sla
from arcturion_governance.remediate.ledger import Ledger
from tests._helpers import Sandbox


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, text, *, title="", priority="normal"):
        self.sent.append(text)
        return True


class TestPolicyGate(unittest.TestCase):
    def test_disabled_policy_yields_no_classes(self):
        self.assertEqual(E.enabled_classes({"enabled": False, "classes": {"drift-debris": {"enabled": True}}}), [])

    def test_enabled_only_returns_enabled_known_classes(self):
        pol = {"enabled": True, "classes": {
            "drift-debris": {"enabled": True},
            "stray-root": {"enabled": False},
            "frontmatter-stamp": {"enabled": True, "type": "command", "dry": ["true"]},
            "bogus-class": {"enabled": True},  # not built in, not a command -> ignored
        }}
        self.assertEqual(E.enabled_classes(pol), ["drift-debris", "frontmatter-stamp"])

    def test_missing_or_broken_policy_is_disabled(self):
        self.assertFalse(E.load_policy(Path("/nonexistent/policy.json"))["enabled"])
        self.assertFalse(E.load_policy(None)["enabled"])

    def test_shipped_example_policy_is_safe_only(self):
        root = Path(__file__).resolve().parent.parent
        pol = E.load_policy(root / "examples" / "remediate_policy.json")
        for c in E.enabled_classes(pol):
            self.assertIn(c, E.known_classes(pol))
        for lossy in ("dedup-merge", "merge", "retire"):
            self.assertNotIn(lossy, pol.get("classes", {}))


class TestArgvBuilding(unittest.TestCase):
    POL = {"classes": {"stamp": {"type": "command",
                                 "dry": ["python3", "stamp.py", "--json"],
                                 "apply": ["python3", "stamp.py", "--apply", "--limit", "{limit}"],
                                 "limit": 50}}}

    def test_apply_has_apply_flag_dry_does_not(self):
        self.assertNotIn("--apply", E.argv_for("stamp", False, self.POL))
        live = E.argv_for("stamp", True, self.POL)
        self.assertIn("--apply", live)
        self.assertIn("50", live)

    def test_limit_defaults_to_zero(self):
        pol = json.loads(json.dumps(self.POL))
        del pol["classes"]["stamp"]["limit"]
        live = E.argv_for("stamp", True, pol)
        self.assertIn("0", live)
        self.assertNotIn("{limit}", " ".join(live))

    def test_summary_flattens_json(self):
        self.assertEqual(E.summarize_output('{"stamped": 3, "ts": "x", "by": {"a": 1}}', ""),
                         "stamped=3; by={a:1}")
        self.assertEqual(E.summarize_output("", "boom\nlast line"), "last line")
        self.assertEqual(E.summarize_output("", ""), "(no output)")


class TestEngine(Sandbox):
    def setUp(self):
        super().setUp()
        self.policy_file = self.root / "remediate_policy.json"
        self.office = self.home("steward")

    def write_policy(self, enabled=True, **classes):
        pol = {"enabled": enabled, "max_actions_per_run": 100, "classes": {
            "drift-debris": {"enabled": True, "sla_days": 7, "fleet_delegate": False},
            **classes}}
        self.policy_file.write_text(json.dumps(pol))

    def engine(self, notifier=None):
        return E.Engine(self.cfg, self.policy_file, notifier=notifier)

    def leftover(self):
        (self.office / "a.py").write_text("x\n")
        bak = self.office / "a.py.bak-1"
        bak.write_text("y\n")
        self.age(bak, 30)
        return bak

    def test_apply_refuses_when_policy_disabled(self):
        self.write_policy(enabled=False)
        bak = self.leftover()
        out = self.engine().apply()
        self.assertFalse(out["applied"])
        self.assertTrue(bak.exists())
        self.assertEqual(Ledger(self.cfg.ledger_dir).read_actions(), [])

    def test_plan_never_writes(self):
        self.write_policy()
        bak = self.leftover()
        out = self.engine().plan()
        self.assertTrue(bak.exists())
        self.assertFalse((self.cfg.state_dir / "remediation_state.json").exists())
        self.assertEqual(out["results"][0]["detail"]["actions"][0]["op"], "would-close")

    def test_apply_acts_reports_notifies_and_undo_restores(self):
        self.write_policy()
        bak = self.leftover()
        rec = _Recorder()
        out = self.engine(rec).apply()
        self.assertTrue(out["applied"])
        self.assertFalse(bak.exists())
        self.assertTrue(Path(out["report"]).exists())
        self.assertIn("a.py.bak-1", Path(out["report"]).read_text())
        self.assertEqual(len(rec.sent), 1)
        self.assertIn("drift-debris", rec.sent[0])
        state = sla.load_state(self.cfg.state_dir / "remediation_state.json")
        self.assertEqual(len(state["closed"]), 1)
        action = Ledger(self.cfg.ledger_dir).read_actions()[0]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = E.main(["--policy", str(self.policy_file), "undo", action["id"]])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertTrue(bak.exists())

    def test_no_notify_flag(self):
        self.write_policy()
        self.leftover()
        rec = _Recorder()
        self.engine(rec).apply(notify=False)
        self.assertEqual(rec.sent, [])

    def test_command_class_runs_with_policy_argv(self):
        marker = self.root / "ran.txt"
        script = self.root / "tool.py"
        script.write_text("import sys, json, pathlib\n"
                          "pathlib.Path(sys.argv[1]).write_text(' '.join(sys.argv[2:]))\n"
                          "print(json.dumps({'stamped': 2}))\n")
        self.write_policy(stamp={"enabled": True, "type": "command", "limit": 5, "cwd": "$ARC_ROOT",
                                 "dry": [sys.executable, str(script), str(marker), "dry"],
                                 "apply": [sys.executable, str(script), str(marker), "apply", "{limit}"]})
        out = self.engine(_Recorder()).apply(only={"stamp"})
        self.assertEqual(out["results"][0]["summary"], "stamped=2")
        self.assertEqual(marker.read_text(), "apply 5")
        self.engine().plan(only={"stamp"})
        self.assertEqual(marker.read_text(), "dry")

    def test_failing_command_is_reported_not_raised(self):
        self.write_policy(broken={"enabled": True, "type": "command", "dry": ["/nonexistent/bin/tool"],
                                  "apply": ["/nonexistent/bin/tool"]})
        out = self.engine(_Recorder()).apply(only={"broken"})
        self.assertFalse(out["results"][0]["ok"])

    def test_routes_and_status(self):
        self.write_policy()
        bak = self.leftover()
        self.age(bak, 2)  # inside SLA -> stays open
        self.engine(_Recorder()).apply()
        routes = self.engine().routes()
        self.assertEqual(routes["trend"]["open"], 1)
        self.assertFalse(routes["open"][0]["breached"])
        status = self.engine().status()
        self.assertEqual(status["self"], "STEWARD")
        self.assertIn("drift-debris", status["enabled_classes"])

    def test_cli_plan_json_and_status(self):
        self.write_policy()
        for argv in (["plan", "--json"], ["status", "--json"], ["routes", "--json"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = E.main(["--policy", str(self.policy_file), *argv])
            self.assertEqual(rc, 0)
            json.loads(buf.getvalue())

    def test_help_does_not_crash(self):
        for sub in (["plan", "-h"], ["apply", "-h"], ["status", "-h"], ["undo", "-h"]):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                E.build_parser().parse_args(sub)


if __name__ == "__main__":
    unittest.main()
