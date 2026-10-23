"""Browser-owned media datasets, asynchronous preparation, and signed model inputs."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
import visual_preprocess as v
from local_media import LocalMediaStore
from media_lifecycle import MediaLifecycle

router = APIRouter()
COOKIE = 'visual_dataset_owner'
ROOT = Path(__file__).resolve().parent
WORK = ROOT / 'state' / 'media-web'
TASKS = {}
GUARD = asyncio.Semaphore(1)
UPLOAD_GUARD = asyncio.Semaphore(2)


def store():
    return LocalMediaStore(WORK / 'datasets')


_lifecycle = None


def lifecycle():
    global _lifecycle
    storage = LocalMediaStore(WORK / 'datasets')
    if _lifecycle is None or _lifecycle.storage.root != storage.root:
        _lifecycle = MediaLifecycle(storage)
    return _lifecycle


async def recover_materials():
    # Before serving requests no previous worker can still use these scratch directories.
    import shutil
    def clear_scratch():
        if WORK.exists():
            for path in WORK.iterdir():
                if path.is_dir() and not path.is_symlink() and path.name.startswith(('upload-', 'prepare-')):
                    shutil.rmtree(path)
    await asyncio.to_thread(clear_scratch)
    await lifecycle().recover()


async def cleanup_loop():
    while True:
        await asyncio.sleep(30)
        try:
            await lifecycle().retry()
        except Exception as exc:
            v.log('media_cleanup_pending', error=v.safe_error(exc))


async def finish_run(config, run_id):
    if not config.get('media_owner'):
        return
    try:
        await lifecycle().finish(config, run_id)
    except Exception as exc:
        v.log('media_cleanup_pending', error=v.safe_error(exc))


def default_store():
    if not os.environ.get('OSS_ACCESS_KEY_ID'):
        raise HTTPException(503, '服务端未配置素材 OSS 凭证')
    return v.OSSDatasetStore(os.environ.get('DATASETS_OSS_BUCKET', 'llm-performance-vision-datasets'),
                             os.environ.get('DATASETS_OSS_REGION', os.environ.get('OSS_REGION', 'cn-beijing')),
                             os.environ.get('DATASETS_OSS_ENDPOINT'))


def owner(request):
    token = request.cookies.get(COOKIE, '')
    if not re.fullmatch(r'[a-f0-9]{64}', token):
        raise HTTPException(401, '请先打开素材面板初始化本浏览器数据集')
    return hashlib.sha256(token.encode()).hexdigest()


def prefix(request):
    return 'users/' + owner(request) + '/vision/v1'


def check_origin(request):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '请从当前工作台上传素材')


def asset_key(base, asset_id):
    if not isinstance(asset_id, str) or not re.fullmatch(r'[a-f0-9]{32}', asset_id):
        raise HTTPException(400, '无效素材 ID')
    return f'{base}/uploads/{asset_id}.json'


def inspect_media(path, kind):
    if kind == 'image':
        from PIL import Image, ImageOps
        with Image.open(path) as original:
            if getattr(original, 'n_frames', 1) != 1:
                raise v.PreparationError('请上传单帧图片')
            image = ImageOps.exif_transpose(original)
            image.load()
            w, h = image.size
        if min(w, h) <= 10 or max(w, h) / min(w, h) > 200:
            raise v.PreparationError('图片尺寸或宽高比不支持')
        edges = [edge for edge in v.IMAGE_EDGES if edge <= max(w, h)]
        if not edges:
            raise v.PreparationError('图片长边至少需要 512 像素')
        return dict(width=w, height=h, edges=edges)
    info = v.probe_video(path)
    v.validate_video(info, duration=10, height=360)
    heights = [h for h in (360, 720, 1080) if h <= info['height'] and h * 16 // 9 <= info['width']]
    durations = [d for d in (10, 30, 60, 180) if d <= info['duration_seconds'] + .001]
    counts = []
    if 60 in durations:
        timeline = v.frame_timeline(path, info['stream_index'])
        for count in (8, 16, 32, 64):
            try:
                v.select_frame_indices(timeline, count)
                counts.append(count)
            except v.PreparationError:
                pass
    return dict(info, heights=heights, durations=durations, frame_counts=counts)


def read_assets(storage, base):
    assets = []
    for obj in storage.objects(base + '/uploads/'):
        if obj['key'].endswith('.json'):
            assets.append(storage.get_json(obj['key']))
    return sorted(assets, key=lambda a: (a['name'], a['id']))


@router.get('/api/media')
async def list_media(request: Request):
    token = request.cookies.get(COOKIE, '')
    if not re.fullmatch(r'[a-f0-9]{64}', token):
        token = secrets.token_hex(32)
    base = 'users/' + hashlib.sha256(token.encode()).hexdigest() + '/vision/v1'
    storage = store()
    assets = await asyncio.to_thread(read_assets, storage, base)
    default_error = None
    default = None
    try:
        default_storage = default_store()
        default = await asyncio.to_thread(default_storage.get_json, 'vision/v1/manifests/latest.json')
        if default and default.get('case_count'):
            key = default['cases_key']
            if not key.startswith('vision/v1/manifests/snapshots/'):
                raise HTTPException(400, '默认数据集索引无效')
            def group_counts():
                result = default_storage.client.get_object(default_storage.sdk.GetObjectRequest(bucket=default_storage.bucket, key=key))
                counts = {}
                with result.body as body:
                    for line in b''.join(body.iter_bytes()).splitlines():
                        group = json.loads(line)['group']
                        counts[group] = counts.get(group, 0) + 1
                return counts
            default['group_counts'] = await asyncio.to_thread(group_counts)
    except Exception as exc:
        default_error = '默认数据集暂不可用：' + v.safe_error(exc)
    response = JSONResponse(dict(assets=assets, default=default, default_error=default_error,
                                 isolation='当前浏览器的素材；清除 Cookie 后无法恢复此数据集'))
    response.set_cookie(COOKIE, token, httponly=True, samesite='strict', secure=request.url.scheme == 'https',
                        max_age=365 * 86400)
    return response


@router.get('/api/media/content/{key:path}')
async def media_content(key: str, request: Request, expires: int, signature: str):
    # This endpoint is fetched by model providers without browser cookies.
    if not re.fullmatch(r'users/[a-f0-9]{64}/vision/v1/prepared/(images|videos|frames)/.+\.(jpg|mp4)', key):
        raise HTTPException(404)
    storage = store()
    try:
        path = storage.path(key)
        valid = storage.verify(key, expires, signature)
    except (ValueError, TypeError):
        valid = False
    if not valid or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type='video/mp4' if key.endswith('.mp4') else 'image/jpeg',
                        headers={'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer'})


@router.post('/api/media/upload')
async def upload(request: Request):
    check_origin(request)
    base = prefix(request)
    storage = store()
    name = Path(unquote(request.headers.get('x-file-name', ''))).name[:180]
    suffix = Path(name).suffix.lower()
    kind = 'image' if suffix in v.IMAGE_EXTENSIONS else 'video' if suffix in v.VIDEO_EXTENSIONS else None
    if not kind:
        raise HTTPException(400, '支持 JPEG/PNG/WebP/BMP/TIFF 图片和 MP4/MOV/MKV/AVI/WebM/M4V 视频')
    limit = v.IMAGE_LIMIT if kind == 'image' else v.VIDEO_LIMIT
    declared = request.headers.get('content-length')
    if declared and (not declared.isdigit() or int(declared) > limit):
        raise HTTPException(413, '图片最多20MB，视频最多2GB')
    WORK.mkdir(parents=True, exist_ok=True)
    async with UPLOAD_GUARD:
        with tempfile.TemporaryDirectory(prefix='upload-', dir=WORK) as temp:
            path = Path(temp) / ('source' + suffix)
            size = 0
            with path.open('wb') as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, '图片最多20MB，视频最多2GB')
                    await asyncio.to_thread(output.write, chunk)
            try:
                info = await asyncio.to_thread(inspect_media, path, kind)
                sha = await asyncio.to_thread(v.file_hash, path)
                asset_id = uuid.uuid4().hex
                key = f'{base}/originals/{"images" if kind == "image" else "videos"}/{asset_id}{suffix}'
                await asyncio.to_thread(storage.put_file, key, path, sha,
                                        'image/' + ('jpeg' if suffix in ('.jpg', '.jpeg') else suffix[1:])
                                        if kind == 'image' else 'application/octet-stream')
                asset = dict(id=asset_id, name=name, kind=kind, key=key, sha256=sha, bytes=size,
                             info=info, created_at=v.now())
                await asyncio.to_thread(storage.put_bytes, asset_key(base, asset_id), v.encoded(asset))
                return asset
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(400, v.safe_error(exc)) from None


def validate_selection(asset, item):
    info = asset['info']
    if asset['kind'] == 'image':
        values = item.get('edges', [])
        if not isinstance(values, list) or not values or any(type(x) is not int or x not in info['edges'] for x in values):
            raise HTTPException(400, '请选择原图支持的分辨率')
        return dict(edges=sorted(set(values)))
    specs = item.get('videos', [])
    frames = item.get('frames', [])
    if not isinstance(specs, list) or not isinstance(frames, list) or not (specs or frames):
        raise HTTPException(400, '请选择视频或抽帧档位')
    for spec in specs:
        if (not isinstance(spec, list) or len(spec) != 2 or any(type(x) is not int for x in spec)
                or spec[0] not in info['durations'] or spec[1] not in info['heights']):
            raise HTTPException(400, '视频档位超出原始素材规格')
    for spec in frames:
        if (not isinstance(spec, list) or len(spec) != 2 or any(type(x) is not int for x in spec)
                or spec[0] not in info['frame_counts'] or spec[1] not in info['heights']):
            raise HTTPException(400, '抽帧档位超出原始素材规格')
    if len(specs) + len(frames) > 32:
        raise HTTPException(400, '单个素材最多选择32种组合')
    return dict(videos=sorted(set(map(tuple, specs))), frames=sorted(set(map(tuple, frames))))


def generate(storage, base, asset, selection):
    settings = v.recipe()
    generation = v.digest(dict(source=asset['sha256'], selection=selection, recipe=settings))
    checkpoint = f'{base}/prepared-index/{asset["id"]}/{generation}.json'
    previous = storage.get_json(checkpoint)
    if previous and all(v.artifact_present(storage, a) for a in previous['artifacts']):
        return previous
    with tempfile.TemporaryDirectory(prefix='prepare-', dir=WORK) as temp:
        root = Path(temp)
        source = root / ('source' + Path(asset['key']).suffix)
        identity = storage.head(asset['key'])
        if identity is None:
            raise v.PreparationError('原始素材不存在')
        storage.download(dict(key=asset['key'], **identity), source)
        if v.file_hash(source) != asset['sha256']:
            raise v.PreparationError('原始素材已改变，请重新上传')
        output = root / 'out'
        output.mkdir()
        if asset['kind'] == 'image':
            metadata, artifacts = v.prepare_image(source, output, selection['edges'])
        else:
            metadata, artifacts = v.prepare_video(source, output, selection['videos'], selection['frames'])
        for a in artifacts:
            path = a.pop('path')
            if asset['kind'] == 'image':
                tier = f'images/long-{a["long_edge"]}'
                relative = f'{asset["id"]}/{generation}.jpg'
            elif a['kind'] == 'video':
                tier = f'videos/{a["height"]}p/{a["target_duration_seconds"]}s'
                relative = f'{asset["id"]}/{generation}.mp4'
            else:
                tier = f'frames/{a["height"]}p/60s/{a["submitted_frame_count"]}frames'
                relative = f'{asset["id"]}/{generation}/{path.name}'
            a.update(key=f'{base}/prepared/{tier}/{relative}', bytes=path.stat().st_size, sha256=v.file_hash(path))
            if not v.artifact_present(storage, a):
                storage.put_file(a['key'], path, a['sha256'], a['content_type'])
            if not v.artifact_present(storage, a):
                raise v.PreparationError('生成文件保存校验失败')
        result = dict(asset_id=asset['id'], name=asset['name'], kind=asset['kind'],
                      source_sha256=asset['sha256'], source_metadata=metadata, artifacts=artifacts,
                      generation=generation, recipe=settings)
        storage.put_bytes(checkpoint, v.encoded(result))
        return result


def media_cases(results, counts):
    cases = []
    images = [r for r in results if r['kind'] == 'image']
    def add(label, modality, artifacts, sources, **extra):
        case = dict(group=label, modality=modality, objects=artifacts,
                    storage='local',
                    sources=[dict(asset_id=r['asset_id'], sha256=r['source_sha256']) for r in sources],
                    prompt=v.PROMPTS['image' if modality == 'image' else 'video'],
                    server_actual_resolution=None, server_actual_frames=None, **extra)
        case['case_id'] = v.digest(case)[:24]
        cases.append(case)
    for r in results:
        if r['kind'] == 'image':
            for a in r['artifacts']:
                add(f'图片 · 长边{a["long_edge"]} · {r["name"]}', 'image', [a], [r],
                    image_count=1, long_edge=a['long_edge'])
        else:
            groups = {}
            for a in r['artifacts']:
                groups.setdefault(a['variant'], []).append(a)
            for variant, artifacts in groups.items():
                a = artifacts[0]
                is_frame = a['kind'] == 'frame'
                add(f'{"视频帧" if is_frame else "视频文件"} · {variant} · {r["name"]}',
                    'video_frames' if is_frame else 'video_file', artifacts, [r],
                    duration_seconds=60 if is_frame else a['target_duration_seconds'], height=a['height'],
                    sampling_fps=a['sampling_fps'] if is_frame else 1,
                    submitted_frame_count=len(artifacts) if is_frame else None,
                    frame_timestamps_seconds=[f['timestamp_seconds'] for f in artifacts] if is_frame else None,
                    source_fps=r['source_metadata']['source_fps'])
    for count in counts:
        candidates = [(r, next((a for a in r['artifacts'] if a.get('long_edge') == 1024), None)) for r in images]
        candidates = [(r, a) for r, a in candidates if a]
        if len(candidates) < count:
            raise v.PreparationError('多图组合的原图数量不足')
        chosen = candidates[:count]
        add(f'多图 · {count}张 · 长边1024', 'image', [a for _, a in chosen], [r for r, _ in chosen],
            image_count=count, long_edge=1024)
    return cases


async def preparation_job(storage, base, job, selections, counts):
    async with GUARD:
        job['status'] = 'processing'
        try:
            results = []
            for asset, selection in selections:
                job['message'] = '正在处理：' + asset['name']
                await asyncio.to_thread(storage.put_bytes, f'{base}/jobs/{job["id"]}.json', v.encoded(job))
                results.append(await asyncio.to_thread(generate, storage, base, asset, selection))
                job['completed'] += 1
            cases = media_cases(results, counts)
            await asyncio.to_thread(storage.put_bytes, f'{base}/cases/{job["id"]}.json', v.encoded(cases))
            job.update(status='ready', case_count=len(cases), message='素材已就绪，可开始评测')
        except asyncio.CancelledError:
            job.update(status='interrupted', message='服务停止，重新选择档位即可复用已完成素材')
            raise
        except Exception as exc:
            job.update(status='failed', message=v.safe_error(exc))
        finally:
            try:
                await asyncio.to_thread(storage.put_bytes, f'{base}/jobs/{job["id"]}.json', v.encoded(job))
            finally:
                await lifecycle().end_preparation(job['id'])


@router.post('/api/media/prepare')
async def prepare(request: Request):
    check_origin(request)
    base = prefix(request)
    storage = store()
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 262144:
            raise HTTPException(413, '素材选择不得超过256KiB')
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, '请求必须为JSON对象') from None
    items = data.get('assets', [])
    counts = data.get('image_counts', [])
    if not isinstance(items, list) or not 1 <= len(items) <= 64:
        raise HTTPException(400, '每次选择1至64份素材')
    if not isinstance(counts, list) or any(type(n) is not int or n not in (2, 4, 8) for n in counts):
        raise HTTPException(400, '多图数量支持2、4、8')
    counts = sorted(set(counts))
    selections = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(400, '素材选择无效')
        asset = await asyncio.to_thread(storage.get_json, asset_key(base, item.get('id', '')))
        if not asset or asset['id'] in seen:
            raise HTTPException(400, '素材不存在或重复')
        seen.add(asset['id'])
        selection = validate_selection(asset, item)
        selections.append((asset, selection))
    eligible = sum(a['kind'] == 'image' and 1024 in s['edges'] for a, s in selections)
    if any(n > eligible for n in counts):
        raise HTTPException(400, '多图组合需选中足够数量的图片，并勾选长边1024')
    if sum(not t.done() for t in TASKS.values()) >= 8:
        raise HTTPException(429, '预处理队列已满，请稍后再试')
    job = dict(id=uuid.uuid4().hex, status='queued', completed=0, total=len(items), message='等待处理')
    try:
        await lifecycle().begin_preparation(owner(request), sorted(seen), job['id'])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    try:
        await asyncio.to_thread(storage.put_bytes, f'{base}/jobs/{job["id"]}.json', v.encoded(job))
    except Exception:
        await lifecycle().end_preparation(job['id'])
        raise
    task_key = base + '/' + job['id']
    task = asyncio.create_task(preparation_job(storage, base, job, selections, counts))
    TASKS[task_key] = task
    task.add_done_callback(lambda done: TASKS.pop(task_key, None))
    return job


@router.get('/api/media/jobs/{job_id}')
async def get_job(job_id: str, request: Request):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise HTTPException(404)
    base = prefix(request)
    storage = store()
    job = await asyncio.to_thread(storage.get_json, f'{base}/jobs/{job_id}.json')
    if not job:
        raise HTTPException(404)
    if job['status'] in ('queued', 'processing') and base + '/' + job_id not in TASKS:
        job.update(status='interrupted', message='服务已重启，请重新生成；已完成素材会复用')
    return job


async def resolve_selection(request, data, config):
    selection = data.get('media')
    if not selection:
        return config
    if not isinstance(selection, dict):
        raise HTTPException(400, '素材选择必须是对象')
    storage = store()
    if selection.get('source') not in ('upload', 'default'):
        raise HTTPException(400, '无效素材来源')
    if selection.get('source') == 'default':
        storage = default_store()
        latest = await asyncio.to_thread(storage.get_json, 'vision/v1/manifests/latest.json')
        if not latest or not latest['case_count']:
            raise HTTPException(400, '默认数据集尚未准备，请先上传自己的素材')
        key = latest['cases_key']
        if not key.startswith('vision/v1/manifests/snapshots/'):
            raise HTTPException(400, '默认数据集索引无效')
        result = await asyncio.to_thread(storage.client.get_object, storage.sdk.GetObjectRequest(bucket=storage.bucket, key=key))
        with result.body as body:
            cases = [json.loads(line) for line in b''.join(body.iter_bytes()).splitlines()]
        groups = selection.get('groups', [])
        if not isinstance(groups, list) or any(not isinstance(group, str) for group in groups):
            raise HTTPException(400, '默认数据集测试组必须是字符串列表')
        cases = [c for c in cases if c['group'] in groups]
    else:
        base = prefix(request)
        job_id = selection.get('job_id', '')
        if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise HTTPException(400, '请先生成选中的素材')
        job = await asyncio.to_thread(storage.get_json, f'{base}/jobs/{job_id}.json')
        if not job or job['status'] != 'ready':
            raise HTTPException(400, '素材尚未准备完成')
        cases = await asyncio.to_thread(storage.get_json, f'{base}/cases/{job_id}.json')
    if not cases or len(cases) > 1024:
        raise HTTPException(400, '请选择有效的视觉测试组（最多1024个样本）')
    if selection.get('source') != 'default':
        config['media_owner'] = owner(request)
        origin = os.environ.get('MEDIA_PUBLIC_BASE_URL', str(request.base_url).rstrip('/'))
        from urllib.parse import urlparse
        parsed = urlparse(origin)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(400, 'MEDIA_PUBLIC_BASE_URL 必须为模型可访问的 HTTP(S) 地址')
        for case in cases:
            case['storage'] = 'local'
            case['media_origin'] = origin
    config['visual_cases'] = cases
    config['input_tokens'] = [None]
    return config


def model_content(case, timeout):
    local = case.get('storage') == 'local'
    storage = store() if local else default_store()
    def signed(key):
        if local:
            return storage.signed_url(case['media_origin'], key, max(3600, int(timeout) * 2 + 600))
        result = storage.client.presign(storage.sdk.GetObjectRequest(bucket=storage.bucket, key=key),
                                         expires=timedelta(seconds=max(3600, int(timeout) * 2 + 600)))
        return result.url
    objects = case['objects']
    if case['modality'] == 'image':
        parts = [dict(type='image_url', image_url=dict(url=signed(a['key'])), max_pixels=16777216) for a in objects]
    elif case['modality'] == 'video_file':
        parts = [dict(type='video_url', video_url=dict(url=signed(objects[0]['key'])),
                      fps=case.get('sampling_fps', case.get('requested_sampling_fps', 1)),
                      max_pixels=2048000, total_pixels=819200000)]
    else:
        parts = [dict(type='video', video=[signed(a['key']) for a in objects],
                      fps=case.get('sampling_fps', len(objects)/60), max_pixels=2048000, total_pixels=819200000)]
    return parts + [dict(type='text', text=case['prompt'])]


async def shutdown():
    # Let active worker threads finish before their temporary directories are removed.
    active = [task for task in TASKS.values() if not task.done()]
    await asyncio.gather(*active, return_exceptions=True)
