"""Index (NULL-not-zero, withheld composites, trajectory), card, and review rotation."""
import datetime as dt
import io
import json
import unittest
from contextlib import redirect_stdout

from arcturion_governance.scorecard import card, cli, collect, index, review, store
from tests.scorecard._synthetic import ScoreSandbox, Session, good_week, rough_week

W1, W2 = "2026-07-06", "2026-07-13"


class _Rec:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send(self, text, *, title="", priority="normal"):
        self.sent.append(text)
        return self.ok


class TestIndex(ScoreSandbox):
    def seed(self):
        good_week(f"{W1}T09:00:00+00:00", "g1").write(self.transcripts, "-a-steward")
        rough_week(f"{W1}T09:00:00+00:00", "r1").write(self.transcripts, "-a-builder")
        collect.collect(self.settings)

    def by_agent(self, week=W1):
        return {r["agent"]: r for r in index.compute_week(self.settings, week)}

    def test_scores_from_synthetic_weeks(self):
        self.seed()
        rows = self.by_agent()
        good, rough = rows["STEWARD"], rows["BUILDER"]
        self.assertEqual((good["economy"], good["precision"], good["speed"]), (100.0, 100.0, 100.0))
        self.assertAlmostEqual(good["recall"], 83.3)
        self.assertEqual(good["status"], "ok")
        self.assertEqual((rough["economy"], rough["precision"], rough["speed"], rough["recall"]),
                         (40.0, 40.0, 30.0, 0.0))
        self.assertEqual(rough["composite"], 27.5)

    def test_missing_measurement_is_null_not_zero(self):
        s = Session(f"{W1}T09:00:00+00:00", "chat")
        for i in range(4):
            s.user(f"Quick question number {i} about naming.").say("Here is an answer.", after=10)
        s.write(self.transcripts, "-a-research")
        collect.collect(self.settings)
        row = self.by_agent()["RESEARCH"]
        self.assertIsNone(row["precision"], "no tool calls: precision unknown, not 0 and not 100")
        self.assertEqual(row["recall"], 0.0, "zero lookups over real turns is a real zero")

    def test_thin_week_is_withheld_with_reasons(self):
        Session(f"{W1}T09:00:00+00:00", "thin").user("One small task.").tool("Bash").say("ok") \
            .write(self.transcripts, "-a-research")
        collect.collect(self.settings)
        row = self.by_agent()["RESEARCH"]
        self.assertIsNone(row["composite"])
        self.assertEqual(row["status"], "incomplete")
        self.assertTrue(any("turn" in r for r in row["reasons"]))

    def test_trajectory_against_prior_weeks(self):
        rough_week(f"{W1}T09:00:00+00:00", "w1").write(self.transcripts, "-a-builder")
        good_week(f"{W2}T09:00:00+00:00", "w2").write(self.transcripts, "-a-builder")
        collect.collect(self.settings)
        index.compute_week(self.settings, W1)
        row = self.by_agent(W2)["BUILDER"]
        self.assertAlmostEqual(row["trajectory"], row["composite"] - 27.5, places=1)
        self.assertIsNone(self.by_agent(W1)["BUILDER"]["trajectory"], "no earlier week, no trajectory")

    def test_recompute_is_idempotent(self):
        self.seed()
        first = self.by_agent()
        second = self.by_agent()
        for agent in first:
            self.assertEqual({k: v for k, v in first[agent].items()},
                             {k: v for k, v in second[agent].items()})
        self.assertEqual(len(index.load_week(self.settings, W1)), 2)

    def test_bad_week_rejected(self):
        with self.assertRaises(ValueError):
            index.compute_week(self.settings, "last week")


class TestCard(ScoreSandbox):
    def setUp(self):
        super().setUp()
        good_week(f"{W1}T09:00:00+00:00", "g1").write(self.transcripts, "-a-steward")
        rough_week(f"{W1}T09:00:00+00:00", "r1").write(self.transcripts, "-a-builder")
        Session(f"{W1}T09:00:00+00:00", "u").user("Stray session.").say("ok").write(self.transcripts, "-tmp-x")
        collect.collect(self.settings)
        index.compute_week(self.settings, W1)

    def test_render(self):
        text = card.render(index.load_week(self.settings, W1), W1)
        self.assertIn("week of 2026-07-06", text)
        self.assertIn("| STEWARD | 6 | 100 | 100 | 100 | 83 |", text)
        self.assertIn("withheld", text)
        self.assertIn("UNKNOWN", text)
        self.assertIn("never treated as zero", text)

    def test_send_and_fallback(self):
        rec = _Rec()
        out = card.send(self.settings, W1, notifier=rec)
        self.assertTrue(out["sent"])
        self.assertEqual(len(rec.sent), 1)
        out = card.send(self.settings, W1, notifier=_Rec(ok=False))
        self.assertFalse(out["sent"])
        self.assertIn("BUILDER", (self.settings.card_dir / f"card-{W1}.md").read_text())

    def test_empty_week(self):
        self.assertIn("No turns", card.render([], "2026-01-05"))


