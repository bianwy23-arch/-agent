"""SDK integration tests with an explicitly scripted model, never API evals."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from agents.models.interface import Model
from agents.items import ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from shopping_agent.config import Settings
from shopping_agent.runtime import ShoppingRuntime


DATA = Path(os.environ.get("SHOPPING_CATALOG_DIR", Path(__file__).resolve().parents[1] / "data/amazon/catalog_v1"))


class ScriptedModel(Model):
    def __init__(self, steps):
        self.steps = iter(steps)
        self.count = 0

    async def get_response(self, *args, **kwargs):
        name, arguments = next(self.steps)
        self.count += 1
        if name == 'message':
            return ModelResponse(output=[ResponseOutputMessage(id=f'm_{self.count}',role='assistant',type='message',status='completed',content=[ResponseOutputText(text=arguments,type='output_text',annotations=[])])],usage=Usage(requests=1,input_tokens=10,output_tokens=10,total_tokens=20),response_id=None)
        return ModelResponse(output=[ResponseFunctionToolCall(id=f"fc_{self.count}",call_id=f"call_{self.count}",name=name,
                             arguments=arguments if isinstance(arguments,str) else json.dumps(arguments),type="function_call")],
                             usage=Usage(requests=1,input_tokens=10,output_tokens=10,total_tokens=20),response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


PLAN = {"plan":{"category":"headphones","groups":[{"group_id":"b","quote":"预算100美元","action":"apply",
         "operations":[{"target":"requirements","key":"budget","value":{"status":"active","strength":"hard","value":{"amount":"100","currency":"USD"}}}]}]}}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_tools_preserve_sqlite_thread_and_usage_serializes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings=Settings(root=Path(tmp),api_key="dummy-not-a-real-key")
            model=ScriptedModel([("process_turn",PLAN),("finish_turn",{"answer":{"kind":"answered","message":"预算已记录"}})])
            runtime=ShoppingRuntime(settings,catalog_dir=DATA,model=model)
            try:
                cid=runtime.create_conversation()
                output=await runtime.run(cid,"预算100美元")
                self.assertEqual(output["kind"],"answered")
                self.assertEqual(output["state"]["requirements"]["budget"]["value"]["amount"],"100")
                self.assertIsNone(output["runtime"]["error"])
                json.dumps(output)  # SDK nested usage objects must not escape.
                trace=(Path(tmp)/".runtime/trace.jsonl").read_text()
                self.assertNotIn(settings.api_key,trace)
            finally:
                await runtime.close()

    async def test_iteration_limit_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings=Settings(root=Path(tmp),api_key="dummy",max_turns=1)
            runtime=ShoppingRuntime(settings,catalog_dir=DATA,model=ScriptedModel([("process_turn",PLAN)]))
            try:
                output=await runtime.run(runtime.create_conversation(),"预算100美元")
                self.assertEqual(output["kind"],"execution_limited")
                self.assertEqual(output["state"]["requirements"]["budget"]["value"]["amount"],"100")
            finally:
                await runtime.close()

    def test_missing_key_fails_without_fallback(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ValueError,"DEEPSEEK_API_KEY"):
                Settings.load(tmp)

    async def test_plain_final_gets_one_bounded_tool_finalization_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings=Settings(root=Path(tmp),api_key='dummy')
            model=ScriptedModel([('process_turn',PLAN),('message','已记录'),('finish_turn',{'answer':{'kind':'answered','message':'预算已记录'}})])
            runtime=ShoppingRuntime(settings,catalog_dir=DATA,model=model)
            try:
                result=await runtime.run(runtime.create_conversation(),'预算100美元')
                self.assertEqual(result['kind'],'answered')
                self.assertEqual(result['runtime']['usage']['requests'],3)
                self.assertIn('finalization_retry',(Path(tmp)/'.runtime/trace.jsonl').read_text())
            finally:await runtime.close()

    async def test_json_retry_reports_position_and_preserves_limit_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings=Settings(root=Path(tmp),api_key='dummy')
            runtime=ShoppingRuntime(settings,catalog_dir=DATA,model=ScriptedModel([('process_turn','{"plan":]}')]*3))
            try:
                result=await runtime.run(runtime.create_conversation(),'预算100美元')
                self.assertEqual(result['runtime']['error'],'schema_retry_limit')
                rows=[json.loads(l) for l in (Path(tmp)/'.runtime/trace.jsonl').read_text().splitlines()]
                errors=[e for e in rows if e['event']=='tool_schema_error']
                self.assertEqual(len(errors),3)
                self.assertGreater(errors[0]['diagnostic']['column'],0)
            finally:await runtime.close()

    async def test_valid_json_with_wrong_field_reports_schema_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings=Settings(root=Path(tmp),api_key='dummy')
            invalid={'plan':{'category':'headphones','pending_turn_id':'misplaced'}}
            runtime=ShoppingRuntime(settings,catalog_dir=DATA,model=ScriptedModel([('process_turn',invalid)]*3))
            try:
                result=await runtime.run(runtime.create_conversation(),'预算100美元')
                self.assertEqual(result['runtime']['error'],'schema_retry_limit')
                rows=[json.loads(l) for l in (Path(tmp)/'.runtime/trace.jsonl').read_text().splitlines()]
                errors=[e for e in rows if e['event']=='tool_schema_error']
                self.assertEqual(errors[0]['diagnostic']['schema_errors'][0]['loc'],['pending_turn_id'])
            finally:await runtime.close()
