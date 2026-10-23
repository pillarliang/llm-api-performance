"""Control-plane tests, using in-process ASGI requests; no sockets or live APIs."""
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

HAS_WEB = all(importlib.util.find_spec(name) is not None for name in ("fastapi", "httpx", "openai", "tiktoken"))


@unittest.skipUnless(HAS_WEB, "需要安装 Web / SDK 依赖后验证 ASGI 接口")
class ServerTests(unittest.TestCase):
    def setUp(self):
        import server
        from fastapi.testclient import TestClient
        self.server = server
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.results = Path(self.temp.name) / "results"
        self.configuration = Path(self.temp.name) / "state" / "config.local.yaml"
        self.patches = [patch.dict(os.environ, {"DASHSCOPE_API_KEY": "private-model-key"}),
                        patch.object(server, "RESULTS", self.results), patch.object(server, "CONFIG", self.configuration),
                        patch.object(server, "active_task", None), patch.object(server, "active_state", None)]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)
        self.client = TestClient(server.app)
        self.headers = {}

    def test_no_login_required_and_no_key_disclosure(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/api/config").status_code, 200)
        response = self.client.get("/api/config", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-model-key", response.text)
        self.assertIn("DASHSCOPE_API_KEY", response.text)
        self.assertEqual(self.client.post("/api/stop").status_code, 200)
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.assertNotIn('id="login"', self.client.get("/").text)

    def test_model_catalog_available_without_login(self):
        from unittest.mock import AsyncMock
        catalog = AsyncMock()
        catalog.get.return_value = {"count": 99, "models": [], "stale": False}
        with patch.object(self.server, "model_catalog", catalog):
            response = self.client.get("/api/models?refresh=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 99)
        self.assertTrue(catalog.get.call_args.kwargs["refresh"])
        self.assertNotIn("private-model-key", response.text)

    def test_web_save_and_reload_from_state_directory(self):
        config = self.client.get("/api/config", headers=self.headers).json()["config"]
        config["name"] = "web-test"
        response = self.client.put("/api/config", headers=self.headers, json={"config": config})
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/api/config", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["name"], "web-test")
        self.assertTrue(self.configuration.exists())

    def test_prompt_path_boundary_and_invalid_yaml(self):
        config = self.client.get("/api/config", headers=self.headers).json()["config"]
        config["tasks"][0]["template_file"] = "/etc/passwd"
        response = self.client.post("/api/config/validate", headers=self.headers, json={"config": config})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/config/validate", headers=self.headers, json={"yaml": "models: ["})
        self.assertEqual(response.status_code, 400)

    def test_plan_has_no_side_effects_and_download_is_bounded(self):
        config = self.client.get("/api/config", headers=self.headers).json()["config"]
        response = self.client.post("/api/plan", headers=self.headers, json={"config": config, "mode": "full"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["planned_cases"], 54)  # Current focus preset: 18 groups × 3 repeats.
        self.assertFalse(self.results.exists())
        self.assertEqual(self.client.get("/api/artifact/%2e%2e/README.md", headers=self.headers).status_code, 404)
        directory = self.results / "test/v1/runs/example"
        directory.mkdir(parents=True)
        (directory / "results.json").write_text(json.dumps({"safe": True}))
        response = self.client.get("/api/artifact/test/v1/runs/example/results.json", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"safe": True})


if __name__ == "__main__": unittest.main()
