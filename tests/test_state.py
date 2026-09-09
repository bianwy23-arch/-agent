from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from shopping_agent.state import InvalidChange, TaskStore


def budget(amount):
    return {"target": "requirements", "key": "budget",
            "value": {"status": "active", "strength": "hard",
                      "value": {"amount": amount, "currency": "USD"}}}


def exclude(pid):
    return {"target": "excluded", "key": pid, "value": True}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.store = TaskStore(self.path)
        self.task = self.store.create("headphones")
        self.store.remember(self.task, "A", {"connection": "wired"})
        self.store.remember(self.task, "B", {"connection": "wireless"})
        self.turn("1", "预算 8000")
        self.store.apply_group(self.task, "1", "budget", [budget("8000")], "预算 8000")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def turn(self, tid, text):
        self.store.register_turn(self.task, tid, text)

    def state(self):
        return self.store.get(self.task)

    def amount(self):
        return self.state()["requirements"]["budget"]["value"]["amount"]

    def test_budget_undo_keeps_exclusion_and_new_facts_after_restart(self):
        self.turn("2", "预算 9000，A 不要")
        self.store.apply_group(self.task, "2", "edit", [budget("9000"), exclude("A")], "预算 9000，A 不要")
        self.store.remember(self.task, "B", {"battery": "30 hours"})
        self.store.close()
        self.store = TaskStore(self.path)
        self.turn("3", "只恢复预算")
        self.store.undo(self.task, "3", "undo", "只恢复预算", field="budget")
        self.assertEqual(self.amount(), "8000")
        self.assertTrue(self.state()["excluded"]["A"])
        self.assertEqual(self.state()["candidates"]["B"]["facts"]["battery"], "30 hours")
        self.assertEqual(self.state()["requirements_version"], 3)
        self.assertEqual(self.state()["candidates"]["A"]["qualification"], "unknown")

    def test_whole_turn_undo_covers_independent_groups(self):
        self.turn("2", "预算 9000，A 不要")
        self.store.apply_group(self.task, "2", "budget", [budget("9000")], "预算 9000")
        self.store.apply_group(self.task, "2", "exclude", [exclude("A")], "A 不要")
        self.turn("3", "撤销刚才的修改")
        self.store.undo(self.task, "3", "undo", "撤销刚才的修改")
        self.assertEqual(self.amount(), "8000")
        self.assertEqual(self.state()["excluded"], {})

    def test_cancel_pending_keeps_independent_applied_group(self):
        self.turn("2", "A 不要，预算提高一点")
        self.store.apply_group(self.task, "2", "exclude", [exclude("A")], "A 不要")
        self.store.clarify(self.task, "2", "budget", "预算提高一点", ["budget"])
        self.store.cancel_clarification(self.task, "2", "budget")
        self.assertEqual(self.amount(), "8000")
        self.assertTrue(self.state()["excluded"]["A"])
        self.assertEqual(self.state()["pending"], {})

    def test_pending_only_previous_turn_does_not_undo_older_change(self):
        self.turn("2", "预算提高一点")
        self.store.clarify(self.task, "2", "pending", "预算提高一点", ["budget"])
        self.turn("3", "撤销刚才的修改")
        with self.assertRaises(InvalidChange):
            self.store.undo(self.task, "3", "undo", "撤销刚才的修改")
        self.assertEqual(self.amount(), "8000")

    def test_group_invalidity_is_atomic_and_correctable(self):
        self.turn("2", "A 不要，预算 9000")
        before = self.state()
        with self.assertRaises(InvalidChange):
            self.store.apply_group(self.task, "2", "edit", [exclude("A"), budget("NaN")], "A 不要，预算 9000")
        self.assertEqual(self.state(), before)
        self.store.apply_group(self.task, "2", "edit", [exclude("A"), budget("9000")], "A 不要，预算 9000")
        self.assertEqual(self.amount(), "9000")

    def test_retry_is_idempotent_and_cannot_change_applied_payload(self):
        args = (self.task, "1", "budget", [budget("8000")], "预算 8000")
        before = self.state()
        self.assertEqual(self.store.apply_group(*args), "already_applied")
        self.assertEqual(self.state(), before)
        with self.assertRaises(InvalidChange):
            self.store.apply_group(self.task, "1", "budget", [budget("9000")], "预算 8000")

    def test_dependent_group_cannot_be_partially_undone(self):
        self.turn("2", "预算 9000，同时排除 A")
        self.store.apply_group(self.task, "2", "edit", [budget("9000"), exclude("A")], "预算 9000，同时排除 A", dependent=True)
        self.turn("3", "只恢复预算")
        before = self.state()
        with self.assertRaises(InvalidChange):
            self.store.undo(self.task, "3", "undo", "只恢复预算", field="budget")
        self.assertEqual(self.state(), before)

    def test_hypothesis_preserves_formal_state_and_ending_keeps_new_facts(self):
        self.turn("2", "如果预算 9000 呢")
        before = self.state()["requirements"]
        self.store.explore(self.task, "2", "如果预算 9000 呢", {"budget": budget("9000")["value"]})
        self.assertEqual(self.state()["requirements"], before)
        self.store.remember(self.task, "B", {"battery": "30 hours"})
        self.store.end_exploration(self.task)
        self.assertEqual(self.amount(), "8000")
        self.assertEqual(self.state()["candidates"]["B"]["facts"]["battery"], "30 hours")

    def test_task_isolation(self):
        second = self.store.create("mice")
        self.store.register_turn(second, "1", "预算 20")
        self.store.apply_group(second, "1", "budget", [budget("20")], "预算 20")
        self.assertEqual(self.amount(), "8000")
        self.assertEqual(self.store.get(second)["excluded"], {})

    def test_display_reference_stays_bound_to_original_order(self):
        self.store.display(self.task, "batch1", ["A", "B"])
        self.store.display(self.task, "batch2", ["B", "A"])
        self.assertEqual(self.store.resolve(self.task, "batch1", 2), "B")
        with self.assertRaises(InvalidChange):
            self.store.display(self.task, "batch1", ["B", "A"])
        with self.assertRaises(InvalidChange):
            self.store.resolve(self.task, "batch1", 0)

    def test_unknown_and_no_preference_differ(self):
        self.assertNotIn("weight", self.state()["requirements"])
        self.turn("2", "重量无所谓")
        op = {"target": "requirements", "key": "weight", "value": {"status": "no_preference", "strength": "soft", "value": None}}
        self.store.apply_group(self.task, "2", "weight", [op], "重量无所谓")
        self.assertEqual(self.state()["requirements"]["weight"]["status"], "no_preference")

    def test_unknown_candidate_and_fabricated_source_are_rejected(self):
        with self.assertRaises(InvalidChange):
            self.store.apply_group(self.task, "1", "bad", [exclude("ghost")], "预算 8000")
        with self.assertRaises(InvalidChange):
            self.store.apply_group(self.task, "1", "bad", [budget("9000")], "预算 9000")

    def test_undo_can_itself_be_undone_and_retries_are_safe(self):
        self.turn("2", "预算 9000")
        self.store.apply_group(self.task, "2", "b", [budget("9000")], "预算 9000")
        self.turn("3", "恢复预算")
        self.store.undo(self.task, "3", "undo", "恢复预算", field="budget")
        self.assertEqual(self.store.undo(self.task, "3", "undo", "恢复预算", field="budget"), "already_applied")
        self.turn("4", "撤销刚才的修改")
        self.store.undo(self.task, "4", "undo", "撤销刚才的修改")
        self.assertEqual(self.amount(), "9000")

    def test_unsupported_condition_is_not_silently_dropped(self):
        self.turn("2", "轻的才加到 9000")
        op = deepcopy(budget("9000"))
        op["condition"] = "light"
        with self.assertRaises(InvalidChange):
            self.store.apply_group(self.task, "2", "bad", [op], "轻的才加到 9000")
        self.assertEqual(self.amount(), "8000")


if __name__ == "__main__":
    unittest.main()
