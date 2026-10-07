"""Decision journal: append-only, backward compatible with other row types."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from arcturion_governance import journal
from tests._helpers import Sandbox

OTHER_ROWS = [
    {"type": "agent_approval", "id": "appr_0001", "title": "old approval", "agent": "builder",
     "decision": "deny", "ts": "2026-04-19T15:06:24+00:00"},
    {"type": "approval", "id": "appr_0002", "title": "Paper experiment", "agent": "research",
     "decision": "denied", "ts": "2026-04-20T13:23:23+00:00"},
]


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.log = self.tmp / "decisions-log.jsonl"
        self.log.write_text("".join(json.dumps(r) + "\n" for r in OTHER_ROWS))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def test_log_decision_preserves_other_rows(self):
        d = journal.log_decision(self.log, "Retire the nightly export job",
                                 expected_outcome="no user impact; one less failure source",
                                 review_date="2026-08-19")
        rows = self.read()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["type"], "agent_approval")
        self.assertEqual((rows[-1]["type"], rows[-1]["id"], rows[-1]["review_date"]),
                         ("decision", d["id"], "2026-08-19"))

    def test_due_lists_only_unretroed_past_review_dates(self):
        d1 = journal.log_decision(self.log, "A", "out A", review_date="2026-01-01")
        journal.log_decision(self.log, "B", "out B", review_date="2099-01-01")
        journal.log_decision(self.log, "C, no review")
        self.assertEqual([r["id"] for r in journal.due(self.log, today="2026-07-19")], [d1["id"]])
        journal.retro(self.log, d1["id"], actual="worked", verdict="good-call")
        self.assertEqual(journal.due(self.log, today="2026-07-19"), [])

    def test_retro_is_an_append_only_row(self):
        d = journal.log_decision(self.log, "A", "expect X", review_date="2026-01-01")
        journal.retro(self.log, d["id"], actual="got Y", verdict="bad-call")
        rows = self.read()
        self.assertEqual((rows[-1]["type"], rows[-1]["decision_id"]), ("retro", d["id"]))
        dec = [r for r in rows if r.get("id") == d["id"] and r["type"] == "decision"]
        self.assertNotIn("actual", dec[0])

    def test_retro_unknown_id_or_bad_verdict_raises(self):
        with self.assertRaises(KeyError):
            journal.retro(self.log, "nope", actual="x", verdict="good-call")
        d = journal.log_decision(self.log, "A")
        with self.assertRaises(ValueError):
            journal.retro(self.log, d["id"], actual="x", verdict="great")

    def test_bad_review_date_and_empty_decision_rejected(self):
        with self.assertRaises(ValueError):
            journal.log_decision(self.log, "A", review_date="next tuesday")
        with self.assertRaises(ValueError):
            journal.log_decision(self.log, "  ")

    def test_calibration(self):
        for verdict in ("good-call", "good-call", "bad-call"):
            d = journal.log_decision(self.log, "x")
            journal.retro(self.log, d["id"], "y", verdict)
        stats = journal.calibration(self.log)
        self.assertEqual((stats["total"], stats["good-call"], stats["good_call_rate"]), (3, 2, 0.667))

    def test_corrupt_lines_are_skipped(self):
        with self.log.open("a") as fh:
            fh.write("{broken\n")
        self.assertEqual(len(journal.rows(self.log)), 2)


class TestJournalCli(Sandbox):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = journal.main(list(argv))
        return rc, buf.getvalue()

    def test_default_log_lives_in_state_dir(self):
        rc, out = self.run_cli("log", "Ship it", "--expect", "fine", "--review", "2026-01-01")
        self.assertEqual(rc, 0)
        self.assertTrue((self.cfg.state_dir / "decisions-log.jsonl").exists())
        rc, out = self.run_cli("due")
        self.assertIn("Ship it", out)

    def test_env_override_and_notify(self):
        log = self.root / "elsewhere.jsonl"
        os.environ["ARC_DECISION_LOG"] = str(log)
        os.environ["GOVERNANCE_NOTIFY"] = "file"
        self.run_cli("log", "Ship it", "--review", "2026-01-01")
        self.run_cli("due", "--notify")
        self.assertTrue(log.exists())
        notices = (self.cfg.state_dir / "notices.log").read_text()
        self.assertIn("1 decision retro(s) due", notices)

    def test_cli_errors_return_nonzero(self):
        rc, _ = self.run_cli("log", "x", "--review", "not-a-date")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
