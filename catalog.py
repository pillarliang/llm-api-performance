"""Full endpoint model discovery with credential-scoped caching."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import openai


class CatalogError(Exception):
    pass


class ModelCatalog:
    def __init__(self, cache_file, client_factory=None, ttl=300):
        self.cache_file = Path(cache_file) if cache_file is not None else None
        self.client_factory = client_factory or openai.AsyncOpenAI
        self.ttl = ttl
        self.lock = asyncio.Lock()
        self.cache = None

    async def get(self, base_url, api_key, refresh=False):
        if not api_key:
            raise CatalogError("尚未配置 DASHSCOPE_API_KEY")
        base_url = base_url.rstrip("/")
        identity = hashlib.sha256((base_url + "\0" + api_key).encode()).hexdigest()
        requested_at = time.time()
        async with self.lock:
            if self.cache is None and self.cache_file and self.cache_file.exists():
                try:
                    self.cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
                    if not isinstance(self.cache, dict) or not {"identity", "timestamp", "data"}.issubset(self.cache):
                        self.cache = None
                except (OSError, ValueError):
                    self.cache = None
            cached = self.cache if self.cache and self.cache.get("identity") == identity else None
            if cached and ((not refresh and requested_at - cached["timestamp"] < self.ttl) or cached["timestamp"] >= requested_at):
                return dict(cached["data"], stale=False)
            try:
                models = {}
                async with asyncio.timeout(45):
                    async with self.client_factory(api_key=api_key, base_url=base_url, timeout=30, max_retries=0) as client:
                        page = await client.models.list()
                        seen_pages = set()
                        while True:
                            page_ids = tuple(model.id for model in page.data)
                            if page_ids in seen_pages:
                                raise CatalogError("上游模型分页重复，未将不完整列表作为新结果保存")
                            seen_pages.add(page_ids)
                            for model in page.data:
                                if not isinstance(model.id, str) or not model.id:
                                    raise CatalogError("上游返回了无效的模型 ID")
                                models[model.id] = {"id": model.id, "owned_by": getattr(model, "owned_by", None),
                                                    "created": getattr(model, "created", None)}
                            if not page.has_next_page():
                                break
                            page = await page.get_next_page()
                data = {"base_url": base_url, "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "count": len(models), "models": sorted(models.values(), key=lambda model: model["id"].casefold())}
                updated = {"identity": identity, "timestamp": time.time(), "data": data}
                if self.cache_file:
                    self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.cache_file.with_suffix(".tmp")
                    temporary.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(self.cache_file)
                self.cache = updated
                return dict(data, stale=False)
            except (openai.APIError, TimeoutError, OSError, ValueError, CatalogError) as exc:
                message = f"获取模型列表失败（HTTP {exc.status_code}）" if isinstance(exc, openai.APIStatusError) else "获取模型列表失败，请稍后重试"
                if cached:
                    return dict(cached["data"], stale=True, error=message)
                raise CatalogError(message) from exc
