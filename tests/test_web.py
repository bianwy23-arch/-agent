from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from shopping_agent.config import Settings
from shopping_agent.web import create_app
from test_runtime import ScriptedModel, PLAN, DATA


class WebTests(unittest.TestCase):
    def test_http_roundtrip_uses_runtime_and_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(root=Path(tmp), api_key="dummy")
            model = ScriptedModel([("process_turn", PLAN), ("finish_turn", {"answer": {"kind":"answered","message":"预算已记录"}})])
            with TestClient(create_app(settings,catalog_dir=DATA,model=model)) as client:
                self.assertEqual(client.get("/health").json()["catalog_count"],600)
                self.assertIn("日常导购",client.get("/").text)
                cid=client.post("/api/conversations").json()["conversation_id"]
                response=client.post(f"/api/conversations/{cid}/messages",json={"message":"预算100美元"})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()["kind"],"answered")
                self.assertEqual(len(client.get(f"/api/conversations/{cid}").json()["messages"]),2)
                self.assertEqual(client.post("/api/conversations/unknown/messages",json={"message":"hi"}).status_code,400)
                self.assertEqual(client.post(f"/api/conversations/{cid}/messages",json={"message":""}).status_code,422)
