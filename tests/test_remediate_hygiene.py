"""Hygiene classes, including the destructive paths. Temp trees only."""
import datetime
import os
import unittest
from pathlib import Path

from arcturion_governance.remediate import hygiene, sla
from arcturion_governance.remediate.ledger import Ledger
from tests._helpers import Sandbox


def _policy(**over):
    base = {"enabled": True, "max_actions_per_run": 500, "classes": {
        "drift-debris": {"enabled": True, "sla_days": 7, "limit": 100,
                         "fleet_delegate": False, "no_counterpart_action": "delete"},
        "stray-root": {"enabled": True, "sla_days": 7, "fleet_delegate": False},
        "alarm-dedup": {"enabled": True, "sla_days": 3},
    }}
    for k, v in over.items():
        if k in ("read_only_prefixes", "protected_names", "owned_roots"):
            base[k] = v
        else:
            base["classes"][k.replace("_", "-")].update(v)
    return base


class _H(Sandbox):
    def ctx(self, policy=None, *, dry=False, today=None):
        return hygiene.Ctx(self.cfg, policy or _policy(), sla.empty_state(), dry=dry,
                           today=today or datetime.date.today().isoformat())

    @property
    def office(self) -> Path:
        return self.home("steward")

    def actions(self):
        return Ledger(self.cfg.ledger_dir).read_actions()

    def undo(self, action_id):
        return Ledger(self.cfg.ledger_dir).undo(action_id)


class TestDriftDebrisDiffers(_H):
    def setUp(self):
        super().setUp()
        self.live = self.office / "engine.py"
        self.live.write_text("alpha\nbeta\ngamma\n")
        self.bak = self.office / "engine.py.bak-migration-20260601"
        self.bak.write_text("alpha\nBETA-OLD\ngamma\ndelta\n")
        self.age(self.bak, 40)

    def test_deletes_but_records_the_diff_first(self):
        res = hygiene.c_drift_debris(self.ctx())
        self.assertFalse(self.bak.exists())
        self.assertTrue(self.live.exists())
        self.assertEqual(res["closed"], 1)
        act = next(a for a in self.actions() if a["src"] == str(self.bak))
        self.assertEqual((act["op"], act["diff_kind"]), ("delete", "unified"))
        self.assertGreater(act["diff_lines"], 0)
        diff = Path(act["diff_path"]).read_text()
        self.assertIn("BETA-OLD", diff)
        self.assertIn("+delta", diff)

    def test_no_stray_copy_is_left_behind(self):
        hygiene.c_drift_debris(self.ctx())
        self.assertEqual([p.name for p in self.office.rglob("*") if p.is_file()], ["engine.py"])

    def test_undo_reconstructs_the_deleted_file(self):
        hygiene.c_drift_debris(self.ctx())
        act = next(a for a in self.actions() if a["op"] == "delete")
        ok, msg = self.undo(act["id"])
        self.assertTrue(ok, msg)
        self.assertEqual(self.bak.read_text(), "alpha\nBETA-OLD\ngamma\ndelta\n")

    def test_inside_sla_it_is_not_touched(self):
        self.age(self.bak, 2)
        res = hygiene.c_drift_debris(self.ctx())
        self.assertTrue(self.bak.exists())
        self.assertEqual((res["closed"], res["open"]), (0, 1))

    def test_dry_run_changes_nothing(self):
        res = hygiene.c_drift_debris(self.ctx(dry=True))
        self.assertTrue(self.bak.exists())
        self.assertEqual(res["actions"][0]["op"], "would-close")
        self.assertEqual(self.actions(), [])


class TestDriftDebrisIdentical(_H):
    def test_identical_leftover_restores_from_live(self):
        live = self.office / "conf.json"
        live.write_text('{"a": 1}\n')
        bak = self.office / "conf.json.bak-20260601"
        bak.write_text('{"a": 1}\n')
        self.age(bak, 30)
        hygiene.c_drift_debris(self.ctx())
        self.assertFalse(bak.exists())
        act = next(a for a in self.actions() if a["src"] == str(bak))
        self.assertEqual((act["diff_kind"], act["diff_lines"]), ("identical", 0))
        ok, msg = self.undo(act["id"])
        self.assertTrue(ok, msg)
        self.assertEqual(bak.read_text(), '{"a": 1}\n')

    def test_other_leftover_suffixes_are_recognised(self):
        for name, live in (("notes.md.orig", "notes.md"), ("todo.txt~", "todo.txt"),
                           ("run.sh.old", "run.sh"), ("cache.tmp", "cache")):
            self.assertEqual(hygiene.live_counterpart(self.office / name).name, live)
        self.assertIsNone(hygiene.live_counterpart(self.office / "plain.md"))


