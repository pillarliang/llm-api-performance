import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import openai
from fastapi.testclient import TestClient
import benchmark
import server
from catalog import ModelCatalog
from connections import normalize_base, resolve_connection


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.yaml'
        for item in [patch.dict(os.environ, {'DASHSCOPE_API_KEY': 'server-secret', 'DASHSCOPE_BASE_URL': 'https://sg.test/v1'}),
                     patch.object(server, 'CONFIG', self.config_path), patch.object(server, 'RESULTS', self.root/'results'),
                     patch.object(server, 'active_task', None), patch.object(server, 'active_state', None)]:
            item.start(); self.addCleanup(item.stop)
        self.client = TestClient(server.app)
        self.config = self.client.get('/api/config').json()['config']
        self.connection = {'mode': 'custom', 'base_url': 'https://custom.test/proxy/v1', 'api_key': 'user-secret'}

    def test_default_metadata_and_destination_binding(self):
        response = self.client.get('/api/config')
        self.assertNotIn('server-secret', response.text)
        self.assertEqual(response.json()['connection'], {'mode': 'default', 'base_url': 'https://sg.test/v1', 'configured': True})
        response = self.client.post('/api/plan', json={'config': self.config, 'connection': {'mode': 'default', 'base_url': 'https://evil.test/v1'}})
        self.assertEqual(response.status_code, 400)
        self.config['models'][0]['base_url'] = 'https://evil.test/v1'
        self.config['models'][0].pop('base_url_env', None)
        self.assertEqual(self.client.post('/api/plan', json={'config': self.config}).status_code, 400)

    def test_custom_plan_save_and_reload_exclude_key(self):
        self.config['ui_version'] = 2
        body = {'config': self.config, 'connection': self.connection}
        response = self.client.post('/api/plan', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('user-secret', response.text)
        groups = response.json()['groups_detail']
        self.assertTrue(all(g['base_url'] == 'https://custom.test/proxy/v1/' for g in groups))
        self.assertFalse((self.root/'results').exists())
        response = self.client.put('/api/config', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('user-secret', self.config_path.read_text())
        loaded = self.client.get('/api/config')
        self.assertNotIn('user-secret', loaded.text)
        self.assertEqual(loaded.json()['config']['models'][0]['base_url'], self.connection['base_url'])
        changed = dict(self.connection, api_key='different-user-secret')
        other = self.client.post('/api/plan', json={'config': self.config, 'connection': changed}).json()['groups_detail']
        self.assertNotEqual(groups[0]['group_id'], other[0]['group_id'])

    def test_custom_catalog_uses_supplied_key_without_shared_cache(self):
        service = AsyncMock()
        service.get.return_value = {'count': 1, 'models': [{'id': 'custom-model'}]}
        with patch.object(server, 'ModelCatalog', return_value=service) as factory:
            response = self.client.post('/api/models', json={'connection': self.connection})
        self.assertEqual(response.status_code, 200)
        factory.assert_called_once_with(None)
        service.get.assert_awaited_once_with('https://custom.test/proxy/v1', 'user-secret', refresh=False)
        self.assertNotIn('user-secret', response.text)
        self.assertEqual(os.environ['DASHSCOPE_API_KEY'], 'server-secret')

    def test_invalid_connections_do_not_fall_back_to_server_key(self):
        for connection in [None, [], {'mode': 'bad'}, dict(self.connection, api_key=''),
                           dict(self.connection, base_url='https://name:password@example.test/v1'),
                           dict(self.connection, base_url='https://example.test/v1?key=secret')]:
            with self.subTest(connection_type=type(connection).__name__):
                response = self.client.post('/api/models', json={'connection': connection})
                self.assertEqual(response.status_code, 400)
                self.assertNotIn('server-secret', response.text)

    def test_sg_alias_and_custom_paths(self):
        self.assertEqual(normalize_base('https://proxy.test/custom/v1/'), 'https://proxy.test/custom/v1')
        self.assertEqual(normalize_base('ws.ap-southeast-1.maas.aliyuncs.com/api/v1'), 'https://ws.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1')
        with patch.dict(os.environ, {'MODELSTUDIO_HOST_SG': 'ws.ap-southeast-1.maas.aliyuncs.com', 'MODELSTUDIO_API_KEY_SG': 'sg-alias-secret'}, clear=True):
            base, name, key = resolve_connection({}, 'https://fallback.test/v1')
            self.assertEqual(name, 'MODELSTUDIO_API_KEY_SG')
            self.assertEqual(key, 'sg-alias-secret')
            self.assertTrue(base.endswith('/compatible-mode/v1'))

    def test_start_run_passes_request_credentials_to_runner(self):
        captured = []
        class Run:
            def __init__(inner, config, root, output, prepared, credentials):
                captured.append((config, credentials.copy()))
                inner.output, inner.session_id, inner.credentials = output, 'test-session', credentials
            async def run(inner):
                await asyncio.sleep(.01)
        with patch.object(server, 'BenchmarkRun', Run), TestClient(server.app) as client:
            response = client.post('/api/runs', json={'config': self.config, 'connection': self.connection})
            self.assertEqual(response.status_code, 200, response.text)
        config, credentials = captured[0]
        self.assertEqual(set(credentials.values()), {'user-secret'})
        self.assertEqual(credentials[config['models'][0]['api_key_env']], 'user-secret')
        self.assertNotIn('user-secret', json.dumps(config))


class RuntimeCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_stream_uses_custom_key_and_artifacts_exclude_it(self):
        from test_benchmark import config, ROOT
        cfg = config();cfg['repetitions'] = 1
        seen = []
        def handler(request):
            seen.append((request.headers['authorization'], str(request.url)))
            event = {'id': 'mock', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'test-model',
                     'choices': [{'index': 0, 'delta': {'content': 'hello'}, 'finish_reason': 'stop'}]}
            return httpx.Response(200, text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n', headers={'content-type':'text/event-stream'})
        def factory(**kwargs):
            return openai.AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with TemporaryDirectory() as directory:
            output = Path(directory)/'test/v1/runs/session'
            run = benchmark.BenchmarkRun(cfg, ROOT, output, client_factory=factory, credentials={'BENCHMARK_TEST_KEY': 'only-this-run-key'})
            await run.run()
            self.assertEqual(seen, [('Bearer only-this-run-key', 'https://example.test/v1/chat/completions')])
            for file in output.iterdir():
                self.assertNotIn('only-this-run-key', file.read_text())
            self.assertEqual(json.loads((output/'results.json').read_text())['totals']['successful_cases'], 1)

    async def test_request_local_catalog_does_not_write_a_cache_file(self):
        def handler(request):
            return httpx.Response(200, json={'object':'list', 'data':[{'id':'mine','object':'model','created':1,'owned_by':'me'}]})
        def factory(**kwargs):
            return openai.AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        service = ModelCatalog(None, client_factory=factory)
        data = await service.get('https://custom.test/v1', 'key')
        self.assertEqual(data['count'], 1)
        self.assertIsNone(service.cache_file)
