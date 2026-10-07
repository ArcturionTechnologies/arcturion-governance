"""drift-sentinel: aggregation, probes, the report writer, and the never-delete invariant."""
import datetime
import io
import json
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from arcturion_governance.drift import sentinel as ds
from arcturion_governance.remediate import hygiene
from tests._helpers import Sandbox

ENGINE_SRC = Path(ds.__file__)


class _Rec:
    def __init__(self):
        self.sent = []

    def send(self, text, *, title="", priority="normal"):
        self.sent.append((priority, text))
        return True


class TestAggregation(unittest.TestCase):
    def test_merge_two_findings(self):
        merged = ds.merge_findings(ds.finding("a", ds.G, "clean"), ds.finding("b", ds.Y, "3 flagged", count=3))
        self.assertEqual((merged["status"], merged["total"], len(merged["findings"])), (ds.Y, 3, 2))

    def test_red_dominates(self):
        merged = ds.merge_findings(ds.finding("x", ds.G, "ok"), ds.finding("y", ds.R, "bad", count=9),
                                   ds.finding("z", ds.Y, "meh", count=1))
        self.assertEqual((merged["status"], merged["total"]), (ds.R, 10))

    def test_merge_empty_is_green(self):
        self.assertEqual(ds.merge_findings()["status"], ds.G)

    def test_safe_json_tolerates_banner_lines(self):
        self.assertEqual(ds.safe_json('WARN something\n{"staged": 2}')["staged"], 2)
        self.assertIsNone(ds.safe_json("not json at all"))

    def test_count_from_shapes(self):
        self.assertEqual(ds.count_from([1, 2, 3], ()), 3)
        self.assertEqual(ds.count_from({"items": [1, 2]}, ("items",)), 2)
        self.assertEqual(ds.count_from({"stale_count": 7}, ("stale_count",)), 7)
        self.assertEqual(ds.count_from("x", ("a",)), 0)

    def test_labelled_count_ignores_unlabelled_words(self):
        text = "steward: 13 agents | link-actions: 2 | rule-audit flags: 3 | flags: 0 elsewhere"
        self.assertEqual(ds.labelled_count(text, [r"link-actions:\s*(\d+)", r"rule-audit flags:\s*(\d+)"]), 5)

    def test_status_thresholds(self):
        self.assertEqual(ds.status_for(0), ds.G)
        self.assertEqual(ds.status_for(1), ds.Y)
        self.assertEqual(ds.status_for(10, red_at=10), ds.R)
        self.assertEqual(ds.status_for(0, errored=True), ds.Y)

    def test_finding_shape_is_stable(self):
        self.assertEqual(set(ds.finding("e", ds.G, "d", count=2, data={"k": 1})),
                         {"engine", "status", "detail", "count", "data", "error"})


