"""Background jobs and retry accounting; all model requests are simulated."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import benchmark as b
from test_benchmark import config, prepared_without_tokenizer, ROOT, HAS_SDK


class DummyClient:
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


@unittest.skipUnless(HAS_SDK, "SDK dependencies required")
class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def run_scenario(self, outcomes, budget=2):
        cfg = config();cfg.update(repetitions=1, max_retries=budget)
        calls = []
        async def request(client, prompt, record, event):
            error, status = outcomes[min(len(calls),len(outcomes)-1)]
            calls.append(record)
            record.update(success=error is None, error_type=error, status_code=status)
        with TemporaryDirectory() as directory, patch.object(b, 'request', request):
            run = b.BenchmarkRun(cfg, ROOT, Path(directory)/'v1/runs/test', prepared_without_tokenizer(cfg),
                                 client_factory=DummyClient, credentials={'BENCHMARK_TEST_KEY':'fake'})
            await run.run()
            data = json.loads((run.output/'results.json').read_text())
        return run, calls, data

    async def test_transient_retry_succeeds_without_inflating_progress(self):
        run, calls, data = await self.run_scenario([('rate_limit',429),(None,200)])
        self.assertEqual([r['attempt'] for r in calls], [1,2])
        self.assertEqual([r['case_complete'] for r in calls], [False,True])
        self.assertEqual(run.progress['completed'],1)
        self.assertEqual(run.progress['succeeded'],1)
        self.assertEqual(run.progress['failed'],0)
        self.assertEqual(run.progress['retries'],1)
        self.assertEqual(data['totals']['requests'],2)
        self.assertEqual(data['totals']['successful_cases'],1)

    async def test_exhaustion_and_non_retryable_auth(self):
        run, calls, _ = await self.run_scenario([('timeout',None)])
        self.assertEqual(len(calls),3);self.assertEqual(run.progress['failed'],1)
        for status in [400,401,403,404,422]:
            run, calls, _ = await self.run_scenario([('http_error',status)])
            self.assertEqual(len(calls),1);self.assertEqual(run.progress['retries'],0)
        run, calls, _ = await self.run_scenario([('http_error',503),(None,200)])
        self.assertEqual(len(calls),2);self.assertEqual(run.progress['succeeded'],1)

    async def test_cancellation_during_backoff_does_not_complete_case(self):
        cfg=config();cfg.update(repetitions=1,max_retries=2)
        async def request(client,prompt,record,event):record.update(error_type='connection_error')
        with TemporaryDirectory() as directory, patch.object(b,'request',request):
            run=b.BenchmarkRun(cfg,ROOT,Path(directory)/'v1/runs/test',prepared_without_tokenizer(cfg),client_factory=DummyClient,credentials={'BENCHMARK_TEST_KEY':'fake'})
            task=asyncio.create_task(run.run())
            while not run.progress['retrying']:await asyncio.sleep(.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(run.progress['completed'],0)
            self.assertEqual(run.progress['active'],0);self.assertEqual(run.progress['retrying'],0)
            self.assertEqual(run.progress['attempts'],1)
            self.assertFalse((run.output/'.run.lock').exists())

    def test_retry_validation(self):
        for value in [-1,6,True,1.5,'2']:
            cfg=config();cfg['max_retries']=value
            with self.assertRaises(ValueError):b.validate_config(cfg)


@unittest.skipUnless(HAS_SDK, "SDK dependencies required")
class JobTests(unittest.TestCase):
    def test_two_jobs_independent_stop_progress_save_and_restore(self):
        import server
        from fastapi.testclient import TestClient
        cfg=config();cfg.update(repetitions=1,max_retries=2)
        cfg['models'][0].update(base_url=server.default_base(),api_key_env='DASHSCOPE_API_KEY')
        prepared=prepared_without_tokenizer(cfg)
        async def request(client,prompt,record,event):
            try:await asyncio.sleep(60)
            except asyncio.CancelledError:
                record.update(error_type='cancelled');raise
        real_run=b.BenchmarkRun
        def factory(*args,**kwargs):return real_run(*args,**kwargs,client_factory=DummyClient)
        with TemporaryDirectory() as directory, patch.object(server,'RESULTS',Path(directory)/'results'), patch.object(server,'CONFIG',Path(directory)/'config.yaml'), patch.object(server,'run_registry',{}), patch.object(server,'active_state',None), patch.object(server,'active_task',None), patch.dict(os.environ,{'DASHSCOPE_API_KEY':'fake-secret'}), patch.object(b,'prepare',return_value=prepared), patch.object(b,'request',request), patch.object(server,'BenchmarkRun',factory):
            with TestClient(server.app) as client:
                first=client.post('/api/runs',json={'config':cfg,'mode':'full'});self.assertEqual(first.status_code,200,first.text)
                second=client.post('/api/runs',json={'config':cfg,'mode':'full'});self.assertEqual(second.status_code,200,second.text)
                a,c=first.json(),second.json();self.assertNotEqual(a['path'],c['path'])
                jobs=client.get('/api/jobs').json();self.assertEqual(len(jobs),2)
                self.assertTrue(all(job['status']=='running' for job in jobs))
                self.assertEqual(client.put('/api/config',json={'config':cfg}).status_code,200)
                conflict=client.post('/api/runs',json={'config':cfg,'mode':'incremental'})
                self.assertEqual(conflict.status_code,409)
                self.assertEqual(client.post(f"/api/jobs/{a['job_id']}/stop").status_code,200)
                jobs={j['job_id']:j for j in client.get('/api/jobs').json()}
                self.assertEqual(jobs[a['job_id']]['status'],'cancelled')
                self.assertEqual(jobs[c['job_id']]['status'],'running')
                self.assertEqual(jobs[a['job_id']]['progress']['completed'],0)
                self.assertEqual(client.get('/api/status').json()['job_id'],c['job_id'])
                self.assertEqual(client.post('/api/jobs/no-such-job/stop').status_code,404)
                self.assertNotIn('fake-secret',client.get('/api/jobs').text)
            # Lifespan cancels every outstanding task; final job metadata survives restart.
            server.run_registry.clear()
            with TestClient(server.app) as client:
                jobs=client.get('/api/jobs').json()
                self.assertEqual(len(jobs),2)
                self.assertTrue(all(j['status']=='cancelled' for j in jobs))
                self.assertTrue(all(j.get('has_results') for j in jobs))
            for path in (server.RESULTS/'.jobs').glob('*.json'):
                self.assertNotIn('fake-secret',path.read_text())

    def test_restart_marks_crashed_job_interrupted(self):
        import server
        from fastapi.testclient import TestClient
        with TemporaryDirectory() as directory, patch.object(server,'RESULTS',Path(directory)), patch.object(server,'run_registry',{}):
            output=Path(directory)/'x/v1/runs/example';output.mkdir(parents=True)
            (output/'.run.lock').write_text('123')
            entry={'state':dict(job_id='example',session_id='example',status='running',path='x/v1/runs/example'), 'run':None,'task':None}
            server.persist_job(entry)
            with TestClient(server.app) as client:
                job=client.get('/api/jobs').json()[0]
                self.assertEqual(job['status'],'interrupted')
                self.assertEqual(job['progress']['completed'],0)
                self.assertFalse((output/'.run.lock').exists())
