"""Collector: turn boundaries, follow-up classification, attribution, idempotency, privacy."""
import json
import sqlite3
import unittest

from arcturion_governance.scorecard import collect, store
from tests.scorecard._synthetic import ScoreSandbox, Session, good_week, rough_week


class TestFollowupClassification(unittest.TestCase):
    def test_labels(self):
        c = collect.classify_followup
        self.assertEqual(c("Add a retry to the sync job", "That's not what I asked for"), "correction")
        self.assertEqual(c("Fix the import", "It still doesn't work"), "correction")
        self.assertEqual(c("Fix the import", "No, the other file"), "correction")
        self.assertEqual(c("Fix the import error in the loader", "Fix the import error in the loader!"),
                         "failed_repeat")
        self.assertEqual(c("Explain the plan", "What do you mean by staged?"), "clarification")
        self.assertEqual(c("Fix the import", "Great. Now update the docs."), "new_request")

    def test_words_inside_other_words_do_not_count(self):
        self.assertEqual(collect.classify_followup("x", "Nothing else, nobody needs a wrongful rename"),
                         "new_request")


class TestCollect(ScoreSandbox):
    def rows(self):
        conn = sqlite3.connect(str(self.settings.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM turns ORDER BY key").fetchall()
        finally:
            conn.close()

    def test_tool_results_are_not_turn_boundaries(self):
        rough_week("2026-07-06T09:00:00+00:00", "rough").write(self.transcripts, "-work-agents-builder")
        out = collect.collect(self.settings)
        self.assertEqual(out["turns"], 6)
        first = self.rows()[0]
        self.assertEqual((first["tool_calls"], first["tool_errors"]), (2, 2))
        self.assertEqual(first["followup"], "correction")
        self.assertEqual(first["first_response_s"], 200)

    def test_followups_and_unknown_last_turn(self):
        rough_week("2026-07-06T09:00:00+00:00", "rough").write(self.transcripts, "-work-agents-builder")
        collect.collect(self.settings)
        followups = [r["followup"] for r in self.rows()]
        self.assertEqual(followups, ["correction", "correction", "failed_repeat", "clarification",
                                     "new_request", None])
        self.assertIsNone(self.rows()[-1]["rework"], "no follow-up yet means unknown, not zero")

    def test_rerun_is_idempotent(self):
        good_week("2026-07-06T09:00:00+00:00", "good").write(self.transcripts, "-work-agents-steward")
        collect.collect(self.settings)
        collect.collect(self.settings)
        self.assertEqual(len(self.rows()), 6)

    def test_attribution(self):
        s = store.Settings(self.cfg)
        self.assertEqual(collect.derive_agent("-home-me-agents-builder", s), "BUILDER")
        self.assertEqual(collect.derive_agent("-home-me-research-tools-builder", s), "BUILDER",
                         "the last (most specific) roster token wins")
        self.assertEqual(collect.derive_agent("-srv-legacy-helper-x", s), "BUILDER", "project_map")
        self.assertEqual(collect.derive_agent("-home-me-oldbot", s), "RESEARCH", "retired -> successor")
        self.assertEqual(collect.derive_agent("-tmp-scratch", s), "UNKNOWN")

    def test_unknown_sessions_are_kept_not_dropped(self):
        good_week("2026-07-06T09:00:00+00:00", "u").write(self.transcripts, "-tmp-scratch")
        out = collect.collect(self.settings)
        self.assertEqual(out["by_agent"], {"UNKNOWN": 6})

    def test_since_filter_and_corrupt_lines(self):
        path = good_week("2026-07-06T09:00:00+00:00", "g").write(self.transcripts, "-a-steward")
        with path.open("a") as fh:
            fh.write("{not json\n")
        Session("2026-06-01T09:00:00+00:00", "old").user("An old request about backups.").say("ok") \
            .write(self.transcripts, "-a-steward")
        out = collect.collect(self.settings, since=store.parse_ts("2026-07-01T00:00:00Z").date())
        self.assertEqual(out["turns"], 6)

    def test_no_message_text_is_stored(self):
        rough_week("2026-07-06T09:00:00+00:00", "rough").write(self.transcripts, "-work-agents-builder")
        collect.collect(self.settings)
        blob = json.dumps([dict(r) for r in self.rows()])
        self.assertNotIn("config loader", blob)
        self.assertNotIn("alias", blob)

    def test_idle_gaps_are_capped(self):
        s = Session("2026-07-06T09:00:00+00:00", "idle").user("Start the migration plan.")
        s.say("Starting.", after=5).say("Back after lunch.", after=5000)
        s.write(self.transcripts, "-a-steward")
        collect.collect(self.settings)
        self.assertEqual(self.rows()[0]["active_s"], 5 + self.settings.idle_cap_s)

    def test_missing_transcript_folder_is_fine(self):
        self.settings.transcript_dirs = [self.root / "nope"]
        self.assertEqual(collect.collect(self.settings)["files"], 0)


if __name__ == "__main__":
    unittest.main()
