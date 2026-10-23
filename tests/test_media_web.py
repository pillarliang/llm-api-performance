import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

import httpx
from PIL import Image

import benchmark as b
import media_web as m
import server
import visual_preprocess as v
from test_visual_preprocess import MemoryStore
from test_benchmark import config


class MediaWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = MemoryStore()
        self.patches = [patch.object(m, 'store', return_value=self.storage),
                        patch.object(m, 'default_store', return_value=self.storage),
                        patch.object(m, 'WORK', Path(self.temp.name)),
                        patch.object(m, 'lifecycle', return_value=SimpleNamespace(begin_preparation=AsyncMock(), end_preparation=AsyncMock()))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        await self.client.get('/api/media')

    async def upload_image(self, size=(1024, 768), name='example.jpg'):
        buffer = io.BytesIO()
        Image.new('RGB', size, 'red').save(buffer, 'JPEG')
        return await self.client.post('/api/media/upload', headers={'X-File-Name': name}, content=buffer.getvalue())

    async def test_upload_inspects_original_and_rejects_upscale_options(self):
        response = await self.upload_image()
        self.assertEqual(response.status_code, 200, response.text)
        asset = response.json()
        self.assertEqual(asset['info']['edges'], [512, 1024])
        response = await self.client.post('/api/media/prepare', json={'assets':[{'id':asset['id'], 'edges':[2048]}]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(m.TASKS)

    async def test_uploads_are_isolated_and_paths_cannot_be_supplied(self):
        asset = (await self.upload_image()).json()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as other:
            result = (await other.get('/api/media')).json()
            self.assertEqual(result['assets'], [])
            denied = await other.post('/api/media/prepare', json={'assets':[{'id':asset['id'],'edges':[512]}]})
            self.assertEqual(denied.status_code, 400)
        traversal = await self.client.post('/api/media/prepare', json={'assets':[{'id':'../secret','edges':[512]}]})
        self.assertEqual(traversal.status_code, 400)
        result = (await self.client.get('/api/media')).json()
        self.assertEqual(len(result['assets']), 1)

    async def test_prepare_only_selected_edge_and_create_cases_for_benchmark(self):
        asset = (await self.upload_image()).json()
        response = await self.client.post('/api/media/prepare', json={'assets':[{'id':asset['id'],'edges':[512]}]})
        self.assertEqual(response.status_code, 200, response.text)
        job_id = response.json()['id']
        for _ in range(200):
            state = (await self.client.get('/api/media/jobs/'+job_id)).json()
            if state['status'] not in ('queued','processing'):
                break
            await asyncio.sleep(.05)
        self.assertEqual(state['status'], 'ready', state)
        prepared = [k for k in self.storage.data if '/prepared/images/' in k]
        self.assertEqual(len(prepared), 1)
        self.assertIn('/long-512/', prepared[0])
        request = httpx.Request('GET','http://test')
        from starlette.requests import Request
        cookie = self.client.cookies.get(m.COOKIE)
        scope = {'type':'http','scheme':'http','server':('test',80),'root_path':'','path':'/','headers':[(b'cookie',f'{m.COOKIE}={cookie}'.encode())]}
        cfg = await m.resolve_selection(Request(scope), {'media':{'source':'upload','job_id':job_id}}, config())
        groups = b.prepare(cfg, Path('.'))
        self.assertTrue(groups)
        self.assertEqual(groups[0][2]['modality'], 'image')
        self.assertEqual(groups[0][2]['media_case']['objects'][0]['width'], 512)
        self.assertNotIn('signature', json.dumps(groups))
        old_writes = list(self.storage.writes)
        row = next(json.loads(data) for key,data in self.storage.data.items() if '/prepared-index/' in key)
        source_asset = next(json.loads(data) for key,data in self.storage.data.items() if '/uploads/' in key)
        base = source_asset['key'].split('/originals/')[0]
        await asyncio.to_thread(m.generate, self.storage, base, source_asset, {'edges':[512]})
        self.assertEqual(self.storage.writes, old_writes)

    async def test_upload_validation_origin_size_type_and_content(self):
        result = await self.client.post('/api/media/upload', headers={'origin':'https://other','X-File-Name':'x.jpg'}, content=b'x')
        self.assertEqual(result.status_code, 403)
        result = await self.client.post('/api/media/upload', headers={'X-File-Name':'x.jpg'}, content=b'not image')
        self.assertEqual(result.status_code, 400)
        result = await self.client.post('/api/media/upload', headers={'X-File-Name':'x.exe'}, content=b'x')
        self.assertEqual(result.status_code, 400)
        result = await self.upload_image((256,256))
        self.assertEqual(result.status_code, 400)

    async def test_default_unavailable_and_untrusted_visual_cases_rejected(self):
        from starlette.requests import Request
        with self.assertRaises(Exception) as exc:
            await m.resolve_selection(Request({'type':'http','headers':[]}), {'media':{'source':'default'}}, config())
        self.assertEqual(exc.exception.status_code,400)
        cfg = config()
        cfg['visual_cases'] = [{'objects':[{'key':'private/file'}]}]
        with self.assertRaises(ValueError):
            server.checked_config(__import__('yaml').safe_dump(cfg))

    async def test_private_jobs_and_artifacts_are_hidden_from_other_browser(self):
        import hashlib
        from types import SimpleNamespace
        own = hashlib.sha256(self.client.cookies.get(m.COOKIE).encode()).hexdigest()
        state = dict(job_id='private-job', path='test/v1/runs/one', status='finished', media_owner=own)
        entry = dict(state=state, task=None, run=None)
        summary = json.dumps({'settings':{'media_owner':own}}).encode()
        fake_store = SimpleNamespace(remote=True, read=lambda key: summary if key.endswith('results.json') else b'private answer')
        with patch.object(server, 'run_registry', {'private-job':entry}), patch.object(server, 'get_store', return_value=fake_store):
            self.assertEqual(len((await self.client.get('/api/jobs')).json()), 1)
            self.assertEqual((await self.client.get('/api/artifact/test/v1/runs/one/results.json')).status_code, 200)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as other:
                self.assertEqual((await other.get('/api/jobs')).json(), [])
                self.assertEqual((await other.get('/api/artifact/test/v1/runs/one/requests.jsonl')).status_code, 404)
                self.assertEqual((await other.post('/api/jobs/private-job/stop')).status_code, 404)

    async def test_real_sdk_serializes_visual_message_and_streams_answer(self):
        import openai
        case = {'group':'image', 'modality':'image', 'prompt':'describe','case_id':'abc123',
                'objects':[{'key':'private/image.jpg','sha256':'a','bytes':1,'width':512,'height':288}]}
        cfg = config()
        cfg['timeout_seconds'] = 10
        cfg['visual_cases'] = [case]
        group = b.prepare(cfg, Path('.'))[0][2]
        record = b.new_record(group,1,'measured','session',1)
        content = [{'type':'image_url','image_url':{'url':'https://media.test/image.jpg?secret-signature'}},
                   {'type':'text','text':'describe'}]
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body['messages'][0]['content'], content)
            chunks = [dict(choices=[dict(index=0,delta={'content':'answer'},finish_reason=None)]),
                      dict(choices=[dict(index=0,delta={},finish_reason='stop')])]
            return httpx.Response(200,headers={'content-type':'text/event-stream'},text=''.join(
                'data: '+json.dumps(dict(id='mock',object='chat.completion.chunk',created=1,model='test',**c))+'\n\n'
                for c in chunks)+'data: [DONE]\n\n')
        async with openai.AsyncOpenAI(api_key='fake',base_url='https://test/v1',
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            with patch.object(m,'model_content',return_value=content):
                await b.request(client,case['prompt'],record)
        self.assertTrue(record['success'], record.get('error_type'))
        self.assertNotIn('secret-signature',json.dumps(record))

    async def test_signed_media_content_not_in_persistent_group_identity(self):
        case = {'group':'图片', 'modality':'image', 'prompt':'描述图片','case_id':'abc12345',
                'objects':[{'key':'users/test/prepared/x.jpg','sha256':'a','bytes':1,'width':512,'height':288}]}
        cfg = config()
        cfg['visual_cases'] = [case]
        group = b.prepare(cfg, Path('.'))[0][2]
        record = b.new_record(group, 1,'measured','session',1)
        with patch.object(m,'model_content', side_effect=RuntimeError('signing failed')):
            await b.request(None,case['prompt'],record)
        self.assertEqual(record['error_type'], 'media_preparation_error')
        self.assertNotIn('signing failed',json.dumps(record))


class DynamicVideoTests(unittest.TestCase):
    def test_short_360p_video_provides_only_feasible_tiers_and_generates_them(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'short.mp4'
            v.run_tool(['ffmpeg','-v','error','-f','lavfi','-i','color=size=640x360:rate=2:duration=10',
                        '-c:v','libx264','-threads','2',str(source)])
            info = m.inspect_media(source,'video')
            self.assertEqual(info['heights'],[360])
            self.assertEqual(info['durations'],[10])
            self.assertEqual(info['frame_counts'],[])
            output = root / 'out'
            output.mkdir()
            _, artifacts = v.prepare_video(source, output, [(10,360)], [])
            self.assertEqual(len(artifacts),1)
            self.assertEqual(artifacts[0]['width'],640)
            self.assertAlmostEqual(artifacts[0]['duration_seconds'],10,places=2)

class InputAuditTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = MediaWebTests.asyncSetUp
    upload_image = MediaWebTests.upload_image
    async def test_malformed_ids_and_media_source_return_400(self):
        for value in (None, 123, [], {}):
            result=await self.client.post('/api/media/prepare',json={'assets':[{'id':value,'edges':[512]}]})
            self.assertEqual(result.status_code,400,result.text)
        from starlette.requests import Request
        with self.assertRaises(Exception) as exc:
            await m.resolve_selection(Request({'type':'http','headers':[]}),{'media':{'source':'unknown'}},config())
        self.assertEqual(exc.exception.status_code,400)

    async def test_duplicate_multimage_counts_do_not_duplicate_cases(self):
        assets=[(await self.upload_image(name=f'{i}.jpg')).json() for i in range(2)]
        response=await self.client.post('/api/media/prepare',json={'assets':[{'id':a['id'],'edges':[1024]} for a in assets],'image_counts':[2,2]})
        self.assertEqual(response.status_code,200,response.text)
        job=response.json()
        for _ in range(200):
            state=(await self.client.get('/api/media/jobs/'+job['id'])).json()
            if state['status'] not in ('queued','processing'):break
            await asyncio.sleep(.05)
        self.assertEqual(state['status'],'ready',state)
        self.assertEqual(state['case_count'],3)
