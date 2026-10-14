import asyncio
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

HAS_SDK = importlib.util.find_spec("openai") is not None


@unittest.skipUnless(HAS_SDK, "需要 openai/httpx 以验证模型目录")
class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from catalog import ModelCatalog
        self.catalog_type = ModelCatalog
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.json"

    def client_factory(self, pages, calls):
        class Page:
            def __init__(self, index):
                self.index = index
                self.data = [SimpleNamespace(id=name, owned_by="test", created=1) for name in pages[index]]
            def has_next_page(self): return self.index + 1 < len(pages)
            async def get_next_page(self): return Page(self.index + 1)
        class Client:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.models = self
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def list(self):
                await asyncio.sleep(.01)
                return Page(0)
        return Client

    async def test_full_paginated_list_dedup_cache_and_secret_exclusion(self):
        calls = []
        catalog = self.catalog_type(self.path, self.client_factory([[f"model-{i}" for i in range(60)], [f"model-{i}" for i in range(59, 99)]], calls))
        data = await catalog.get("https://example.test/v1", "private-key")
        self.assertEqual(data["count"], 99)
        self.assertEqual(len({m["id"] for m in data["models"]}), 99)
        self.assertNotIn("private-key", json.dumps(data))
        self.assertNotIn("private-key", self.path.read_text())
        await catalog.get("https://example.test/v1", "private-key")
        self.assertEqual(len(calls), 1)
        await catalog.get("https://example.test/v1", "private-key", refresh=True)
        self.assertEqual(len(calls), 2)

    async def test_concurrent_reads_share_refresh_and_key_changes_invalidate(self):
        calls = []
        catalog = self.catalog_type(self.path, self.client_factory([["model-a"]], calls))
        await asyncio.gather(*(catalog.get("https://example.test/v1", "first") for _ in range(6)))
        self.assertEqual(len(calls), 1)
        await catalog.get("https://example.test/v1", "second")
        self.assertEqual(len(calls), 2)

    async def test_failed_refresh_preserves_old_catalog(self):
        import openai
        calls = []
        catalog = self.catalog_type(self.path, self.client_factory([["model-a"]], calls))
        await catalog.get("https://example.test/v1", "private-key")
        def failing(**kwargs): raise ValueError("private-key must not leak")
        catalog.client_factory = failing
        data = await catalog.get("https://example.test/v1", "private-key", refresh=True)
        self.assertTrue(data["stale"])
        self.assertEqual(data["count"], 1)
        self.assertNotIn("private-key", json.dumps(data))

    async def test_real_sdk_parses_full_model_response(self):
        import httpx
        import openai
        def handler(request):
            self.assertEqual(request.url.path, "/v1/models")
            return httpx.Response(200, json={"object": "list", "data": [{"id": f"model-{i}", "object": "model", "created": 1, "owned_by": "system"} for i in range(99)]})
        def factory(**kwargs):
            return openai.AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        data = await self.catalog_type(self.path, factory).get("https://example.test/v1", "private-key")
        self.assertEqual(data["count"], 99)

    async def test_missing_key_and_repeated_pages_fail_without_partial_cache(self):
        from catalog import CatalogError
        catalog = self.catalog_type(self.path, self.client_factory([["model-a"], ["model-a"]], []))
        with self.assertRaises(CatalogError): await catalog.get("https://example.test/v1", "")
        with self.assertRaises(CatalogError): await catalog.get("https://example.test/v1", "private-key")
        self.assertFalse(self.path.exists())
