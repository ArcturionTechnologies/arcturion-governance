"""SLA router: a finding past its SLA never simply stays open."""
import shutil
import tempfile
import unittest
from pathlib import Path

from arcturion_governance.remediate import sla

POLICY = {"classes": {"drift-debris": {"sla_days": 7}, "alarm-dedup": {"sla_days": 3}}}
ME = "STEWARD"


def _state():
    return sla.empty_state()


def route(rec, auto_ok, owner, today="2026-07-31"):
    return sla.route("drift-debris", rec, POLICY, auto_ok=auto_ok, owner=owner,
                     self_name=ME, human_name="OPERATOR", today=today)


class TestOwnerAndSla(unittest.TestCase):
    def test_every_finding_carries_an_owner_and_a_clock(self):
        rec = sla.observe(_state(), "drift-debris", "/x/y.bak", owner=ME,
                          first_seen="2026-07-01", today="2026-07-10")
        self.assertEqual(rec["owner"], ME)
        self.assertEqual(sla.age_days(rec, "2026-07-10"), 9)
        self.assertGreater(sla.sla_for("drift-debris", POLICY), 0)

    def test_first_seen_is_seeded_from_evidence(self):
        rec = sla.observe(_state(), "drift-debris", "/x/y.bak", owner=ME,
                          first_seen="2026-06-01", today="2026-07-31")
        self.assertEqual(sla.age_days(rec, "2026-07-31"), 60)

    def test_earlier_evidence_wins_on_re_observation(self):
        st = _state()
        sla.observe(st, "drift-debris", "/x/y.bak", owner=ME, first_seen="2026-07-01", today="2026-07-10")
        rec = sla.observe(st, "drift-debris", "/x/y.bak", owner=ME, first_seen="2026-05-01", today="2026-07-11")
        self.assertEqual(rec["first_seen"], "2026-05-01")
        self.assertEqual(rec["occurrences"], 2)

    def test_same_day_re_observation_does_not_inflate_occurrences(self):
        st = _state()
        for _ in range(3):
            rec = sla.observe(st, "drift-debris", "k", owner=ME, today="2026-07-10")
        self.assertEqual(rec["occurrences"], 1)

    def test_unknown_class_gets_a_default_sla(self):
        self.assertEqual(sla.sla_for("custom", {}), 7)


class TestRouting(unittest.TestCase):
    def _rec(self, first, owner=ME):
        return sla.observe(_state(), "drift-debris", "/x/y.bak", owner=owner,
                           first_seen=first, today="2026-07-31")

    def test_inside_sla_is_open(self):
        self.assertEqual(route(self._rec("2026-07-29"), True, ME), sla.OPEN)

    def test_breached_reversible_and_ours_auto_closes(self):
        self.assertEqual(route(self._rec("2026-06-01"), True, ME), sla.AUTO)

    def test_breached_in_another_agents_area_delegates(self):
        self.assertEqual(route(self._rec("2026-06-01", "BUILDER"), False, "BUILDER"), sla.DELEGATE)

    def test_breached_and_not_reversible_goes_to_a_human(self):
        self.assertEqual(route(self._rec("2026-06-01"), False, ME), sla.HUMAN)

    def test_owner_named_as_the_human_goes_to_the_human(self):
        self.assertEqual(route(self._rec("2026-06-01", "OPERATOR"), False, "OPERATOR"), sla.HUMAN)

    def test_a_breached_finding_never_stays_open(self):
        rec = self._rec("2026-01-01")
        for auto_ok in (True, False):
            for owner in (ME, "BUILDER", "OPERATOR", ""):
                r = route(rec, auto_ok, owner)
                self.assertIn(r, (sla.AUTO, sla.DELEGATE, sla.HUMAN))

    def test_age_alone_never_unlocks_an_auto_close(self):
        self.assertEqual(route(self._rec("2020-01-01"), False, ME), sla.HUMAN)


