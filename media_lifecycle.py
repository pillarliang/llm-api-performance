"""Reference-counted cleanup of personal media; leases survive worker crashes."""
import asyncio
import hashlib
import json
import re
from pathlib import Path

import visual_preprocess as v


class MediaLifecycle:
    def __init__(self, storage):
        self.storage = storage
        self.lock = asyncio.Lock()
        self.runs = {}
        self.ended = {}
        self.preparations = {}

    def resources(self, config):
        owner = config.get('media_owner')
        if owner is None:
            return None
        if not re.fullmatch(r'[a-f0-9]{64}', owner):
            raise ValueError('无效素材空间')
        assets = set()
        for case in config.get('visual_cases', []):
            if case.get('storage') != 'local':
                raise ValueError('个人评测素材来源无效')
            for source in case.get('sources', []):
                asset = source['asset_id']
                if not re.fullmatch(r'[a-f0-9]{32}', asset):
                    raise ValueError('无效素材ID')
                assets.add(asset)
        if not assets:
            raise ValueError('个人评测缺少素材引用')
        return dict(owner=owner, assets=sorted(assets))

    def lease_key(self, run_id):
        return '.leases/' + hashlib.sha256(run_id.encode()).hexdigest() + '.json'

    def base(self, owner):
        if not re.fullmatch(r'[a-f0-9]{64}', owner):
            raise ValueError('无效素材空间')
        return f'users/{owner}/vision/v1'

    def pending_key(self, owner, asset):
        if not re.fullmatch(r'[a-f0-9]{32}', asset):
            raise ValueError('无效素材ID')
        return f'.pending-cleanup/{owner}/{asset}.json'

    def verify_sources(self, owner, assets):
        for asset in assets:
            record = self.storage.get_json(f'{self.base(owner)}/uploads/{asset}.json')
            if not record or self.storage.head(record['key']) is None:
                raise ValueError('原件已在评测结束后清理，请重新上传并生成素材')

    async def acquire(self, config, run_id):
        resources = self.resources(config)
        if resources is None:
            return
        async with self.lock:
            await asyncio.to_thread(self.verify_sources, **resources)
            for case in config['visual_cases']:
                for obj in case['objects']:
                    if not obj['key'].startswith(self.base(resources['owner']) + '/prepared/'):
                        raise ValueError('素材路径不属于当前浏览器空间')
                    if await asyncio.to_thread(self.storage.head, obj['key']) is None:
                        raise ValueError('生成素材已清理，请重新上传并生成')
            await asyncio.to_thread(self.storage.put_bytes, self.lease_key(run_id), v.encoded(resources))
            self.runs[run_id] = resources

    async def abandon(self, run_id):
        """Submission failed before model execution: retain material so the user can retry."""
        async with self.lock:
            self.runs.pop(run_id, None)
            await asyncio.to_thread(self.storage.delete, self.lease_key(run_id))
            await asyncio.to_thread(self._collect)

    async def finish(self, config, run_id):
        resources = self.resources(config)
        if resources is None:
            return
        async with self.lock:
            self.runs.pop(run_id, None)
            self.ended[self.lease_key(run_id)] = resources
            await asyncio.to_thread(self._collect)

    async def begin_preparation(self, owner, assets, job_id):
        async with self.lock:
            await asyncio.to_thread(self.verify_sources, owner, assets)
            self.preparations[job_id] = dict(owner=owner, assets=sorted(assets))

    async def end_preparation(self, job_id):
        async with self.lock:
            self.preparations.pop(job_id, None)
            await asyncio.to_thread(self._collect)

    def _collect(self):
        # Failed cleanup-intent writes and lease removal are retried as well as file deletion.
        for key, resources in list(self.ended.items()):
            for asset in resources['assets']:
                self.storage.put_bytes(self.pending_key(resources['owner'], asset),
                                       v.encoded(dict(owner=resources['owner'], asset=asset)))
            self.storage.delete(key)
            del self.ended[key]
        active = {(r['owner'], a) for r in [*self.runs.values(), *self.preparations.values()] for a in r['assets']}
        pending = []
        deleted_by_owner = {}
        for obj in self.storage.objects('.pending-cleanup/'):
            record = self.storage.get_json(obj['key'])
            owner, asset = record['owner'], record['asset']
            if (owner, asset) in active:
                continue
            self._delete_asset(owner, asset)
            pending.append((obj['key'], owner, asset))
            deleted_by_owner.setdefault(owner, set()).add(asset)
        # Publish "cleaned" only after the complete deletion batch has finished.
        # Keeping all intent markers until here also makes partial I/O failures recoverable.
        blocked = set()
        for owner, assets in deleted_by_owner.items():
            blocked.update((owner, a) for a in self._invalidate_jobs(owner, assets, active))
        for key, owner, asset in pending:
            if (owner, asset) not in blocked:
                self.storage.delete(key)
        return len(pending)

    def _delete_asset(self, owner, asset):
        # Keys are built only from validated owner/asset IDs, never from user-supplied paths.
        self.pending_key(owner, asset)
        base = self.base(owner)
        root = self.storage.path(base)
        keys = []
        for directory in ('originals', 'prepared', 'prepared-index'):
            path = root / directory
            if path.exists():
                for file in path.rglob('*'):
                    if file.is_file() and (asset in file.relative_to(path).parts or file.stem == asset):
                        keys.append(str(file.relative_to(self.storage.root)))
        for key in keys:
            self.storage.delete(key)
        self.storage.delete(f'{base}/uploads/{asset}.json')
    def _invalidate_jobs(self, owner, removed_assets, active):
        base = self.base(owner)
        blocked = set()
        for obj in self.storage.objects(f'{base}/cases/'):
            cases = self.storage.get_json(obj['key'])
            sources = {s['asset_id'] for c in cases for s in c.get('sources', [])}
            touched = sources & removed_assets
            if not touched:
                continue
            if any((owner, a) in active for a in sources):
                blocked.update(touched)
                continue
            job_id = Path(obj['key']).stem
            job_key = f'{base}/jobs/{job_id}.json'
            job = self.storage.get_json(job_key)
            if job:
                job.update(status='cleaned', case_count=0,
                           message='评测已结束，原件与生成素材已清理；重新评测请重新上传')
                self.storage.put_bytes(job_key, v.encoded(job))
            self.storage.delete(obj['key'])
        return blocked

    async def recover(self):
        """Called before serving requests; previous worker's runs cannot resume themselves."""
        async with self.lock:
            self.runs.clear()
            self.preparations.clear()
            for obj in self.storage.objects('.leases/'):
                self.ended[obj['key']] = self.storage.get_json(obj['key'])
            return await asyncio.to_thread(self._collect)

    async def retry(self):
        async with self.lock:
            return await asyncio.to_thread(self._collect)