class TestDriftDebrisNoCounterpart(_H):
    def setUp(self):
        super().setUp()
        self.orphan = self.office / "vanished.py.bak-oldsprint-20260501"
        self.orphan.write_text("answer = 42\nkeep_me = True\n")
        self.age(self.orphan, 60)

    def test_default_deletes_with_the_full_preimage_in_the_ledger(self):
        res = hygiene.c_drift_debris(self.ctx())
        self.assertFalse(self.orphan.exists())
        self.assertEqual(res["closed"], 1)
        act = next(a for a in self.actions() if a["src"] == str(self.orphan))
        self.assertEqual(act["diff_kind"], "full")
        self.assertIsNone(act["live"])
        self.assertIn("answer = 42", Path(act["diff_path"]).read_text())
        ok, msg = self.undo(act["id"])
        self.assertTrue(ok, msg)
        self.assertEqual(self.orphan.read_text(), "answer = 42\nkeep_me = True\n")

    def test_policy_can_hold_orphans_for_a_human(self):
        res = hygiene.c_drift_debris(self.ctx(_policy(drift_debris={"no_counterpart_action": "human"})))
        self.assertTrue(self.orphan.exists())
        self.assertEqual(res["closed"], 0)
        self.assertIn("no live counterpart", res["human"][0]["reason"])

    def test_binary_orphan_is_never_auto_deleted(self):
        blob = self.office / "image.png.bak-20260501"
        blob.write_bytes(b"\x89PNG\x00\x01\x02binary")
        self.age(blob, 60)
        res = hygiene.c_drift_debris(self.ctx())
        self.assertTrue(blob.exists())
        self.assertIn("binary", " ".join(f["reason"] for f in res["human"]))


class TestDanglingSymlink(_H):
    def test_dangling_symlink_is_removed_and_ledgered(self):
        link = self.office / "points-nowhere"
        link.symlink_to(self.office / "gone.txt")
        self.age(link, 30)
        res = hygiene.c_drift_debris(self.ctx())
        self.assertFalse(link.is_symlink())
        self.assertEqual(res["closed"], 1)
        act = self.actions()[0]
        self.assertEqual(act["diff_kind"], "symlink")
        self.assertIn("ln -s", act["restore"])
        ok, msg = self.undo(act["id"])
        self.assertFalse(ok)
        self.assertIn("by hand", msg)


class TestDebrisDirectory(_H):
    def test_directory_with_live_counterpart_is_diffed_then_removed(self):
        live = self.office / "templates"
        (live).mkdir()
        (live / "a.md").write_text("one\n")
        old = self.office / "templates_old"
        old.mkdir()
        (old / "a.md").write_text("one-old\n")
        self.age(old, 30)
        res = hygiene.c_drift_debris(self.ctx())
        self.assertFalse(old.exists())
        self.assertEqual(res["closed"], 1)
        act = next(a for a in self.actions() if a["op"] == "delete-tree")
        self.assertIn("one-old", Path(act["diff_path"]).read_text())
        ok, msg = self.undo(act["id"])
        self.assertFalse(ok)
        self.assertIn("manual", msg)

    def test_directory_without_counterpart_goes_to_a_human(self):
        old = self.office / "drafts_old"
        old.mkdir()
        (old / "x.md").write_text("x\n")
        self.age(old, 30)
        res = hygiene.c_drift_debris(self.ctx())
        self.assertTrue(old.exists())
        self.assertIn("judgment call", res["human"][0]["reason"])


class TestNeverAutoOutranksAge(_H):
    def test_read_only_prefix(self):
        ro = self.office / "canon"
        ro.mkdir()
        bak = ro / "rules.md.bak-20260101"
        bak.write_text("x\n")
        self.age(bak, 900)
        res = hygiene.c_drift_debris(self.ctx(_policy(read_only_prefixes=["$ARC_ROOT/agents/steward/canon"])))
        self.assertTrue(bak.exists())
        self.assertEqual(res["closed"], 0)
        self.assertTrue(any("read-only" in f["reason"] for f in res["human"]))

    def test_protected_name(self):
        notes = self.office / "Private Notes"
        notes.mkdir()
        bak = notes / "n.md.bak-20260101"
        bak.write_text("x\n")
        self.age(bak, 900)
        res = hygiene.c_drift_debris(self.ctx(_policy(protected_names=["Private Notes"])))
        self.assertTrue(bak.exists())
        self.assertTrue(any("protected" in f["reason"] for f in res["human"]))