class TestDelegationIsIdempotent(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.inbox = self.tmp / "BUILDER" / "inbox.md"
        self.inbox.parent.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, today):
        return sla.observe(_state(), "drift-debris", "BUILDER::drift-debris", owner="BUILDER",
                           detail="3 leftovers in your area", first_seen="2026-06-01", today=today)

    def test_repeat_delegation_updates_the_same_line(self):
        for day in ("2026-07-20", "2026-07-21", "2026-07-22"):
            sla.write_delegation(self.inbox, self._rec(day), due="2026-07-27", sender=ME, today=day)
        lines = [ln for ln in self.inbox.read_text().splitlines() if "gov-sla:" in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn("age 51 days", lines[0])

    def test_the_fingerprint_marker_survives(self):
        rec = self._rec("2026-07-20")
        sla.write_delegation(self.inbox, rec, due="2026-07-27", sender=ME, today="2026-07-20")
        body = self.inbox.read_text()
        self.assertIn(f"gov-sla:{rec['fp']}", body)
        self.assertIn("due 2026-07-27", body)
        self.assertIn("from STEWARD", body)

    def test_existing_inbox_content_is_preserved(self):
        self.inbox.write_text("# Inbox\n\nExisting note from the operator.\n")
        sla.write_delegation(self.inbox, self._rec("2026-07-20"), due="2026-07-27", sender=ME, today="2026-07-20")
        self.assertIn("Existing note from the operator.", self.inbox.read_text())

    def test_dry_run_writes_nothing(self):
        ok, msg = sla.write_delegation(self.inbox, self._rec("2026-07-20"), due="2026-07-27",
                                       sender=ME, today="2026-07-20", dry=True)
        self.assertTrue(ok)
        self.assertFalse(self.inbox.exists())

    def test_missing_agent_home_is_reported_not_created(self):
        ghost = self.tmp / "GHOST" / "inbox.md"
        ok, msg = sla.write_delegation(ghost, self._rec("2026-07-20"), due="2026-07-27", sender=ME)
        self.assertFalse(ok)
        self.assertFalse(ghost.parent.exists())


class TestClosureAccounting(unittest.TestCase):
    def test_delegation_does_not_count_as_closure(self):
        st = _state()
        rec = sla.observe(st, "drift-debris", "k", owner="BUILDER", today="2026-07-31")
        sla.mark_delegated(rec, "BUILDER", "2026-08-07", "2026-07-31")
        t = sla.trend(st, "2026-07-31")
        self.assertEqual((t["open"], t["closed_total"]), (1, 0))

    def test_reap_closes_only_what_a_scanned_class_stopped_seeing(self):
        st = _state()
        sla.observe(st, "drift-debris", "gone", owner=ME, today="2026-07-30")
        sla.observe(st, "drift-debris", "still", owner=ME, today="2026-07-31")
        sla.observe(st, "alarm-dedup", "unscanned", owner=ME, today="2026-07-30")
        self.assertEqual(sla.reap(st, {"drift-debris"}, "2026-07-31"), 1)
        self.assertEqual({r["key"] for r in st["findings"].values()}, {"still", "unscanned"})

    def test_trend_reports_the_delta(self):
        st = _state()
        sla.observe(st, "drift-debris", "a", owner=ME, today="2026-07-30")
        sla.observe(st, "drift-debris", "b", owner=ME, today="2026-07-30")
        sla.trend(st, "2026-07-30")
        sla.close(st, sla.fingerprint("drift-debris", "a"), sla.AUTO, today="2026-07-31")
        t = sla.trend(st, "2026-07-31")
        self.assertEqual((t["open"], t["delta_open"], t["closed_today"], t["closure_rate"]), (1, -1, 1, 0.5))

    def test_state_roundtrip_and_corrupt_state(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        try:
            st = _state()
            sla.observe(st, "drift-debris", "a", owner=ME, today="2026-07-30")
            sla.save_state(st, tmp / "s" / "state.json")
            self.assertEqual(len(sla.load_state(tmp / "s" / "state.json")["findings"]), 1)
            (tmp / "bad.json").write_text("{nope")
            self.assertEqual(sla.load_state(tmp / "bad.json"), sla.empty_state())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFingerprint(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(sla.fingerprint("c", "k"), sla.fingerprint("c", "k"))
        self.assertNotEqual(sla.fingerprint("c", "k"), sla.fingerprint("d", "k"))


if __name__ == "__main__":
    unittest.main()
