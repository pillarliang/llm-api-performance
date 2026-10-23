"""Exercise real OSS with simulated model calls; remove this check's objects afterwards."""
import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import alibabacloud_oss_v2 as oss
from fastapi.testclient import TestClient
import httpx
import openai

import benchmark
import server
from storage import get_store


def main():
    if os.environ.get("RESULTS_BACKEND") != "oss":
        raise SystemExit("This check requires RESULTS_BACKEND=oss")
    original_prefix = os.environ.get("OSS_PREFIX", "results")
    os.environ["OSS_PREFIX"] = ".verification/" + uuid.uuid4().hex
    calls = []

    def handler(request):
        calls.append(request)
        chunks = [dict(choices=[dict(index=0, delta=dict(content="OSS verification"), finish_reason=None)]),
                  dict(choices=[dict(index=0, delta={}, finish_reason="stop")])]
        body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    def factory(*args, **kwargs):
        def client(**settings):
            return openai.AsyncOpenAI(**settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        return benchmark.BenchmarkRun(*args, **kwargs, client_factory=client)

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = get_store(root)
        try:
            with patch.object(server, "RESULTS", root), patch.object(server, "run_registry", {}), \
                 patch.object(server, "active_state", None), patch.object(server, "active_task", None), \
                 patch.object(server, "BenchmarkRun", factory), patch.dict(os.environ, {"DASHSCOPE_API_KEY": "oss-verification-only"}):
                with TestClient(server.app) as web:
                    config = web.get("/api/config").json()["config"]
                    config.pop("providers", None)
                    config.update(name="oss-verification", version="v1", input_tokens=[512], concurrencies=[1], repetitions=1, warmup=0, max_retries=0)
                    config["models"] = config["models"][:1]
                    config["models"][0].update(enabled=True, profiles=config["models"][0]["profiles"][:1])
                    response = web.post("/api/runs", json={"config": config, "mode": "full"})
                    assert response.status_code == 200, response.text
                    job = response.json()
                    async def finish(job_id):
                        await server.run_registry[job_id]["task"]
                    web.portal.call(finish, job["job_id"])
                    state = web.get("/api/jobs").json()[0]
                    assert state["status"] == "finished" and not state.get("storage_pending"), state
                    assert not (root / job["path"]).exists(), "Local staging was not cleaned"
                    assert len(web.get("/api/runs").json()) == 1
                    for name in ["results.json", "requests.jsonl", "events.jsonl", "report.html"]:
                        response = web.get("/api/artifact/" + job["path"] + "/" + name)
                        assert response.status_code == 200, name
                server.run_registry.clear()
                # Simulate moving to a host with an entirely empty local results directory.
                with TemporaryDirectory() as fresh, patch.object(server, "RESULTS", Path(fresh)):
                    with TestClient(server.app) as web:
                        assert len(web.get("/api/jobs").json()) == 1
                        assert len(web.get("/api/runs").json()) == 1
                        response = web.post("/api/runs", json={"config": config, "mode": "incremental"})
                        assert response.status_code == 200, response.text
                        assert response.json()["plan"]["pending_calls"] == 0, response.text
                        web.portal.call(finish, response.json()["job_id"])
                assert len(calls) == 1, "Incremental run repeated the model request"
                print("PASS: real OSS upload, all artifact downloads, local cleanup, empty-disk restart and incremental skip; model request simulated.")
        finally:
            for key in store.keys():
                store.client.delete_object(oss.DeleteObjectRequest(bucket=store.bucket, key=store.key(key)))
            assert store.keys() == []
            os.environ["OSS_PREFIX"] = original_prefix
            print("Verification objects removed.")


if __name__ == "__main__":
    main()