class TestFleetDelegation(_H):
    def test_other_agents_leftovers_become_one_aged_inbox_line(self):
        for i in range(3):
            p = self.home("builder") / f"f{i}.py.bak-1"
            p.write_text("x\n")
            self.age(p, 30)
        res = hygiene.c_drift_debris(self.ctx(_policy(drift_debris={"fleet_delegate": True})))
        self.assertEqual([d["agent"] for d in res["delegated"]], ["BUILDER"])
        inbox = (self.home("builder") / "inbox.md").read_text()
        self.assertEqual(inbox.count("gov-sla:"), 1)
        self.assertIn("3 leftover file(s)", inbox)
        self.assertTrue(all(p.exists() for p in self.home("builder").glob("*.bak-1")),
                        "another agent's files are never touched")

    def test_rerun_ages_the_same_line(self):
        p = self.home("builder") / "f.py.bak-1"
        p.write_text("x\n")
        self.age(p, 30)
        pol = _policy(drift_debris={"fleet_delegate": True})
        hygiene.c_drift_debris(self.ctx(pol))
        hygiene.c_drift_debris(self.ctx(pol))
        self.assertEqual((self.home("builder") / "inbox.md").read_text().count("gov-sla:"), 1)


# ── alarm-dedup ──────────────────────────────────────────────────────────────
QUEUE_HEAD = "# Drift backlog\n\n> Append-only.\n"


def _sweep(day, *rows):
    return f"\n## {day} sweep\n" + "".join(f"- [ ] 🟡 {day} `{e}` — {d}\n" for e, d in rows)


