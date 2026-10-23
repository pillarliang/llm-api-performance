import asyncio
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
from PIL import Image

import media_web as m
from local_media import LocalMediaStore
import server
import visual_preprocess as v


class LocalMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        p = patch.object(m, 'WORK', self.work)
        p.start()
        self.addCleanup(p.stop)
        # Uploading and preparing personal data must still work while OSS is unavailable.
        p = patch.object(m, 'default_store', side_effect=RuntimeError('OSS unavailable'))
        self.cloud = p.start()
        self.addCleanup(p.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        response = await self.client.get('/api/media')
        self.assertEqual(response.status_code,200)
        self.assertIsNotNone(response.json()['default_error'])

    async def test_upload_prepare_fetch_and_reuse_without_oss(self):
        data = io.BytesIO()
        Image.new('RGB',(1024,768),'green').save(data,'JPEG')
        response = await self.client.post('/api/media/upload',headers={'X-File-Name':'x.jpg'},content=data.getvalue())
        self.assertEqual(response.status_code,200,response.text)
        asset = response.json()
        response = await self.client.post('/api/media/prepare',json={'assets':[{'id':asset['id'],'edges':[512]}]})
        self.assertEqual(response.status_code,200,response.text)
        job_id = response.json()['id']
        for _ in range(200):
            job = (await self.client.get('/api/media/jobs/'+job_id)).json()
            if job['status'] not in ('queued','processing'):break
            await asyncio.sleep(.05)
        self.assertEqual(job['status'],'ready',job)
        storage = m.store()
        cases = storage.get_json(asset['key'].split('/originals/')[0]+'/cases/'+job_id+'.json')
        case = cases[0] | {'media_origin':'http://test'}
        # Signing and serving personal media never instantiate an OSS client.
        self.cloud.reset_mock()
        content = m.model_content(case,30)
        self.cloud.assert_not_called()
        url = content[0]['image_url']['url']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as model:
            response = await model.get(url)
            self.assertEqual(response.status_code,200,response.text)
            with Image.open(io.BytesIO(response.content)) as image:
                self.assertEqual(image.size,(512,384))
            response = await model.get(url,headers={'Range':'bytes=0-9'})
            self.assertEqual(response.status_code,206)
            self.assertEqual(len(response.content),10)
            self.assertEqual((await model.get(url.replace('signature=','signature=bad'))).status_code,404)
            parsed = urlparse(url)
            self.assertEqual((await model.get(parsed.path)).status_code,422)
        regenerated = await asyncio.to_thread(m.generate, storage, asset['key'].split('/originals/')[0],asset,{'edges':[512]})
        self.assertEqual(len(regenerated['artifacts']),1)
        self.assertTrue(storage.path(asset['key']).is_file())
        self.assertEqual(len(storage.objects(asset['key'].split('/originals/')[0]+'/prepared/')),1)

    async def test_signature_expiry_path_binding_and_secret_survives_restart(self):
        storage = m.store()
        key = 'users/'+'a'*64+'/vision/v1/prepared/videos/360p/10s/id/hash.mp4'
        storage.put_bytes(key,b'video','video/mp4')
        expiry = int(time.time())+60
        signature = storage.signature(key,expiry)
        self.assertTrue(LocalMediaStore(storage.root).verify(key,expiry,signature))
        self.assertFalse(storage.verify(key+'x',expiry,signature))
        expired = int(time.time())-1
        self.assertFalse(storage.verify(key,expired,storage.signature(key,expired)))
        for path in ('../secrets','users/../../secrets','/etc/passwd','users\\secrets'):
            with self.assertRaises(ValueError):storage.path(path)
        # Even a signed arbitrary local path must not be served by the media endpoint.
        storage.put_bytes('users/private.json',b'{}')
        url = storage.signed_url('http://test','users/private.json',60)
        self.assertEqual((await self.client.get(url)).status_code,404)

    async def test_default_model_inputs_still_use_only_oss(self):
        from types import SimpleNamespace
        calls = []
        class Client:
            def presign(self, request, **kwargs):
                calls.append(request.key)
                return SimpleNamespace(url='https://default-oss.test/image.jpg?signature=test')
        import alibabacloud_oss_v2 as oss
        default = SimpleNamespace(client=Client(),sdk=oss,bucket='default-dataset')
        case = dict(modality='image',prompt='describe',objects=[{'key':'vision/v1/prepared/images/x.jpg'}])
        with patch.object(m,'default_store',return_value=default), patch.object(m,'store',side_effect=AssertionError('local store used')):
            content=m.model_content(case,30)
        self.assertEqual(calls,['vision/v1/prepared/images/x.jpg'])
        self.assertTrue(content[0]['image_url']['url'].startswith('https://default-oss.test'))
