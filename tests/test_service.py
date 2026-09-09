import os
from pathlib import Path
import unittest

from pydantic import ValidationError
from shopping_agent.catalog import Catalog
from shopping_agent.contracts import FinalAnswer, TurnPlan
from shopping_agent.service import ShoppingTurn
from shopping_agent.state import InvalidChange, TaskStore


DATA = Path(os.environ.get("SHOPPING_CATALOG_DIR", Path(__file__).resolve().parents[1] / "data/amazon/catalog_v1"))


def plan(amount="100", **kwargs):
    return TurnPlan.model_validate({"category": "headphones", "groups": [{"group_id": "budget", "quote": "预算100美元", "action": "apply", "operations": [{"target": "requirements", "key": "budget", "value": {"status": "active", "strength": "hard", "value": {"amount": amount, "currency": "USD"}}}]}], **kwargs})


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog(DATA)

    def setUp(self):
        self.store = TaskStore(":memory:")
        self.conv = {"task_ids": [], "active_task_id": None}
        self.turn = ShoppingTurn(self.store, self.catalog, self.conv, "预算100美元")

    def tearDown(self):
        self.store.close()

    def test_tools_cannot_bypass_turn_processing(self):
        with self.assertRaises(InvalidChange):
            self.turn.search("", "formal")

    def test_search_uses_authoritative_budget(self):
        self.turn.process(plan())
        result = self.turn.search("", "formal")
        self.assertTrue(all(p["price"]["amount"] <= 100 for p in result["items"]))
        self.assertEqual(result["filters"]["budget"], {"amount": "100", "currency": "USD"})

    def test_keyword_empty_cannot_be_presented_as_no_match(self):
        self.turn.process(plan())
        self.turn.search("unfindabletoken012345", "formal")
        with self.assertRaises(InvalidChange):
            self.turn.finish(FinalAnswer(kind="no_match", message="没有"))

    def test_recommendation_requires_inspection_and_valid_citation(self):
        self.turn.process(plan())
        pid = self.turn.search("", "formal")["items"][0]["id"]
        answer = FinalAnswer(kind="recommendation", message="推荐", product_ids=[pid])
        with self.assertRaises(InvalidChange):
            self.turn.finish(answer)
        evidence = self.turn.inspect(pid, ["price"])
        answer = FinalAnswer(kind="recommendation", message="推荐", product_ids=[pid], citations=[{"product_id":pid,"field":"title","quote":evidence["context"]["title"]}])
        result = self.turn.finish(answer)
        self.assertIsNone(result["state"]["decision"]["selection"])
        self.assertEqual(self.store.resolve(self.turn.task_id, result["display_id"], 1), pid)

    def test_unimplemented_hard_constraint_cannot_be_marked_passed(self):
        p = plan().model_dump()
        p["groups"][0]["operations"].append({"target":"requirements","key":"waterproof","value":{"status":"active","strength":"hard","value":"IPX8"}})
        self.turn.process(TurnPlan.model_validate(p))
        pid = self.turn.search("", "formal")["items"][0]["id"]
        self.turn.inspect(pid, ["price"])
        self.assertIn("waterproof", self.turn.qualification(pid,"formal")["unknown"])

    def test_failed_group_cannot_disappear_by_omission(self):
        self.turn.process(plan(amount="NaN"))
        self.turn.process(TurnPlan(category="headphones", groups=[]))
        self.assertTrue(self.turn.group_errors)
        with self.assertRaises(InvalidChange):
            self.turn.finish(FinalAnswer(kind="answered",message="已更新"))
        self.turn.process(plan())
        self.assertEqual(self.turn.group_errors, [])

    def test_hypothesis_cannot_be_created_by_search_parameter(self):
        self.turn.process(plan())
        with self.assertRaises(InvalidChange):
            self.turn.search("", "hypothetical")

    def test_comparison_scope_blocks_search_and_other_products(self):
        self.turn.process(plan())
        pid = self.turn.search("", "formal")["items"][0]["id"]
        next_turn = ShoppingTurn(self.store,self.catalog,self.conv,"只比较这个")
        next_turn.process(TurnPlan(scope_ids=[pid]))
        with self.assertRaises(InvalidChange):
            next_turn.search("", "formal")
        self.assertEqual(next_turn.inspect(pid, ["price"])["product_id"],pid)

    def test_cheapest_cannot_skip_cheaper_unknown_candidates(self):
        self.turn.process(plan(cheapest_requested=True))
        items = self.turn.search("", "formal")["items"]
        pid = items[-1]["id"]
        source = self.turn.inspect(pid,["price"])
        with self.assertRaisesRegex(InvalidChange,"cheaper"):
            self.turn.finish(FinalAnswer(kind="recommendation",message="最便宜",product_ids=[pid],citations=[{"product_id":pid,"field":"title","quote":source["context"]["title"]}]))

    def test_unsupported_schema_fields_rejected(self):
        with self.assertRaises(ValidationError):
            TurnPlan.model_validate({"force_replace_entire_task": True})

    def test_explicit_stop_blocks_more_tools(self):
        self.turn.process(plan(stop_requested=True))
        with self.assertRaises(InvalidChange):
            self.turn.search("", "formal")
        self.assertEqual(self.turn.finish(FinalAnswer(kind="stopped",message="好的"))["kind"],"stopped")

    def test_cannot_resume_another_conversations_task(self):
        other = self.store.create("mice")
        with self.assertRaises(InvalidChange):
            self.turn.process(TurnPlan(resume_task_id=other))

    def test_currency_gap_is_not_an_execution_failure(self):
        p = plan().model_dump()
        p["groups"][0]["operations"][0]["value"]["value"]["currency"]="CNY"
        result = self.turn.process(TurnPlan.model_validate(p))
        self.assertEqual(result["blocking_issues"][0]["action"],"needs_user")
        with self.assertRaisesRegex(InvalidChange,"runtime only"):
            self.turn.finish(FinalAnswer(kind="execution_limited",message="请提供美元预算"))
        self.assertEqual(self.turn.finish(FinalAnswer(kind="needs_user",message="请提供美元预算"))["kind"],"needs_user")