class TestAlarmAges(_H):
    def setUp(self):
        super().setUp()
        self.q = hygiene.drift_queue_path(self.cfg)
        self.q.parent.mkdir(parents=True, exist_ok=True)
        self.inbox = self.office / "inbox.md"

    def open_items(self):
        return [ln for ln in self.q.read_text().splitlines() if ln.startswith("- [ ]")]

    def test_repeated_alarm_becomes_one_item_that_ages(self):
        self.q.write_text(QUEUE_HEAD
                          + _sweep("2026-07-01", ("pulse", "items=12 critical=7"))
                          + _sweep("2026-07-02", ("pulse", "items=12 critical=2"))
                          + _sweep("2026-07-03", ("pulse", "items=12 critical=4")))
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        items = self.open_items()
        self.assertEqual(len(items), 1)
        self.assertIn("first seen **2026-07-01**", items[0])
        self.assertIn("age 30 days", items[0])
        self.assertIn("(3 sighting(s))", items[0])

    def test_a_number_change_does_not_mint_a_new_item(self):
        self.q.write_text(QUEUE_HEAD + "".join(
            _sweep(f"2026-07-{d:02d}", ("steward", f"rules: 13 agents | flags: {d}")) for d in range(1, 11)))
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        items = self.open_items()
        self.assertEqual(len(items), 1)
        self.assertIn("(10 sighting(s))", items[0])

    def test_second_run_ages_instead_of_recounting(self):
        self.q.write_text(QUEUE_HEAD + _sweep("2026-07-01", ("pulse", "items=12")))
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-20"))
        self.assertIn("age 19 days", self.open_items()[0])
        self.q.write_text(self.q.read_text() + _sweep("2026-07-21", ("pulse", "items=14")))
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-21"))
        items = self.open_items()
        self.assertEqual(len(items), 1)
        self.assertIn("first seen **2026-07-01**", items[0])
        self.assertIn("age 20 days", items[0])
        self.assertIn("(2 sighting(s))", items[0])

    def test_running_twice_on_the_same_day_changes_nothing(self):
        self.q.write_text(QUEUE_HEAD + _sweep("2026-07-01", ("pulse", "items=12"))
                          + _sweep("2026-07-02", ("pulse", "items=13")))
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        once = self.q.read_text()
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        self.assertEqual(once, self.q.read_text())
        self.assertEqual(once.count("aged, never recounted"), 1)

    def test_repeated_runs_do_not_inflate_the_sighting_count(self):
        self.q.write_text(QUEUE_HEAD + _sweep("2026-07-01", ("pulse", "items=12"))
                          + _sweep("2026-07-02", ("pulse", "items=13")))
        for _ in range(4):
            hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        self.assertIn("(2 sighting(s))", self.open_items()[0])

    def test_distinct_alarms_are_not_merged(self):
        self.q.write_text(QUEUE_HEAD + _sweep("2026-07-01", ("pulse", "items=12"), ("validate", "errors=3")))
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        self.assertEqual(len(self.open_items()), 2)

    def test_closed_items_are_preserved(self):
        self.q.write_text(QUEUE_HEAD + "- [x] 🟢 2026-07-01 `done` — resolved\n"
                          + _sweep("2026-07-02", ("pulse", "items=12")))
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        self.assertIn("- [x] 🟢 2026-07-01 `done` — resolved", self.q.read_text())

    def test_the_rewrite_is_reversible(self):
        self.q.write_text(QUEUE_HEAD + _sweep("2026-07-01", ("pulse", "items=12"))
                          + _sweep("2026-07-02", ("pulse", "items=12")))
        before = self.q.read_text()
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        act = next(a for a in self.actions() if a["op"] == "rewrite")
        ok, msg = self.undo(act["id"])
        self.assertTrue(ok, msg)
        self.assertEqual(self.q.read_text(), before)

    def test_inside_sla_the_queue_is_left_alone(self):
        self.q.write_text(QUEUE_HEAD + _sweep("2026-07-30", ("pulse", "items=12"))
                          + _sweep("2026-07-31", ("pulse", "items=12")))
        res = hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        self.assertEqual(res["open"], 1)
        self.assertEqual(len(self.open_items()), 2)

    def test_inbox_absorbs_a_hand_written_aged_line(self):
        self.inbox.write_text(
            "## 🔔 Aged alarms (aged, not recounted)\n\n"
            "- 🟡 **Conformance RED** — agents: builder · first seen **2026-06-30**, "
            "last **2026-07-31**, **age 27 days**.\n"
            "- 🔴 2026-07-31 Conformance RED — agents: builder (F).\n")
        self.q.write_text(QUEUE_HEAD)
        hygiene.c_alarm_dedup(self.ctx(today="2026-07-31"))
        lines = [ln for ln in self.inbox.read_text().splitlines() if "Conformance RED" in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn("first seen **2026-06-30**", lines[0])


class TestStrayRoot(_H):
    def test_file_without_domain_frontmatter_goes_to_a_human(self):
        p = self.office / "loose-note.md"
        p.write_text("no frontmatter at all\n")
        self.age(p, 30)
        res = hygiene.c_stray_root(self.ctx())
        self.assertTrue(p.exists())
        self.assertIn("does not say where it belongs", res["human"][0]["reason"])

    def test_file_is_filed_by_its_own_frontmatter_and_undo_moves_it_back(self):
        (self.office / "04 Governance" / "Ledger").mkdir(parents=True)
        p = self.office / "sweep.md"
        p.write_text('---\ndomain: "04 Governance"\ntype: ledger-entry\n---\n\nx\n')
        self.age(p, 30)
        res = hygiene.c_stray_root(self.ctx())
        self.assertFalse(p.exists())
        self.assertTrue((self.office / "04 Governance" / "Ledger" / "sweep.md").exists())
        self.assertEqual(res["closed"], 1)
        ok, msg = self.undo(self.actions()[0]["id"])
        self.assertTrue(ok, msg)
        self.assertTrue(p.exists())

    def test_missing_surface_is_never_created(self):
        (self.office / "04 Governance").mkdir()
        p = self.office / "sweep.md"
        p.write_text('---\ndomain: "04 Governance"\ntype: ledger-entry\n---\n\nx\n')
        self.age(p, 30)
        res = hygiene.c_stray_root(self.ctx())
        self.assertTrue(p.exists())
        self.assertFalse((self.office / "04 Governance" / "Ledger").exists())
        self.assertIn("does not exist", res["human"][0]["reason"])

    def test_canonical_root_files_are_left_alone(self):
        for name in ("CLAUDE.md", "inbox.md", "README.md"):
            p = self.office / name
            p.write_text("x\n")
            self.age(p, 90)
        res = hygiene.c_stray_root(self.ctx())
        self.assertEqual((res["human"], res["open"], res["closed"]), ([], 0, 0))

    def test_deferred_items_are_carded(self):
        p = self.office / "queue.md"
        p.write_text("live queue\n")
        self.age(p, 60)
        res = hygiene.c_stray_root(self.ctx(_policy(stray_root={"defer": {"queue.md": "held: a live writer owns it"}})))
        self.assertTrue(p.exists())
        self.assertEqual(res["human"], [])
        self.assertEqual(len(res["deferred"]), 1)


class TestCaps(_H):
    def test_per_class_limit_stops_the_run(self):
        for i in range(6):
            (self.office / f"f{i}.py").write_text("x\n")
            bak = self.office / f"f{i}.py.bak-20260101"
            bak.write_text("y\n")
            self.age(bak, 40)
        res = hygiene.c_drift_debris(self.ctx(_policy(drift_debris={"limit": 2})))
        self.assertEqual(res["closed"], 2)
        self.assertEqual(len(list(self.office.glob("*.bak-20260101"))), 4)

    def test_global_budget_stops_the_run(self):
        for i in range(4):
            (self.office / f"g{i}.py").write_text("x\n")
            bak = self.office / f"g{i}.py.bak-1"
            bak.write_text("y\n")
            self.age(bak, 40)
        pol = _policy()
        pol["max_actions_per_run"] = 1
        res = hygiene.c_drift_debris(self.ctx(pol))
        self.assertEqual(res["closed"], 1)
        self.assertTrue(any("cap reached" in e for e in res["errors"]))


if __name__ == "__main__":
    unittest.main()
