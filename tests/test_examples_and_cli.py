"""Shipped examples and the `arcgov` dispatcher."""
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from arcturion_governance import cli

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


class TestPlistTemplates(unittest.TestCase):
    def templates(self):
        found = sorted((EXAMPLES / "launchd").glob("*.plist.template"))
        self.assertTrue(found)
        return found

    def test_templates_parse_and_use_placeholders_only(self):
        for path in self.templates():
            text = path.read_text()
            data = plistlib.loads(text.encode())
            self.assertTrue(data["Label"].startswith("com.example.arcturion-governance."), path.name)
            self.assertEqual(data["ProgramArguments"][:3], ["__PYTHON__", "-m", "arcturion_governance"])
            self.assertIn("__REPO_DIR__", text)
            self.assertNotIn(str(Path.home()), text)
            self.assertFalse(data.get("RunAtLoad"))

    def test_template_tools_exist(self):
        for path in self.templates():
            tool = plistlib.loads(path.read_bytes())["ProgramArguments"][3]
            self.assertIn(tool, cli.TOOLS)


class TestDispatcher(unittest.TestCase):
    def test_help_lists_every_tool(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main([]), 0)
        for tool in cli.TOOLS:
            self.assertIn(tool, buf.getvalue())

    def test_unknown_tool(self):
        with redirect_stdout(io.StringIO()):
            import contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["nope"]), 2)

    def test_every_tool_module_imports_and_has_main(self):
        import importlib
        for name, (module, _desc) in cli.TOOLS.items():
            mod = importlib.import_module(module)
            self.assertTrue(callable(getattr(mod, "main", None)), name)


class TestDemoWorkspace(unittest.TestCase):
    """Run the demo as CI does, in a temp copy so the repo is never written."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.demo = self.tmp / "demo"
        shutil.copytree(EXAMPLES / "demo", self.demo)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("ARC_", "GOVERNANCE_", "ADVISOR_"))}
        self.env.update(ARC_ROOT=str(self.demo), ARC_GOVERNANCE_CONFIG=str(self.demo / "governance.json"),
                        PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1", GOVERNANCE_NOTIFY="none")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def arcgov(self, *args):
        return subprocess.run([sys.executable, "-m", "arcturion_governance", *args], cwd=str(self.demo),
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_dry_commands_succeed_and_write_nothing(self):
        before = sorted(str(p.relative_to(self.demo)) for p in self.demo.rglob("*"))
        for args in (["remediate", "plan"], ["drift", "run"], ["drift", "report"],
                     ["journal", "due"], ["advisor", "--status"], ["remediate", "status"]):
            r = self.arcgov(*args)
            self.assertEqual(r.returncode, 0, f"{args}: {r.stderr}")
        after = sorted(str(p.relative_to(self.demo)) for p in self.demo.rglob("*"))
        self.assertEqual(before, after)

    def test_aged_apply_closes_delegates_and_undoes(self):
        for p in self.demo.rglob("*"):
            if p.is_file():
                os.utime(p, (1_767_225_600, 1_767_225_600))  # 2026-01-01
        r = self.arcgov("remediate", "apply", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        closed = sum(x["detail"]["closed"] for x in out["results"])
        self.assertEqual(closed, 4)
        self.assertFalse((self.demo / "agents/steward/04 Governance/Runbooks/deploy-checklist.md.orig").exists())
        self.assertTrue((self.demo / "agents/steward/04 Governance/Ledger/weekly-sweep.md").exists())
        self.assertTrue((self.demo / "agents/builder/build.sh.old").exists(), "peer files are never touched")
        self.assertIn("gov-sla:", (self.demo / "agents/builder/messages.md").read_text())
        actions = [json.loads(x) for f in (self.demo / "governance/ledger/actions").glob("*.jsonl")
                   for x in f.read_text().splitlines()]
        for a in actions:
            r = self.arcgov("remediate", "undo", a["id"])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.demo / "agents/steward/04 Governance/Runbooks/deploy-checklist.md.orig").exists())
        self.assertTrue((self.demo / "agents/steward/weekly-sweep.md").exists())
        backlog = (self.demo / "governance/drift-backlog.md").read_text()
        self.assertEqual(backlog.count("missing-created"), 3, "the rewrite was reversed")


if __name__ == "__main__":
    unittest.main()
