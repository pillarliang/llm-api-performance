"""Reproducible OSS visual dataset preparation. No model requests are made."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from datetime import datetime, timezone
import fcntl
from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time


PIPELINE_VERSION = 2
IMAGE_EDGES = (512, 1024, 2048, 3840)
IMAGE_COUNTS = (1, 2, 4, 8)
VIDEO_SPECS = ((10, 720), (30, 720), (60, 360), (60, 720), (60, 1080), (180, 720))
FRAME_SPECS = ((8, 720), (16, 720), (32, 360), (32, 720), (32, 1080), (64, 720))
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}
IMAGE_LIMIT = 20_000_000
VIDEO_LIMIT = 2_000_000_000
PROMPTS = {
    'image': '请综合观察提供的全部图片，输出5条可核验的视觉事实，总计约150至200个汉字。不要猜测不可见的信息。',
    'video': '请按时间顺序描述视频中可观察到的主要场景与动作，输出5条，总计约150至200个汉字。不要猜测不可见的信息。',
}


class PreparationError(Exception):
    pass


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def log(event, **fields):
    print(json.dumps(dict(event=event, **fields), ensure_ascii=False), flush=True)


def safe_error(exc):
    # SDK errors can include request targets/headers: never print them or credentials.
    while hasattr(exc, 'unwrap'):
        cause = exc.unwrap()
        if cause is None or cause is exc:
            break
        exc = cause
    if hasattr(exc, 'code'):
        return f'OSS {exc.code}'
    if isinstance(exc, PreparationError):
        return str(exc)
    return type(exc).__name__


def run_tool(args, timeout=1800):
    try:
        result = subprocess.run(args, capture_output=True, check=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors='replace')[-1800:]
        raise PreparationError(f'{args[0]} 处理失败：{detail}') from None
    except subprocess.TimeoutExpired:
        raise PreparationError(f'{args[0]} 超时（{timeout} 秒）') from None
    return result.stdout


def recipe():
    from PIL import __version__ as pillow_version, features
    return dict(
        pipeline_version=PIPELINE_VERSION,
        image_edges=IMAGE_EDGES, image_counts=IMAGE_COUNTS,
        video_specs=VIDEO_SPECS, frame_specs=FRAME_SPECS,
        image=dict(format='JPEG', quality=90, subsampling=0, resample='LANCZOS',
                   alpha_background='white', exif_orientation='applied', color_profile='sRGB'),
        video=dict(codec='libx264', crf=20, preset='medium', pixel_format='yuv420p',
                   fps='preserve_source_timestamps', audio=False, subtitles=False,
                   clip_start_seconds=0, aspect_ratio='16:9', threads=2),
        frames=dict(duration_seconds=60, master_count=64,
                    sampling='first source frame at or after i * 60 / N; nested subsets',
                    master_resolution=[1920, 1080]),
        tools=dict(pillow=pillow_version, jpeg=features.version('jpg'),
                   ffmpeg=run_tool(['ffmpeg', '-version'], 30).decode().splitlines()[0]),
    )


class OSSDatasetStore:
    """Separate dataset namespace; deliberately never uses OSS_BUCKET/OSS_PREFIX."""
    def __init__(self, bucket, region, endpoint=None):
        import alibabacloud_oss_v2 as oss
        self.sdk = oss
        self.bucket = bucket
        cfg = oss.config.load_default()
        cfg.region = region
        cfg.endpoint = endpoint or None
        cfg.credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        cfg.connect_timeout = 10
        cfg.readwrite_timeout = 120
        cfg.retry_max_attempts = 3
        self.client = oss.Client(cfg)

    def objects(self, prefix):
        pages = self.client.list_objects_v2_paginator().iter_page(
            self.sdk.ListObjectsV2Request(bucket=self.bucket, prefix=prefix))
        return [dict(key=obj.key, size=obj.size, etag=obj.etag,
                     last_modified=obj.last_modified.isoformat())
                for page in pages for obj in (page.contents or []) if not obj.key.endswith('/')]

    def head(self, key):
        try:
            result = self.client.head_object(self.sdk.HeadObjectRequest(bucket=self.bucket, key=key))
        except Exception as exc:
            cause = exc
            while isinstance(cause, self.sdk.exceptions.OperationError):
                cause = cause.unwrap()
            if isinstance(cause, self.sdk.exceptions.ServiceError) and cause.code == 'NoSuchKey':
                return None
            raise
        return dict(size=result.content_length, etag=result.etag,
                    last_modified=result.last_modified.isoformat(), metadata=result.metadata or {})

    def get_json(self, key):
        if self.head(key) is None:
            return None
        result = self.client.get_object(self.sdk.GetObjectRequest(bucket=self.bucket, key=key))
        with result.body as body:
            return json.loads(b''.join(body.iter_bytes()))

    def download(self, obj, target):
        result = self.client.get_object(self.sdk.GetObjectRequest(
            bucket=self.bucket, key=obj['key'], if_match=obj['etag']))
        with result.body as body, Path(target).open('wb') as output:
            for chunk in body.iter_bytes():
                output.write(chunk)
        if Path(target).stat().st_size != obj['size']:
            raise PreparationError('下载文件长度与 OSS 对象不一致')

    def put_file(self, key, path, sha256, content_type):
        with Path(path).open('rb') as body:
            self.client.put_object(self.sdk.PutObjectRequest(
                bucket=self.bucket, key=key, body=body, content_length=Path(path).stat().st_size,
                metadata={'sha256': sha256}, content_type=content_type))

    def put_bytes(self, key, data, content_type='application/json; charset=utf-8'):
        self.client.put_object(self.sdk.PutObjectRequest(
            bucket=self.bucket, key=key, body=data, metadata={'sha256': hashlib.sha256(data).hexdigest()},
            content_type=content_type))


def scaled_size(width, height, long_edge):
    if max(width, height) < long_edge:
        raise PreparationError(f'原图长边不足 {long_edge} 像素，不进行放大')
    ratio = long_edge / max(width, height)
    return max(1, round(width * ratio)), max(1, round(height * ratio))


def save_jpeg(image, path):
    image.save(path, 'JPEG', quality=90, subsampling=0, optimize=False, progressive=False)
    if path.stat().st_size > IMAGE_LIMIT:
        raise PreparationError('JPEG 超过 20 MB；保持固定质量参数，未擅自降低压缩质量')


def prepare_image(source, output, edges=IMAGE_EDGES):
    from PIL import Image, ImageCms, ImageOps
    with Image.open(source) as original:
        if getattr(original, 'n_frames', 1) != 1:
            raise PreparationError('请提供单帧图片，不支持动画或多页 TIFF')
        stored_size = list(original.size)
        orientation = original.getexif().get(274, 1)
        image = ImageOps.exif_transpose(original)
        if max(image.size) < max(edges):
            raise PreparationError('图片分辨率不足，不进行放大')
        if min(image.size) <= 10 or max(image.size) / min(image.size) > 200:
            raise PreparationError('图片宽高或比例不符合视觉输入要求')
        if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
            rgba = image.convert('RGBA')
            image = Image.new('RGB', rgba.size, 'white')
            image.paste(rgba, mask=rgba.getchannel('A'))
        icc = original.info.get('icc_profile')
        if icc:
            try:
                image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                                 ImageCms.createProfile('sRGB'), outputMode='RGB')
            except Exception:
                raise PreparationError('无法将图片 ICC 色彩配置转换为 sRGB') from None
        else:
            image = image.convert('RGB')
        metadata = dict(stored_resolution=stored_size, original_resolution=list(image.size),
                        exif_orientation=orientation, color_profile='sRGB' if icc else 'assumed_sRGB')
        artifacts = []
        for edge in edges:
            size = scaled_size(*image.size, edge)
            path = output / f'long-{edge}.jpg'
            save_jpeg(image.resize(size, Image.Resampling.LANCZOS), path)
            artifacts.append(dict(path=path, variant=f'long-{edge}', long_edge=edge,
                                  width=size[0], height=size[1], format='JPEG', content_type='image/jpeg'))
    return metadata, artifacts


def probe_video(source):
    data = json.loads(run_tool(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                               '-of', 'json', str(source)]))
    videos = [s for s in data.get('streams', []) if s['codec_type'] == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    if not videos:
        raise PreparationError('文件中没有可用的视频流')
    stream = videos[0]
    rotation = int(stream.get('tags', {}).get('rotate', 0))
    for side in stream.get('side_data_list', []):
        if 'rotation' in side:
            rotation = int(side['rotation'])
    width, height = stream['width'], stream['height']
    if rotation % 180:
        width, height = height, width
    duration = float(stream.get('duration') or data.get('format', {}).get('duration') or 0)
    rate = stream.get('avg_frame_rate', '0/0')
    try:
        fps = float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        fps = 0
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(fps) or fps <= 0:
        raise PreparationError('无法读取有效的视频时长或帧率')
    start_time = float(stream.get('start_time') or data.get('format', {}).get('start_time') or 0)
    if not math.isfinite(start_time):
        raise PreparationError('视频起始时间戳无效')
    return dict(width=width, height=height, stored_resolution=[stream['width'], stream['height']],
                duration_seconds=duration, source_fps=rate, fps=fps, rotation=rotation,
                start_time_seconds=start_time,
                sample_aspect_ratio=stream.get('sample_aspect_ratio', '1:1'),
                color_transfer=stream.get('color_transfer', 'unknown'),
                codec=stream.get('codec_name'), stream_index=stream['index'],
                frame_count=int(stream['nb_frames']) if str(stream.get('nb_frames', '')).isdigit() else None,
                has_audio=any(s['codec_type'] == 'audio' for s in data['streams']),
                has_subtitles=any(s['codec_type'] == 'subtitle' for s in data['streams']),
                burned_in_subtitles='not_inspected')


def validate_video(info, duration=180, height=1080):
    if info['width'] < height * 16 // 9 or info['height'] < height:
        raise PreparationError('原视频分辨率不足，不进行放大')
    if abs(info['width'] / info['height'] - 16 / 9) > .002:
        raise PreparationError('请提供横向 16:9 原视频；不进行裁剪或拉伸')
    if info['sample_aspect_ratio'] not in ('1:1', '0:1', 'N/A'):
        raise PreparationError('请提供方形像素（SAR 1:1）视频')
    if info['duration_seconds'] < duration - .001:
        raise PreparationError('原视频时长不足，不循环或补帧延长')
    if info['color_transfer'] in ('smpte2084', 'arib-std-b67'):
        raise PreparationError('首版需要 SDR 视频；请先将 HDR 原视频转换为 SDR')


def frame_timeline(source, stream_index):
    data = json.loads(run_tool([
        'ffprobe', '-v', 'error', '-select_streams', str(stream_index),
        '-read_intervals', '%+61', '-show_frames',
        '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(source)]))
    frames = data.get('frames', [])
    if not frames or any('best_effort_timestamp_time' not in f for f in frames):
        raise PreparationError('无法获取抽帧时间戳')
    timestamps = [float(f['best_effort_timestamp_time']) for f in frames]
    timestamps = [t - timestamps[0] for t in timestamps]
    if any(not math.isfinite(t) for t in timestamps) or timestamps != sorted(timestamps):
        raise PreparationError('视频时间戳异常')
    return timestamps


def select_frame_indices(timestamps, count=64, duration=60):
    targets = [i * duration / count for i in range(count)]
    indices = [bisect_left(timestamps, target) for target in targets]
    if any(i >= len(timestamps) or timestamps[i] >= duration for i in indices):
        raise PreparationError('视频前 60 秒没有足够的有效采样帧')
    if len(set(indices)) != count:
        raise PreparationError('源视频帧过少，无法获得指定数量的不同采样帧')
    return targets, indices


def prepare_video(source, output, video_specs=VIDEO_SPECS, frame_specs=FRAME_SPECS):
    from PIL import Image
    info = probe_video(source)
    validate_video(info, max([d for d, _ in video_specs] + ([60] if frame_specs else [0])),
                   max([h for _, h in video_specs] + [h for _, h in frame_specs]))
    artifacts = []
    for duration, height in video_specs:
        width = height * 16 // 9
        path = output / f'{duration}s-{height}p.mp4'
        run_tool(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                  '-copyts', '-itsoffset', str(-info['start_time_seconds']),
                  '-threads', '2', '-i', str(source), '-map', f'0:{info["stream_index"]}',
                  '-an', '-sn', '-dn', '-map_metadata', '-1', '-map_chapters', '-1',
                  '-vf', f'scale={width}:{height}:flags=lanczos,setsar=1',
                  '-t', str(duration), '-fps_mode', 'passthrough', '-c:v', 'libx264',
                  '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p',
                  '-threads', '2', '-filter_threads', '2', '-movflags', '+faststart', str(path)])
        actual = probe_video(path)
        if (actual['width'], actual['height']) != (width, height) or actual['has_audio'] or actual['has_subtitles']:
            raise PreparationError('转码后视频的尺寸或音轨校验失败')
        if abs(actual['duration_seconds'] - duration) > max(.1, 1 / info['fps']):
            raise PreparationError('转码后视频时长不符合目标档位')
        if path.stat().st_size > VIDEO_LIMIT:
            raise PreparationError('生成的视频超过 2 GB；未擅自改变编码质量')
        artifacts.append(dict(path=path, variant=f'{duration}s-{height}p', kind='video',
                              width=width, height=height, target_duration_seconds=duration,
                              duration_seconds=actual['duration_seconds'], fps=actual['source_fps'],
                              frame_count=actual['frame_count'], audio=False, subtitles=False,
                              format='MP4/H.264', content_type='video/mp4'))
    if not frame_specs:
        return info, artifacts
    timestamps = frame_timeline(source, info['stream_index'])
    master_count = max(count for count, _ in frame_specs)
    master_height = max(height for _, height in frame_specs)
    master_width = master_height * 16 // 9
    targets, indices = select_frame_indices(timestamps, master_count)
    master_dir = output / 'masters'
    master_dir.mkdir()
    selection = '+'.join(f'eq(n\\,{index})' for index in indices)
    run_tool(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
              '-threads', '2', '-i', str(source), '-map', f'0:{info["stream_index"]}',
              '-an', '-sn', '-vf', f'select={selection},scale={master_width}:{master_height}:flags=lanczos,setsar=1',
              '-fps_mode', 'vfr', '-frames:v', str(master_count), '-threads', '2', '-filter_threads', '2',
              str(master_dir / '%03d.png')])
    masters = sorted(master_dir.glob('*.png'))
    if len(masters) != master_count:
        raise PreparationError('实际抽取帧数不是 64，停止发布该视频')
    for count, height in frame_specs:
        group = f'60s-{count}frames-{height}p'
        directory = output / group
        directory.mkdir()
        for j, master_index in enumerate(range(0, master_count, master_count // count)):
            path = directory / f'{j:03d}.jpg'
            width = height * 16 // 9
            with Image.open(masters[master_index]) as image:
                save_jpeg(image.convert('RGB').resize((width, height), Image.Resampling.LANCZOS), path)
            artifacts.append(dict(path=path, variant=group, kind='frame', frame_index=j,
                                  source_frame_index=indices[master_index],
                                  target_timestamp_seconds=targets[master_index],
                                  timestamp_seconds=timestamps[indices[master_index]],
                                  width=width, height=height, sampling_fps=count / 60,
                                  submitted_frame_count=count, format='JPEG', content_type='image/jpeg'))
    return info, artifacts


def artifact_present(store, artifact, inventory=None):
    if inventory is not None and artifact.get('oss_etag'):
        obj = inventory.get(artifact['key'])
        return bool(obj and obj['size'] == artifact['bytes'] and obj['etag'] == artifact['oss_etag'])
    head = store.head(artifact['key'])
    if head is None:
        return False
    metadata = {k.lower().removeprefix('x-oss-meta-'): v for k, v in head['metadata'].items()}
    return head['size'] == artifact['bytes'] and metadata.get('sha256') == artifact['sha256']


def source_identity(obj):
    return {key: obj[key] for key in ('key', 'size', 'etag', 'last_modified')}


def prepare_asset(store, obj, kind, prefix, work, settings, inventory=None):
    identity = source_identity(obj)
    asset_id = digest(dict(bucket=store.bucket, key=obj['key']))[:20]
    recipe_hash = digest(settings)
    checkpoint_key = f'{prefix}/manifests/assets/{asset_id}.json'
    previous = store.get_json(checkpoint_key)
    if (previous and previous.get('source_identity') == identity
            and previous.get('recipe_sha256') == recipe_hash
            and previous.get('artifacts')
            and all(artifact_present(store, artifact, inventory) for artifact in previous['artifacts'])):
        log('skipped', source=obj['key'])
        return previous
    with tempfile.TemporaryDirectory(prefix='asset-', dir=work) as temp:
        directory = Path(temp)
        source = directory / ('source' + PurePosixPath(obj['key']).suffix.lower())
        store.download(obj, source)
        sha256 = file_hash(source)
        output = directory / 'output'
        output.mkdir()
        metadata, artifacts = (prepare_image if kind == 'image' else prepare_video)(source, output)
        generation = digest(dict(source_sha256=sha256, recipe_sha256=recipe_hash))
        records = []
        for artifact in artifacts:
            path = artifact.pop('path')
            if kind == 'image':
                tier = f'images/long-{artifact["long_edge"]}'
                relative = f'{asset_id}/{generation}.jpg'
            elif artifact['kind'] == 'video':
                tier = f'videos/{artifact["height"]}p/{artifact["target_duration_seconds"]}s'
                relative = f'{asset_id}/{generation}.mp4'
            else:
                tier = f'frames/{artifact["height"]}p/60s/{artifact["submitted_frame_count"]}frames'
                relative = f'{asset_id}/{generation}/{path.name}'
            artifact.update(key=f'{prefix}/prepared/{tier}/{relative}',
                            sha256=file_hash(path), bytes=path.stat().st_size)
            if not artifact_present(store, artifact):
                store.put_file(artifact['key'], path, artifact['sha256'], artifact['content_type'])
                if not artifact_present(store, artifact):
                    raise PreparationError('上传后的对象校验失败')
            artifact['oss_etag'] = store.head(artifact['key'])['etag']
            records.append(artifact)
        # Refuse to commit a generation if the source changed while processing.
        current = store.head(obj['key'])
        if current is None or any(current[k] != obj[k] for k in ('size', 'etag', 'last_modified')):
            raise PreparationError('处理期间原始文件发生变化，请重新运行')
        result = dict(schema_version=1, asset_id=asset_id, kind=kind, source_identity=identity,
                      source_sha256=sha256, recipe_sha256=recipe_hash, recipe=settings,
                      source_metadata=metadata, artifacts=records, created_at=now())
        # Asset checkpoint is written only after all its outputs have been verified.
        store.put_bytes(checkpoint_key, encoded(result))
        log('prepared', source=obj['key'], artifacts=len(records))
        return result


def build_cases(assets):
    cases = []
    skipped = []
    images = sorted((a for a in assets if a['kind'] == 'image'), key=lambda a: a['source_identity']['key'])

    def add(group, modality, selected, asset_ids, **conditions):
        case = dict(group=group, modality=modality, asset_ids=asset_ids,
                    objects=[{k: a[k] for k in ('key', 'sha256', 'bytes', 'width', 'height')} for a in selected],
                    prompt=PROMPTS['image' if modality == 'image' else 'video'],
                    output_requirements='5条，约150至200个汉字；记录实际答案Token数',
                    server_actual_resolution=None, server_actual_frames=None,
                    server_observation_status='不可观测，后续仅按API实际披露填写', **conditions)
        case['case_id'] = digest(case)[:24]
        cases.append(case)

    for asset in images:
        for artifact in asset['artifacts']:
            add('image_resolution', 'image', [artifact], [asset['asset_id']],
                long_edge=artifact['long_edge'], image_count=1,
                api_parameters=dict(vl_high_resolution_images=False, max_pixels=16777216))
    # Cyclic windows give every image equal representation at each supported count.
    for count in IMAGE_COUNTS:
        if len(images) < count:
            skipped.append(dict(group='image_count', image_count=count, reason='原图数量不足'))
            continue
        for start in range(len(images)):
            selection = [images[(start+i) % len(images)] for i in range(count)]
            artifacts = [next(a for a in item['artifacts'] if a['long_edge'] == 1024) for item in selection]
            add('image_count', 'image', artifacts, [a['asset_id'] for a in selection],
                long_edge=1024, image_count=count, combination_index=start,
                api_parameters=dict(vl_high_resolution_images=False, max_pixels=16777216))
    for asset in sorted((a for a in assets if a['kind'] == 'video'), key=lambda a: a['asset_id']):
        for duration, height in VIDEO_SPECS:
            artifact = next(a for a in asset['artifacts'] if a['variant'] == f'{duration}s-{height}p')
            for group in (['video_duration'] if height == 720 else []) + (['video_resolution'] if duration == 60 else []):
                add(group, 'video_file', [artifact], [asset['asset_id']],
                    duration_seconds=duration, height=height, source_fps=asset['source_metadata']['source_fps'],
                    submitted_video_fps=artifact['fps'], requested_sampling_fps=1,
                    nominal_frame_count=duration, submitted_frame_count=None,
                    audio=False, external_subtitles=False,
                    api_parameters=dict(fps=1, max_pixels=2048000, total_pixels=819200000))
        for count, height in FRAME_SPECS:
            frames = sorted((a for a in asset['artifacts'] if a['variant'] == f'60s-{count}frames-{height}p'),
                            key=lambda a: a['frame_index'])
            for group in (['video_frame_count'] if height == 720 else []) + (['video_frame_resolution'] if count == 32 else []):
                add(group, 'video_frames', frames, [asset['asset_id']],
                    duration_seconds=60, height=height, source_fps=asset['source_metadata']['source_fps'],
                    sampling_fps=count / 60, submitted_frame_count=len(frames),
                    frame_timestamps_seconds=[f['timestamp_seconds'] for f in frames],
                    target_timestamps_seconds=[f['target_timestamp_seconds'] for f in frames],
                    audio=False, external_subtitles=False,
                    api_parameters=dict(max_pixels=2048000, total_pixels=819200000))
    return cases, skipped


def discover(store, prefix):
    sources = []
    ignored = []
    for kind, directory, extensions in [('image', 'images', IMAGE_EXTENSIONS), ('video', 'videos', VIDEO_EXTENSIONS)]:
        for obj in store.objects(f'{prefix}/originals/{directory}/'):
            if PurePosixPath(obj['key']).suffix.lower() not in extensions:
                ignored.append(obj['key'])
            else:
                sources.append((kind, obj))
    return sorted(sources, key=lambda pair: pair[1]['key']), sorted(ignored)


def process_once(store, prefix, work, settings, plan=False):
    sources, ignored = discover(store, prefix)
    log('inventory', images=sum(kind == 'image' for kind, _ in sources),
        videos=sum(kind == 'video' for kind, _ in sources), ignored=ignored)
    if plan:
        for kind, obj in sources:
            log('planned', kind=kind, source=obj['key'], bytes=obj['size'],
                outputs=4 if kind == 'image' else 190)
        return 0
    inventory = {obj['key']: obj for obj in store.objects(f'{prefix}/prepared/')}
    assets, errors = [], []
    for kind, obj in sources:
        log('processing', source=obj['key'])
        try:
            assets.append(prepare_asset(store, obj, kind, prefix, work, settings, inventory))
        except Exception as exc:
            error = dict(source=obj['key'], error=safe_error(exc))
            errors.append(error)
            log('failed', **error)
    # Re-list so concurrent source uploads/replacements cannot produce an incoherent snapshot.
    current, current_ignored = discover(store, prefix)
    if current != sources or current_ignored != ignored:
        errors.append(dict(source=None, error='处理期间原始素材目录发生变化，请重新运行'))
    status = dict(updated_at=now(), bucket=store.bucket, prefix=prefix,
                  complete=not errors, assets=len(assets), errors=errors, ignored=ignored)
    if errors:
        status['published'] = False
        store.put_bytes(f'{prefix}/manifests/preprocess-status.json', encoded(status))
        log('incomplete', errors=errors, message='未更新 latest.json；已完成素材可在下次运行时复用')
        return 1
    cases, skipped = build_cases(assets)
    asset_bytes = b''.join(encoded(a) for a in assets)
    case_bytes = b''.join(encoded(c) for c in cases)
    snapshot_id = hashlib.sha256(asset_bytes + b'\n' + case_bytes).hexdigest()
    snapshot_prefix = f'{prefix}/manifests/snapshots/{snapshot_id}'
    latest_key = f'{prefix}/manifests/latest.json'
    latest = store.get_json(latest_key)
    snapshot_files = [(f'{snapshot_prefix}/assets.jsonl', asset_bytes), (f'{snapshot_prefix}/cases.jsonl', case_bytes)]
    snapshots_intact = all(artifact_present(store, dict(key=key, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
                           for key, data in snapshot_files)
    if latest is None or latest.get('snapshot_id') != snapshot_id or not snapshots_intact:
        store.put_bytes(f'{snapshot_prefix}/assets.jsonl', asset_bytes, 'application/x-ndjson')
        store.put_bytes(f'{snapshot_prefix}/cases.jsonl', case_bytes, 'application/x-ndjson')
        latest = dict(schema_version=1, snapshot_id=snapshot_id, bucket=store.bucket,
                      assets_key=f'{snapshot_prefix}/assets.jsonl', cases_key=f'{snapshot_prefix}/cases.jsonl',
                      asset_count=len(assets), case_count=len(cases), skipped_groups=skipped,
                      recipe_sha256=digest(settings), published_at=now())
        # One small pointer commits both immutable manifests atomically.
        store.put_bytes(latest_key, encoded(latest))
    status.update(published=True, snapshot_id=snapshot_id, cases=len(cases), skipped_groups=skipped)
    store.put_bytes(f'{prefix}/manifests/preprocess-status.json', encoded(status))
    log('complete', **status)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='生成 OSS 视觉评测档位；默认单次执行，不调用模型 API。')
    parser.add_argument('--bucket', default=os.environ.get('DATASETS_OSS_BUCKET', 'llm-performance-vision-datasets'))
    parser.add_argument('--region', default=os.environ.get('DATASETS_OSS_REGION', os.environ.get('OSS_REGION', 'cn-beijing')))
    parser.add_argument('--endpoint', default=os.environ.get('DATASETS_OSS_ENDPOINT'))
    parser.add_argument('--prefix', default='vision/v1')
    parser.add_argument('--work-dir', type=Path, default=Path('state/visual-preprocess'))
    parser.add_argument('--plan', action='store_true', help='只列出待扫描素材，不下载或写入 OSS')
    parser.add_argument('--watch', action='store_true', help='持续扫描上传目录，自动处理新素材或发生变化的素材')
    parser.add_argument('--interval', type=int, default=60, help='持续扫描间隔秒数，默认60')
    args = parser.parse_args(argv)
    if not args.prefix or any(p in ('', '.', '..') for p in args.prefix.split('/')) or '\\' in args.prefix:
        parser.error('prefix 必须是非空、无前后斜杠且不含 .. 的 OSS 前缀')
    if args.plan and args.watch:
        parser.error('--plan 与 --watch 不能同时使用')
    if args.interval < 10:
        parser.error('--interval 至少为10秒')
    if args.bucket == os.environ.get('OSS_BUCKET'):
        parser.error('素材桶不能与 OSS_BUCKET 配置的结果桶相同')
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with (args.work_dir / '.preprocess.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PreparationError('此工作目录已有预处理进程运行') from None
        settings = None
        if not args.plan:
            for binary in ('ffmpeg', 'ffprobe'):
                if not shutil.which(binary):
                    raise PreparationError(f'缺少 {binary}，请使用 deploy/preprocess_visual.sh')
            settings = recipe()
        store = OSSDatasetStore(args.bucket, args.region, args.endpoint)
        while True:
            try:
                code = process_once(store, args.prefix, args.work_dir, settings, args.plan)
            except Exception as exc:
                log('error', error=safe_error(exc))
                if not args.watch:
                    return 1
                code = 1
            if not args.watch:
                return code
            time.sleep(args.interval)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log('stopped', message='下次运行将复用已完成素材')
        sys.exit(130)
    except Exception as exc:
        log('error', error=safe_error(exc))
        sys.exit(1)
