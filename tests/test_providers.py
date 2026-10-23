"""Multiple source bindings, credential isolation and resumable provider identities."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
import openai
from fastapi.testclient import TestClient

import benchmark
from connections import bind_providers
from report import build_report
import server
from test_benchmark import config, ROOT, prepared_without_tokenizer


def provider_config():
    cfg = config()
    cfg.update(ui_version=3, repetitions=1, concurrencies=[1], providers=[
        dict(id='a', name='Provider A', mode='custom', base_url='https://a.test/v1'),
        dict(id='b', name='Provider B', mode='custom', base_url='https://b.test/v1'),
    ])
    first = cfg['models'][0]
    first.update(id='a-model', provider_id='a')
    second = deepcopy(first)
    second.update(id='b-model', provider_id='b')
    cfg['models'].append(second)
    return cfg


def credentials():
    return [dict(id='a', api_key='secret-source-a'), dict(id='b', api_key='secret-source-b')]


class ProviderApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.cfg = provider_config()
        self.body = dict(config=self.cfg, connections=credentials(), mode='full')
        for item in [patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'server-secret', 'DASHSCOPE_BASE_URL': 'https://sg.test/v1'}),
                     patch.object(server, 'CONFIG', self.directory/'config.yaml'),
                     patch.object(server, 'RESULTS', self.directory/'results'),
                     patch.object(server, 'run_registry', {}), patch.object(server, 'active_task', None), patch.object(server, 'active_state', None)]:
            item.start(); self.addCleanup(item.stop)
        self.client = TestClient(server.app)

    def test_same_model_is_bound_to_each_source_and_saved_without_secrets(self):
        response = self.client.post('/api/plan', json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        groups = response.json()['groups_detail']
        self.assertEqual(len(groups), 2)
        self.assertEqual(len({g['group_id'] for g in groups}), 2)
        self.assertEqual({g['model'] for g in groups}, {'test-model'})
        self.assertEqual([(g['provider_id'], g['provider_name'], g['base_url']) for g in groups],
                         [('a', 'Provider A', 'https://a.test/v1/'), ('b', 'Provider B', 'https://b.test/v1/')])
        self.assertFalse((self.directory/'results').exists())
        self.assertEqual(self.client.put('/api/config', json=self.body).status_code, 200)
        loaded = self.client.get('/api/config')
        self.assertEqual(loaded.json()['config']['providers'], self.cfg['providers'])
        for secret in ['secret-source-a', 'secret-source-b', 'server-secret']:
            self.assertNotIn(secret, response.text + loaded.text + (self.directory/'config.yaml').read_text())

    def test_save_sources_before_choosing_models_or_entering_keys(self):
        self.cfg['models'] = []
        self.body['connections'] = []
        response = self.client.put('/api/config', json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/config').json()['config']['models'], [])
        self.assertEqual(self.client.post('/api/runs', json=self.body).status_code, 400)

    def test_missing_source_key_prevents_any_start(self):
        self.body['connections'] = credentials()[:1]
        with patch.object(server, 'BenchmarkRun') as runner:
            response = self.client.post('/api/runs', json=self.body)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn('Provider B', response.text)
            runner.assert_not_called()
        self.assertIsNone(server.active_task)
        self.assertFalse((self.directory/'results').exists())

    def test_invalid_provider_references_and_credential_lists(self):
        for mutation in [
            lambda body: body['config']['models'][0].update(provider_id='missing'),
            lambda body: body['config']['providers'].append(deepcopy(body['config']['providers'][0])),
            lambda body: body['connections'].append(dict(id='a', api_key='duplicate')),
            lambda body: body['connections'].append(dict(id='unknown', api_key='unknown')),
            lambda body: body['config']['providers'][0].update(api_key='embedded-secret'),
            lambda body: body['config']['providers'][0].update(mode='default', base_url='https://evil.test/v1'),
        ]:
            body = deepcopy(self.body);mutation(body)
            with self.subTest(body_type=list(body)):
                response = self.client.post('/api/runs', json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertNotIn('server-secret', response.text)

    def test_unused_source_does_not_require_a_key(self):
        self.cfg['models'] = self.cfg['models'][:1]
        self.body['connections'] = credentials()[:1]
        response = self.client.post('/api/plan', json=self.body)
        self.assertEqual(response.status_code, 200, response.text)

    def test_one_click_start_validates_and_passes_all_bound_credentials(self):
        captured = []
        class Run:
            def __init__(inner, cfg, root, output, prepared, credentials):
                captured.append((deepcopy(cfg), credentials.copy()))
                inner.output, inner.session_id, inner.credentials = output, 'test-session', credentials
            async def run(inner):
                await asyncio.sleep(.01)
        with patch.object(server, 'BenchmarkRun', Run), TestClient(server.app) as client:
            response = client.post('/api/runs', json=self.body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['plan']['pending_calls'], 2)
        cfg, keys = captured[0]
        self.assertEqual([keys[m['api_key_env']] for m in cfg['models']], ['secret-source-a', 'secret-source-b'])
        self.assertEqual([m['base_url'] for m in cfg['models']], ['https://a.test/v1', 'https://b.test/v1'])


class ProviderIdentityTests(unittest.TestCase):
    def test_legacy_model_gains_source_metadata_without_losing_resume_identity(self):
        cfg = config()
        original = prepared_without_tokenizer(cfg)[0][2]['group_id']
        cfg['models'][0].update(provider_id='migrated', provider_name='Existing source')
        self.assertEqual(original, prepared_without_tokenizer(cfg)[0][2]['group_id'])

    def test_rename_is_display_only_and_key_or_endpoint_change_is_new_work(self):
        cfg = provider_config()
        bind_providers(cfg, credentials(), 'https://sg.test/v1', True)
        initial = prepared_without_tokenizer(cfg)
        original = [g['group_id'] for _, _, g in initial]
        cfg['providers'][0]['name'] = 'Renamed A'
        bind_providers(cfg, credentials(), 'https://sg.test/v1', True)
        renamed = prepared_without_tokenizer(cfg)
        self.assertEqual(original, [g['group_id'] for _, _, g in renamed])
        self.assertEqual(renamed[0][2]['provider_name'], 'Renamed A')
        cfg['providers'][0]['base_url'] = 'https://new-a.test/v1'
        bind_providers(cfg, credentials(), 'https://sg.test/v1', True)
        moved = prepared_without_tokenizer(cfg)
        self.assertNotEqual(original[0], moved[0][2]['group_id'])
        self.assertEqual(original[1], moved[1][2]['group_id'])
        keys = credentials();keys[1]['api_key'] = 'changed-account-b'
        bind_providers(cfg, keys, 'https://sg.test/v1', True)
        self.assertNotEqual(original[1], prepared_without_tokenizer(cfg)[1][2]['group_id'])
        jobs = benchmark.plan_jobs(initial, [dict(case_id=original[0]+'-0001', phase='measured', success=True)], 1)
        self.assertEqual(jobs, {original[0]: [], original[1]: [1]})


class MultiProviderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_model_reaches_distinct_endpoints_and_exports_stay_separate(self):
        cfg = provider_config()
        keys = bind_providers(cfg, credentials(), 'https://sg.test/v1', True)
        seen = []
        def handler(request):
            seen.append((request.url.host, request.headers['authorization'], json.loads(request.content)['model']))
            event = {'id': 'mock', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'test-model',
                     'choices': [{'index': 0, 'delta': {'content': 'hello'}, 'finish_reason': 'stop'}]}
            return httpx.Response(200, text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
        def factory(**kwargs):
            return openai.AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with TemporaryDirectory() as directory:
            output = Path(directory)/'test/v1/runs/session'
            run = benchmark.BenchmarkRun(cfg, ROOT, output, credentials=keys, client_factory=factory)
            await run.run()
            self.assertEqual(sorted(seen), [('a.test', 'Bearer secret-source-a', 'test-model'), ('b.test', 'Bearer secret-source-b', 'test-model')])
            data = json.loads((output/'results.json').read_text())
            self.assertEqual(data['totals']['successful_cases'], 2)
            self.assertEqual({g['provider_name'] for g in data['summary']}, {'Provider A', 'Provider B'})
            build_report(output/'results.json')
            self.assertIn('Provider A', (output/'report.html').read_text())
            for path in output.iterdir():
                for secret in keys.values():self.assertNotIn(secret, path.read_text())
            seen.clear()
            # Resume only B by retaining only A's successful measured record.
            records = [r for r in benchmark.read_records(output/'requests.jsonl') if r['provider_id']=='a']
            (output/'requests.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
            await benchmark.BenchmarkRun(cfg, ROOT, output, credentials=keys, client_factory=factory).run()
            self.assertEqual(seen, [('b.test', 'Bearer secret-source-b', 'test-model')])
