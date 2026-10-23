"""OSS persistence, failure recovery, and remote-authoritative API tests."""
import asyncio
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import alibabacloud_oss_v2 as oss

import benchmark as b
import server
from storage import OSSStore, StorageError, encoded, restore_latest, valid_key
from test_benchmark import config, prepared_without_tokenizer, ROOT
from test_jobs import DummyClient


class Body(io.BytesIO):
    def iter_bytes(self):
        yield self.read()


class MemoryClient:
    def __init__(self):
        self.objects = {}
        self.fail = False
        self.puts = []

    def get_object(self, request):
        if self.fail:
            raise oss.exceptions.ServiceError(status_code=403, code="AccessDenied", request_id="test", message="denied", ec="", timestamp="", request_target="")
        if request.key not in self.objects:
            raise oss.exceptions.ServiceError(status_code=404, code="NoSuchKey", request_id="test", message="missing", ec="", timestamp="", request_target="")
        return SimpleNamespace(body=Body(self.objects[request.key]))

    def put_object(self, request):
        if self.fail:
            raise RuntimeError("private-access-key-must-not-leak")
        self.objects[request.key] = request.body
        self.puts.append(request.key)

    def list_objects_v2_paginator(self):
        return self

    def iter_page(self, request):
        if self.fail:
            raise RuntimeError("private-access-key-must-not-leak")
        # One object per page verifies that callers consume every page.
        for key in sorted(self.objects):
            if key.startswith(request.prefix):
                yield SimpleNamespace(contents=[SimpleNamespace(key=key)])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"OSS_BUCKET": "test", "OSS_REGION": "cn-beijing", "OSS_PREFIX": "results"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = MemoryClient()
        self.store = OSSStore(self.root, self.client)

    def output(self):
        path = self.root / "test/v1/runs/run1"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_pagination_and_missing_vs_access_denied(self):
        self.store.put("a.json", b"a")
        self.store.put("b.json", b"b")
        self.assertEqual(self.store.keys(), ["a.json", "b.json"])
        self.assertIsNone(self.store.read("missing.json"))
        self.client.fail = True
        with self.assertRaises(StorageError):
            self.store.read("a.json")
        with self.assertRaises(StorageError):
            self.store.keys()

    def test_sdk_wrapped_missing_object_is_not_a_storage_failure(self):
        for code, status in [("NoSuchKey", 404), ("NoSuchBucket", 404), ("AccessDenied", 403)]:
            cause = oss.exceptions.ServiceError(status_code=status, code=code, request_id="test",
                                                message="test", ec="", timestamp="", request_target="")
            error = oss.exceptions.OperationError(name="GetObject", error=cause)
            with patch.object(self.client, "get_object", side_effect=error):
                if code == "NoSuchKey":
                    self.assertIsNone(self.store.read("missing.json"))
                else:
                    with self.assertRaises(StorageError):
                        self.store.read("missing.json")

    def test_archive_upload_order_cleanup_and_retry_after_restart(self):
        output = self.output()
        (output / "results.json").write_bytes(b'{"ok":true}')
        (output / "requests.jsonl").write_bytes(b'{}\n')
        job = dict(job_id="job1", status="finished", path="test/v1/runs/run1")
        self.client.fail = True
        with self.assertRaises(StorageError):
            self.store.archive(output, job)
        self.assertTrue((output / "results.json").exists())
        self.assertTrue(list((self.root / ".pending").glob("*.json")))
        self.client.fail = False
        restarted = OSSStore(self.root, self.client)
        self.assertEqual(restarted.retry_pending(), [job])
        self.assertFalse(output.exists())
        self.assertFalse(list((self.root / ".pending").glob("*.json")))
        self.assertEqual(self.client.puts[-1], "results/.jobs/job1.json")
        self.assertEqual(restarted.read("test/v1/runs/run1/results.json"), b'{"ok":true}')

    def test_incremental_reads_remote_and_removes_stale_local_artifacts(self):
        output = self.output()
        (output / "requests.jsonl").write_bytes(b'{"stale":true}\n')
        (output / "report.html").write_text("stale")
        self.store.put("test/v1/latest.json", encoded(dict(run_id="run1")))
        self.store.put("test/v1/runs/run1/requests.jsonl", b'{"remote":true}\n')
        restore_latest(self.store, "test", "v1")
        self.assertEqual((output / "requests.jsonl").read_bytes(), b'{"remote":true}\n')
        self.assertFalse((output / "report.html").exists())
        self.client.objects.pop("results/test/v1/latest.json")
        restore_latest(self.store, "test", "v1")
        self.assertFalse((self.root / "test/v1/latest.json").exists())

    def test_pending_upload_prevents_stale_incremental_restore(self):
        output = self.output()
        (output / "results.json").write_bytes(b"{}")
        self.store.put("test/v1/latest.json", encoded(dict(run_id="run1")))
        self.client.fail = True
        with self.assertRaises(StorageError):
            self.store.archive(output)
        self.client.fail = False
        with self.assertRaises(StorageError):
            restore_latest(self.store, "test", "v1")

    def test_keys_are_bounded(self):
        for key in ["../secret", "/etc/passwd", "a//b", "a/../b", "a\\b"]:
            with self.assertRaises(ValueError):
                valid_key(key)

    def test_web_reads_oss_even_with_conflicting_local_file(self):
        from fastapi.testclient import TestClient
        output = self.output()
        (output / "results.json").write_text('{"local":true}')
        self.store.put("test/v1/runs/run1/results.json", b'{"remote":true}')
        with patch.object(server, "RESULTS", self.root), patch.object(server, "get_store", return_value=self.store):
            web = TestClient(server.app)
            response = web.get("/api/artifact/test/v1/runs/run1/results.json")
            self.assertEqual(response.json(), {"remote": True})
            self.assertEqual(web.get("/api/artifact/%2e%2e/results.json").status_code, 404)
            self.client.fail = True
            self.assertEqual(web.get("/api/artifact/test/v1/runs/run1/results.json").status_code, 503)
            response = web.get("/api/runs")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private-access-key", response.text)

    def test_checkpoint_survives_cancellation_and_restart_progress(self):
        cfg = config()
        cfg.update(name="test", repetitions=1, warmup=0)
        output = self.output()
        async def request(client, prompt, record, event):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                record.update(error_type="cancelled")
                raise
        async def scenario():
            run = b.BenchmarkRun(cfg, ROOT, output, prepared_without_tokenizer(cfg),
                                 client_factory=DummyClient, credentials={"BENCHMARK_TEST_KEY": "fake"})
            task = asyncio.create_task(run.run())
            for _ in range(200):
                if "results/test/v1/runs/run1/results.json" in self.client.objects:
                    break
                await asyncio.sleep(.005)
            else:
                self.fail("No periodic OSS checkpoint")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue((output / "requests.jsonl").exists())
            self.store.archive(output)
            records = json.loads(self.store.read("test/v1/runs/run1/results.json"))["requests"]
            self.assertEqual(records[0]["error_type"], "cancelled")
            self.assertFalse(output.exists())
        with patch.object(b, "get_store", return_value=self.store), patch.object(b, "request", request), patch.object(b, "CHECKPOINT_SECONDS", .01):
            asyncio.run(scenario())

    def test_failed_job_archive_keeps_retryable_terminal_snapshot(self):
        output = self.output()
        (output / "results.json").write_bytes(b"{}")
        entry = dict(state=dict(job_id="job1", path="test/v1/runs/run1", status="finished", has_results=True), run=None, task=None)
        with patch.object(server, "RESULTS", self.root), patch.object(server, "get_store", return_value=self.store):
            self.client.fail = True
            asyncio.run(server.archive_job(entry, output))
            self.assertTrue(entry["state"]["storage_pending"])
            self.assertFalse(entry["state"]["has_results"])
            self.client.fail = False
            jobs = self.store.retry_pending()
            self.assertEqual(jobs[0]["status"], "finished")
            self.assertTrue(jobs[0]["has_results"])
            self.assertNotIn("storage_pending", jobs[0])

    def test_web_run_and_restart_without_local_results(self):
        from fastapi.testclient import TestClient
        cfg = config()
        cfg.update(name="test", repetitions=1, warmup=0)
        cfg["models"][0].update(base_url=server.default_base(), api_key_env="DASHSCOPE_API_KEY")
        prepared = prepared_without_tokenizer(cfg)
        calls = []

        async def request(client, prompt, record, event):
            calls.append(record)
            record.update(success=True)

        real_run = b.BenchmarkRun
        def factory(*args, **kwargs):
            return real_run(*args, **kwargs, client_factory=DummyClient)

        with patch.object(server, "RESULTS", self.root), patch.object(server, "run_registry", {}), \
             patch.object(server, "active_state", None), patch.object(server, "active_task", None), \
             patch.object(server, "get_store", return_value=self.store), patch.object(b, "get_store", return_value=self.store), \
             patch.object(b, "prepare", return_value=prepared), patch.object(b, "request", request), \
             patch.object(server, "BenchmarkRun", factory), patch.dict(os.environ, {"DASHSCOPE_API_KEY": "private-model-key"}):
            with TestClient(server.app) as web:
                response = web.post("/api/runs", json={"config": cfg, "mode": "full"})
                self.assertEqual(response.status_code, 200, response.text)
                job = response.json()
                async def finish():
                    await server.run_registry[job["job_id"]]["task"]
                web.portal.call(finish)
                self.assertEqual(web.get("/api/jobs").json()[0]["status"], "finished")
                self.assertFalse((self.root / job["path"]).exists())
                self.assertEqual(len(web.get("/api/runs").json()), 1)
                self.assertEqual(web.get("/api/artifact/" + job["path"] + "/report.html").status_code, 200)
                self.assertNotIn(b"private-model-key", b"".join(self.client.objects.values()))
            server.run_registry.clear()
            with TestClient(server.app) as web:
                jobs = web.get("/api/jobs").json()
                self.assertEqual(len(jobs), 1)
                self.assertTrue(jobs[0]["has_results"])
                response = web.post("/api/runs", json={"config": cfg, "mode": "incremental"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["plan"]["pending_calls"], 0)
            self.assertEqual(len(calls), 1)
