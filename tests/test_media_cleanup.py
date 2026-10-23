import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_media import LocalMediaStore
from media_lifecycle import MediaLifecycle
import media_web
import server
import visual_preprocess as v


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalMediaStore(self.temp.name)
        self.manager = MediaLifecycle(self.store)
        self.owner = 'a'*64
        self.asset = 'b'*32
        self.other = 'c'*32
        self.config = self.make_asset(self.owner, self.asset)
        self.other_config = self.make_asset(self.owner, self.other)
        self.default = 'vision/v1/prepared/images/default.jpg'
        self.store.put_bytes(self.default,b'default')

    def make_asset(self, owner, asset):
        base = f'users/{owner}/vision/v1'
        raw = f'{base}/originals/images/{asset}.jpg'
        output = f'{base}/prepared/images/long-512/{asset}/generation.jpg'
        index = f'{base}/prepared-index/{asset}/generation.json'
        self.store.put_bytes(raw,b'raw')
        self.store.put_bytes(output,b'prepared')
        self.store.put_bytes(f'{base}/uploads/{asset}.json', v.encoded(dict(id=asset,key=raw)))
        self.store.put_bytes(index,b'{}')
        case = dict(storage='local',sources=[dict(asset_id=asset)],objects=[dict(key=output)])
        self.store.put_bytes(f'{base}/cases/{asset}.json',v.encoded([case]))
        self.store.put_bytes(f'{base}/jobs/{asset}.json',v.encoded(dict(id=asset,status='ready')))
        return dict(media_owner=owner,visual_cases=[case])

    def raw_exists(self, asset=None):
        asset = asset or self.asset
        return self.store.head(f'users/{self.owner}/vision/v1/originals/images/{asset}.jpg') is not None

    async def test_last_run_deletes_raw_all_variants_metadata_and_invalidates_preparation(self):
        # A generation not selected by this run must also be removed for the used original.
        extra=f'users/{self.owner}/vision/v1/prepared/images/long-1024/{self.asset}/other.jpg'
        self.store.put_bytes(extra,b'other tier')
        await self.manager.acquire(self.config,'run')
        await self.manager.finish(self.config,'run')
        self.assertFalse(self.raw_exists())
        self.assertIsNone(self.store.head(extra))
        self.assertFalse(self.store._metadata_path(extra).exists())
        base=f'users/{self.owner}/vision/v1'
        self.assertEqual(self.store.get_json(f'{base}/jobs/{self.asset}.json')['status'],'cleaned')
        self.assertIsNone(self.store.get_json(f'{base}/cases/{self.asset}.json'))
        self.assertTrue(self.raw_exists(self.other))
        self.assertIsNotNone(self.store.head(self.default))
        self.assertEqual(self.store.objects('.leases/'),[])
        self.assertEqual(self.store.objects('.pending-cleanup/'),[])
        with self.assertRaises(ValueError):await self.manager.acquire(self.config,'later')

    async def test_cleaned_notification_waits_until_all_used_assets_are_deleted(self):
        mixed=dict(media_owner=self.owner,visual_cases=self.config['visual_cases']+self.other_config['visual_cases'])
        base=f'users/{self.owner}/vision/v1'
        self.store.put_bytes(f'{base}/cases/mixed.json',v.encoded(mixed['visual_cases']))
        self.store.put_bytes(f'{base}/jobs/mixed.json',v.encoded(dict(status='ready')))
        await self.manager.acquire(mixed,'mixed-run')
        original=self.manager._delete_asset
        observed=[]
        def check(owner,asset):
            original(owner,asset)
            observed.append(self.store.get_json(f'{base}/jobs/mixed.json')['status'])
        with patch.object(self.manager,'_delete_asset',side_effect=check):
            await self.manager.finish(mixed,'mixed-run')
        self.assertEqual(observed,['ready','ready'])
        self.assertFalse(self.raw_exists())
        self.assertFalse(self.raw_exists(self.other))
        self.assertEqual(self.store.get_json(f'{base}/jobs/mixed.json')['status'],'cleaned')

    async def test_concurrent_runs_keep_shared_material_until_final_release(self):
        await self.manager.acquire(self.config,'run1')
        await self.manager.acquire(self.config,'run2')
        await self.manager.finish(self.config,'run1')
        self.assertTrue(self.raw_exists())
        await self.manager.finish(self.config,'run2')
        self.assertFalse(self.raw_exists())

    async def test_failed_submission_preserves_material(self):
        await self.manager.acquire(self.config,'run')
        await self.manager.abandon('run')
        self.assertTrue(self.raw_exists())
        self.assertEqual(self.store.objects('.leases/'),[])

    async def test_restart_cleans_interrupted_run_leases_and_defaults_survive(self):
        await self.manager.acquire(self.config,'crashed-run')
        restarted=MediaLifecycle(self.store)
        await restarted.recover()
        self.assertFalse(self.raw_exists())
        self.assertTrue(self.raw_exists(self.other))
        self.assertIsNotNone(self.store.head(self.default))

    async def test_cleanup_io_failure_retries_without_leaving_permanent_reference(self):
        await self.manager.acquire(self.config,'run')
        original=self.store.delete
        def fail(key):
            if '/originals/' in key:raise OSError('temporary disk failure')
            original(key)
        with patch.object(self.store,'delete',side_effect=fail):
            with self.assertRaises(OSError):await self.manager.finish(self.config,'run')
        self.assertTrue(self.raw_exists())
        await self.manager.retry()
        self.assertFalse(self.raw_exists())

    async def test_intent_write_failure_is_retried(self):
        await self.manager.acquire(self.config,'run')
        with patch.object(self.store,'put_bytes',side_effect=OSError('disk')):
            with self.assertRaises(OSError):await self.manager.finish(self.config,'run')
        await self.manager.retry()
        self.assertFalse(self.raw_exists())

    async def test_preparation_is_protected_and_default_runs_do_not_trigger_cleanup(self):
        await self.manager.begin_preparation(self.owner,[self.asset],'preparation')
        await self.manager.acquire(self.config,'run')
        await self.manager.finish(self.config,'run')
        self.assertTrue(self.raw_exists())
        await self.manager.end_preparation('preparation')
        self.assertFalse(self.raw_exists())
        await self.manager.acquire({},'text-or-default')
        await self.manager.finish({},'text-or-default')
        self.assertTrue(self.raw_exists(self.other))

    async def test_success_error_and_cancellation_all_release_run_media(self):
        for outcome in ('success','error','cancelled'):
            config=self.make_asset(self.owner,self.asset)
            run=SimpleNamespace(config=config,session_id=outcome)
            await self.manager.acquire(config,outcome)
            async def body(*args):
                if outcome=='error':raise RuntimeError('benchmark failed')
                if outcome=='cancelled':raise asyncio.CancelledError()
            with patch.object(server,'execute_body',side_effect=body),patch.object(media_web,'lifecycle',return_value=self.manager):
                try:await server.execute(run,{})
                except (RuntimeError,asyncio.CancelledError):pass
            self.assertFalse(self.raw_exists(),outcome)


class ScratchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_cleans_only_owned_scratch_directories(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ('upload-abandoned','prepare-abandoned','unrelated'):
                (root/name).mkdir()
                (root/name/'data').write_bytes(b'test')
            with patch.object(media_web,'WORK',root):
                await media_web.recover_materials()
            self.assertFalse((root/'upload-abandoned').exists())
            self.assertFalse((root/'prepare-abandoned').exists())
            self.assertTrue((root/'unrelated/data').exists())