class TestProbes(Sandbox):
    def probe(self, **p):
        return ds.run_probe(self.cfg, p, datetime.date(2026, 7, 31))

    def test_command_probe_json_count(self):
        f = self.probe(name="checker", kind="command", count_keys=["issues"],
                       argv=[sys.executable, "-c", "import json; print(json.dumps({'issues': [1, 2]}))"])
        self.assertEqual((f["status"], f["count"]), (ds.Y, 2))

    def test_command_probe_labelled_count_and_rc(self):
        f = self.probe(name="steward", kind="command", count_patterns=[r"flags:\s*(\d+)"],
                       argv=[sys.executable, "-c", "print('rule flags: 4')"], red_at=3)
        self.assertEqual((f["status"], f["count"]), (ds.R, 4))
        f2 = self.probe(name="rc", kind="command", argv=[sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(f2["count"], 1)

    def test_broken_probe_is_amber_not_a_crash(self):
        f = self.probe(name="gone", kind="command", argv=["/nonexistent/checker"])
        self.assertEqual(f["status"], ds.Y)
        self.assertTrue(f["error"])
        self.assertEqual(self.probe(name="x", kind="nope")["status"], ds.Y)

    def test_stale_files_uses_frontmatter_date_then_mtime(self):
        h = self.home("builder")
        (h / "fresh.md").write_text("---\nupdated: 2026-07-20\n---\nx\n")
        (h / "old.md").write_text("---\nupdated: 2025-01-01\n---\nx\n")
        old_mtime = h / "nofm.md"
        old_mtime.write_text("x\n")
        self.age(old_mtime, 400)
        f = self.probe(name="stale", kind="stale-files", days=90)
        self.assertEqual(f["count"], 2)
        self.assertTrue(any(p.endswith("old.md") for p in f["data"]["stale"]))

    def test_missing_frontmatter(self):
        h = self.home("research")
        (h / "ok.md").write_text("---\ncreated: 2026-01-01\n---\nx\n")
        (h / "bad.md").write_text("no frontmatter\n")
        f = self.probe(name="fm", kind="missing-frontmatter", required=["created"],
                       paths=["$ARC_ROOT/agents/research"])
        self.assertEqual(f["count"], 1)

    def test_leftovers_probe(self):
        (self.home("builder") / "a.py.bak-1").write_text("x\n")
        self.assertEqual(self.probe(name="lo", kind="leftovers")["count"], 1)

    def test_default_probes_and_sections(self):
        sections = ds.run_sections(self.cfg)
        self.assertEqual(set(sections), {"scan", "memory"})
        only = ds.run_sections(self.cfg, ["scan"])
        self.assertEqual(set(only), {"scan"})


class TestReportWriter(Sandbox):
    def sections(self):
        return {"scan": ds.merge_findings(ds.finding("pulse", ds.Y, "2 urgent", count=2)),
                "memory": ds.merge_findings(ds.finding("staleness", ds.R, "120 stale", count=120)),
                "graph": ds.merge_findings(ds.finding("validate", ds.G, "clean"))}

    def test_apply_writes_report_and_queue_and_notifies_on_red(self):
        rec = _Rec()
        result = ds.report(self.cfg, self.sections(), apply=True, notifier=rec)
        self.assertTrue(result["applied"])
        self.assertTrue(Path(result["report"]).exists())
        self.assertEqual(result["queued_items"], 2)
        body = Path(result["report"]).read_text()
        self.assertIn("PROPOSAL", body)
        self.assertIn("diff before delete", body)
        self.assertTrue(rec.sent and rec.sent[0][0] == "high")

    def test_preview_writes_nothing(self):
        result = ds.report(self.cfg, self.sections(), apply=False, notifier=_Rec())
        self.assertFalse(Path(result["report"]).exists())
        self.assertFalse(Path(result["queue"]).exists())
        self.assertIn("Drift sweep", result["preview"])

    def test_queue_lines_feed_alarm_dedup(self):
        """The backlog the sentinel writes is exactly what alarm-dedup collapses."""
        for day in ("2026-07-01", "2026-07-02"):
            ds.report(self.cfg, self.sections(), apply=True, notifier=_Rec(),
                      now=datetime.datetime.fromisoformat(f"{day}T09:00:00+00:00"))
        text = hygiene.drift_queue_path(self.cfg).read_text()
        new, stats = hygiene.collapse_queue(text, "2026-07-31")
        self.assertEqual(stats["distinct"], 2)
        self.assertEqual(stats["collapsed_from"], 4)

    def test_green_sweep_does_not_notify(self):
        rec = _Rec()
        ds.report(self.cfg, {"scan": ds.merge_findings(ds.finding("a", ds.G, "ok"))}, apply=True, notifier=rec)
        self.assertEqual(rec.sent, [])


class TestCli(Sandbox):
    def test_help_does_not_crash(self):
        for argv in (["--help"], ["run", "--help"], ["report", "--help"], ["probes", "--help"]):
            buf = io.StringIO()
            with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
                ds.main(argv)
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("usage", buf.getvalue().lower())

    def test_run_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ds.main(["run", "--json"])
        self.assertEqual(rc, 0)
        self.assertIn("scan", json.loads(buf.getvalue()))

    def test_configured_probes_replace_defaults(self):
        raw = json.loads(self.cfg_path.read_text())
        raw["drift"] = {"probes": [{"name": "only-one", "kind": "leftovers", "section": "scan"}]}
        self.cfg_path.write_text(json.dumps(raw))
        buf = io.StringIO()
        with redirect_stdout(buf):
            ds.main(["probes"])
        self.assertIn("only-one", buf.getvalue())
        self.assertNotIn("stale-notes", buf.getvalue())


class TestNeverDeleteInvariant(unittest.TestCase):
    def test_engine_has_no_delete_path(self):
        src = ENGINE_SRC.read_text(encoding="utf-8")
        for pat in (r"\bos\.remove\b", r"\bos\.unlink\b", r"\bos\.rmdir\b", r"\.unlink\s*\(",
                    r"\bshutil\.rmtree\b", r"\bos\.replace\b", r"\.rename\s*\("):
            self.assertIsNone(re.search(pat, src), pat)


if __name__ == "__main__":
    unittest.main()
