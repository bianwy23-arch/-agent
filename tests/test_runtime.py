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
from openai.types.responses import ResponseFunctionToolCall

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
        return ModelResponse(output=[ResponseFunctionToolCall(id=f"fc_{self.count}",call_id=f"call_{self.count}",name=name,
                             arguments=json.dumps(arguments),type="function_call")],
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