class TestReview(ScoreSandbox):
    def setUp(self):
        super().setUp()
        good_week(f"{W1}T09:00:00+00:00", "g1").write(self.transcripts, "-a-steward")
        rough_week(f"{W1}T09:00:00+00:00", "r1").write(self.transcripts, "-a-builder")
        collect.collect(self.settings)
        index.compute_week(self.settings, W1)

    def test_plan_spreads_the_roster_over_weekdays(self):
        p = review.plan(self.settings, W2)
        self.assertEqual(p, {"2026-07-13": ["BUILDER", "RESEARCH"], "2026-07-14": ["STEWARD"]})

    def test_weak_faculties_become_evidence_backed_mitigations(self):
        r = review.build(self.settings, "builder", W1)
        faculties = sorted(m["faculty"] for m in r["mitigations"])
        self.assertEqual(faculties, ["economy", "precision", "recall", "speed"])
        self.assertTrue(all("turn(s), week of 2026-07-06" in m["evidence"] for m in r["mitigations"]))
        self.assertEqual(review.build(self.settings, "steward", W1)["mitigations"], [])

    def test_no_data_and_incomplete(self):
        self.assertEqual(review.build(self.settings, "research", W1)["status"], "no-data")

    def test_inbox_block_is_replaced_not_duplicated(self):
        inbox = self.home("builder") / "inbox.md"
        inbox.write_text("# Builder inbox\n\nKeep this note.\n")
        rec = _Rec()
        for _ in range(2):
            review.run(self.settings, agent="builder", week=W1, notifier=rec)
        body = inbox.read_text()
        self.assertEqual(body.count("<!-- gov-review:BUILDER:2026-07-06 -->"), 1)
        self.assertIn("Keep this note.", body)
        self.assertIn("rework is high", body)
        self.assertEqual(len(rec.sent), 2)

    def test_dry_run_writes_nothing(self):
        out = review.run(self.settings, agent="builder", week=W1, dry=True, notifier=_Rec())
        self.assertIn("would have", out[0]["result"])
        self.assertFalse((self.home("builder") / "inbox.md").exists())

    def test_rotation_reviews_todays_agents_for_last_full_week(self):
        rec = _Rec()
        out = review.run(self.settings, today=dt.date(2026, 7, 13), notifier=rec)
        self.assertEqual([r["agent"] for r in out], ["BUILDER", "RESEARCH"])
        self.assertTrue(all(r["week"] == W1 for r in out))
        self.assertEqual(review.run(self.settings, today=dt.date(2026, 7, 18), notifier=rec), [])


class TestCli(ScoreSandbox):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def test_end_to_end(self):
        import os
        os.environ["GOVERNANCE_NOTIFY"] = "file"
        good_week(f"{W1}T09:00:00+00:00", "g1").write(self.transcripts, "-a-steward")
        rc, out = self.run_cli("collect", "--json")
        self.assertEqual((rc, json.loads(out)["turns"]), (0, 6))
        rc, out = self.run_cli("index", "--week", "2026-07-08")  # any day maps to its Monday
        self.assertIn("STEWARD", out)
        rc, out = self.run_cli("card", "--week", W1)
        self.assertIn("STEWARD", out)
        rc, out = self.run_cli("card", "--week", W1, "--send")
        self.assertEqual(out.strip(), "sent")
        rc, out = self.run_cli("review", "plan", "--week", W1)
        self.assertIn("2026-07-06: BUILDER, RESEARCH", out)
        rc, out = self.run_cli("review", "run", "--agent", "steward", "--week", W1, "--dry-run")
        self.assertIn("STEWARD: ok", out)
        rc, out = self.run_cli("weekly", "--week", W1)
        self.assertEqual(rc, 0)
        self.assertIn("Agent scorecard", (self.cfg.state_dir / "notices.log").read_text())

    def test_dispatcher_reaches_scorecard(self):
        from arcturion_governance import cli as top
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = top.main(["scorecard", "review", "plan", "--week", W1])
        self.assertEqual(rc, 0)
        self.assertIn("STEWARD", buf.getvalue())

    def test_default_week_is_last_full_week(self):
        self.assertEqual(store.last_full_week(dt.date(2026, 7, 15)), "2026-07-06")
        self.assertEqual(store.week_start(dt.date(2026, 7, 12)), "2026-07-06")


if __name__ == "__main__":
    unittest.main()
