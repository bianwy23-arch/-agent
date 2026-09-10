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
        p["groups"][0]["operations"].append({"target":"requirements","key":"battery_health","value":{"status":"active","strength":"hard","value":"100%"}})
        self.turn.process(TurnPlan.model_validate(p))
        pid = self.turn.search("", "formal")["items"][0]["id"]
        self.turn.inspect(pid, ["price"])
        self.assertIn("battery_health", self.turn.qualification(pid,"formal")["unknown"])

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

    def test_formal_assessment_is_persisted_and_invalidated_on_change(self):
        self.turn.process(plan())
        pid=self.turn.search('', 'formal')['items'][0]['id']
        candidate=self.turn.state()['candidates'][pid]
        self.assertEqual(candidate['qualification'],'satisfied')
        self.assertEqual(candidate['assessment']['requirements_version'],1)
        next_turn=ShoppingTurn(self.store,self.catalog,self.conv,'预算100美元')
        next_turn.process(plan(amount='50'))
        candidate=next_turn.state()['candidates'][pid]
        self.assertEqual(candidate['qualification'],'unknown')
        self.assertNotIn('assessment',candidate)

    def test_explicit_clarification_answer_resolves_prior_turn_pending(self):
        self.turn.process(TurnPlan(category='headphones',groups=[{'group_id':'q','quote':'预算100美元','action':'clarify','clarification_fields':['budget']}]))
        self.assertTrue(self.turn.state()['pending'])
        next_turn=ShoppingTurn(self.store,self.catalog,self.conv,'预算100美元')
        next_turn.process(plan())
        self.assertFalse(next_turn.state()['pending'])

    def test_explicit_one_product_request_rejects_extra_recommendations(self):
        self.turn.text='预算100美元，推荐一个'
        self.turn.process(plan())
        ids=[c['id'] for c in self.turn.search('', 'formal')['items'][:2]]
        citations=[]
        for pid in ids:
            result=self.turn.inspect(pid,['price'])
            citations.append({'product_id':pid,'field':'title','quote':result['context']['title']})
        with self.assertRaisesRegex(InvalidChange,'exactly 1'):
            self.turn.finish(FinalAnswer(kind='recommendation',message='推荐两款',product_ids=ids,citations=citations))

    def test_unsupported_typed_requirement_is_saved_not_dropped(self):
        p=plan().model_dump()
        p['groups'][0]['operations'].append({'target':'requirements','key':'noise_cancellation','value':{'status':'active','strength':'hard','value':{'operator':'gte','value':'40','unit':'dB'}}})
        self.turn.process(TurnPlan.model_validate(p))
        self.assertEqual(self.turn.state()['requirements']['noise_cancellation']['value']['unit'],'dB')
        pid=self.turn.search('', 'formal')['items'][0]['id']
        self.assertIn('noise_cancellation',self.turn.qualification(pid,'formal')['unknown'])

    def test_group_retry_can_omit_initial_route_without_losing_task(self):
        p=plan(amount='NaN',new_task=True)
        self.turn.process(p)
        tid=self.turn.task_id
        retry=plan().model_dump();retry['category']=None
        self.turn.process(TurnPlan.model_validate(retry))
        self.assertEqual(self.turn.task_id,tid)
        self.assertEqual(self.turn.group_errors,[])
        self.assertEqual(len(self.conv['task_ids']),1)

    def test_recommendation_draft_cannot_publish_unverified_specs_or_guarantees(self):
        self.turn.process(plan())
        pid=self.turn.search('', 'formal')['items'][0]['id']
        data=self.turn.inspect(pid,['price'])
        result=self.turn.finish(FinalAnswer(kind='recommendation',message='保证全市场音质最好，IPX6等于防尘，续航999小时',product_ids=[pid],citations=[{'product_id':pid,'field':'title','quote':data['context']['title']}]))
        self.assertNotIn('999',result['message'])
        self.assertNotIn('保证全市场',result['message'])
        self.assertIn('历史价格',result['message'])
        self.assertIn('未进行独立实测',result['message'])

    def test_initial_empty_group_is_no_change_but_cannot_hide_rejection(self):
        self.turn.process(TurnPlan(category='headphones',groups=[{'group_id':'empty','action':'apply','quote':'预算100美元','operations':[]}]))
        self.assertEqual(self.turn.group_errors,[])
        self.assertEqual(self.turn.state()['history'],[])
        self.turn.process(plan(amount='NaN'))
        self.turn.process(TurnPlan(groups=[{'group_id':'budget','action':'apply','quote':'预算100美元','operations':[]}]))
        self.assertTrue(self.turn.group_errors)

    def test_unsupported_category_cannot_mutate_existing_task(self):
        self.turn.process(plan())
        before=self.turn.state()
        other=ShoppingTurn(self.store,self.catalog,self.conv,'牛奶预算5美元')
        result=other.process(TurnPlan(category='milk',new_task=True))
        self.assertEqual(result['blocking_issues'][0]['code'],'unsupported_category')
        other.process(TurnPlan(category='headphones',groups=[]))
        self.assertEqual(other.state(),before)
        with self.assertRaises(InvalidChange):other.search('', 'formal')
        self.assertEqual(other.finish(FinalAnswer(kind='data_limited',message='不支持牛奶'))['kind'],'data_limited')

    def test_uncertainty_answer_does_not_claim_unperformed_exhaustive_inspection(self):
        p=plan().model_dump()
        p['groups'][0]['operations'].append({'target':'requirements','key':'protocol','value':{'status':'active','strength':'hard','value':'ZQX-9000'}})
        self.turn.process(TurnPlan.model_validate(p))
        result=self.turn.finish(FinalAnswer(kind='data_limited',message='我已穷举查看所有商品，都不兼容'))
        self.assertNotIn('穷举',result['message'])
        self.assertIn('不足以确认',result['message'])

    def test_budget_clarification_does_not_publish_false_incompatibility_claim(self):
        self.turn.process(TurnPlan(category='headphones',groups=[{'group_id':'q','quote':'预算100美元','action':'clarify','clarification_fields':['budget']}]))
        result=self.turn.finish(FinalAnswer(kind='needs_user',message='100和50两个上限无法同时成立'))
        self.assertNotIn('无法同时成立',result['message'])
        self.assertIn('待澄清的预算变更暂不生效',result['message'])

    def test_duplicate_undo_intent_in_distinct_groups_is_idempotent(self):
        self.turn.process(plan())
        second=ShoppingTurn(self.store,self.catalog,self.conv,'预算100美元')
        second.process(plan(amount='150'))
        undo=ShoppingTurn(self.store,self.catalog,self.conv,'撤销刚才修改')
        result=undo.process(TurnPlan(groups=[{'group_id':g,'quote':'撤销刚才修改','action':'undo'} for g in ['one','duplicate']]))
        self.assertEqual(result['errors'],[])
        self.assertEqual(undo.state()['requirements']['budget']['value']['amount'],'100')
        self.assertEqual(sum(bool(e['undo_of']) for e in undo.state()['history']),1)
        self.assertEqual(result['applied'][1]['result'],'already_applied')

    def test_unsupported_predicate_exposes_capability_stop_without_losing_state(self):
        p=plan().model_dump()
        p['groups'][0]['operations'].append({'target':'requirements','key':'noise_cancellation','value':{'status':'active','strength':'hard','value':{'operator':'gte','value':'40','unit':'dB'}}})
        result=self.turn.process(TurnPlan.model_validate(p))
        self.assertEqual(result['errors'],[])
        self.assertEqual(result['capability_issues'][0]['fields'],['noise_cancellation'])
        self.assertEqual(result['capability_issues'][0]['recommendation_result'],'data_limited')
        self.assertIn('noise_cancellation',self.turn.state()['requirements'])
