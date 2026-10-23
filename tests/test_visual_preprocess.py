"""Media correctness and restart/publication guarantees; no model calls."""
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import visual_preprocess as v


class MemoryStore:
    bucket = 'dataset-test'

    def __init__(self):
        self.data = {}
        self.metadata = {}
        self.writes = []
        self.downloads = 0
        self.fail_fragment = None

    def put_bytes(self, key, data, content_type=None):
        if self.fail_fragment and self.fail_fragment in key:
            raise RuntimeError('simulated failure with secret that must not be logged')
        self.data[key] = data
        self.metadata[key] = {'sha256': hashlib.sha256(data).hexdigest()}
        self.writes.append(key)

    def put_file(self, key, path, sha256, content_type):
        self.put_bytes(key, Path(path).read_bytes())

    def head(self, key):
        if key not in self.data:
            return None
        return dict(size=len(self.data[key]), etag=hashlib.md5(self.data[key]).hexdigest(),
                    last_modified='2026-10-23T00:00:00+00:00', metadata=self.metadata[key])

    def objects(self, prefix):
        return [dict(key=k, **{field: self.head(k)[field] for field in ('size', 'etag', 'last_modified')})
                for k in sorted(self.data) if k.startswith(prefix) and not k.endswith('/')]

    def get_json(self, key):
        return json.loads(self.data[key]) if key in self.data else None

    def download(self, obj, target):
        self.downloads += 1
        if obj['etag'] != self.head(obj['key'])['etag']:
            raise RuntimeError('source changed')
        Path(target).write_bytes(self.data[obj['key']])


def fake_prepare_image(source, output):
    artifacts = []
    for edge in v.IMAGE_EDGES:
        path = output / f'long-{edge}.jpg'
        path.write_bytes(source.read_bytes() + str(edge).encode())
        artifacts.append(dict(path=path, variant=f'long-{edge}', long_edge=edge,
                              width=edge, height=edge * 3 // 4, format='JPEG', content_type='image/jpeg'))
    return dict(original_resolution=[4000, 3000]), artifacts


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.store = MemoryStore()
        self.prefix = 'vision/v1'
        self.source = self.prefix + '/originals/images/中文 空格.jpg'
        self.store.put_bytes(self.source, b'original')
        self.settings = {'version': 1}
        patcher = patch.object(v, 'prepare_image', side_effect=fake_prepare_image)
        self.generator = patcher.start()
        self.addCleanup(patcher.stop)
        logs = patch.object(v, 'log')
        logs.start()
        self.addCleanup(logs.stop)

    def run_once(self, **kwargs):
        return v.process_once(self.store, self.prefix, self.work, self.settings, **kwargs)

    def latest(self):
        return self.store.get_json(self.prefix + '/manifests/latest.json')

    def test_plan_never_downloads_or_writes(self):
        before = list(self.store.writes)
        self.assertEqual(self.run_once(plan=True), 0)
        self.assertEqual(self.store.writes, before)
        self.assertEqual(self.store.downloads, 0)

    def test_rerun_skips_media_and_retains_same_snapshot(self):
        self.assertEqual(self.run_once(), 0)
        latest = self.latest()
        count = len(self.store.writes)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.generator.call_count, 1)
        self.assertEqual(self.store.downloads, 1)
        self.assertEqual(self.latest(), latest)
        self.assertFalse(any('/prepared/' in k for k in self.store.writes[count:]))
        self.assertEqual(len(latest['skipped_groups']), 3)

    def test_missing_or_modified_output_is_repaired(self):
        self.run_once()
        key = next(k for k in self.store.data if '/prepared/' in k)
        original = self.store.data[key]
        self.store.put_bytes(key, b'changed')
        self.run_once()
        self.assertEqual(self.store.data[key], original)
        del self.store.data[key]
        self.run_once()
        self.assertEqual(self.store.data[key], original)
        self.assertEqual(self.generator.call_count, 3)

    def test_source_change_creates_new_generation_retains_previous(self):
        self.run_once()
        old_keys = {k for k in self.store.data if '/prepared/' in k}
        old_snapshot = self.latest()['snapshot_id']
        self.store.put_bytes(self.source, b'changed source')
        self.run_once()
        self.assertNotEqual(old_snapshot, self.latest()['snapshot_id'])
        self.assertTrue(old_keys.issubset(self.store.data))
        self.assertEqual(len([k for k in self.store.data if '/prepared/' in k]), 8)

    def test_recipe_change_invalidates_checkpoint(self):
        self.run_once()
        old_snapshot = self.latest()['snapshot_id']
        self.settings['version'] = 2
        self.run_once()
        self.assertEqual(self.generator.call_count, 2)
        self.assertNotEqual(old_snapshot, self.latest()['snapshot_id'])

    def test_missing_snapshot_is_repaired_without_regenerating_media(self):
        self.run_once()
        latest = self.latest()
        key = latest['cases_key']
        expected = self.store.data.pop(key)
        self.run_once()
        self.assertEqual(self.store.data[key], expected)
        self.assertEqual(self.generator.call_count, 1)

    def test_upload_failure_does_not_publish_and_retry_recovers(self):
        self.store.fail_fragment = '/long-2048/'
        self.assertEqual(self.run_once(), 1)
        self.assertIsNone(self.latest())
        self.assertFalse(any('/manifests/assets/' in k for k in self.store.data))
        status = self.store.get_json(self.prefix + '/manifests/preprocess-status.json')
        self.assertNotIn('secret', json.dumps(status))
        self.store.fail_fragment = None
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.latest()['asset_count'], 1)

    def test_manifest_failure_preserves_previous_pointer(self):
        self.run_once()
        previous = self.latest()
        self.store.put_bytes(self.source, b'new source')
        self.store.fail_fragment = 'cases.jsonl'
        with self.assertRaises(RuntimeError):
            self.run_once()
        self.assertEqual(self.latest(), previous)

    def test_source_changes_during_processing_are_not_committed(self):
        def changed(source, output):
            self.store.put_bytes(self.source, b'replacement')
            return fake_prepare_image(source, output)
        self.generator.side_effect = changed
        self.assertEqual(self.run_once(), 1)
        self.assertIsNone(self.latest())

    def test_deleting_sources_removes_them_from_new_snapshot_not_history(self):
        self.run_once()
        prior = self.latest()
        del self.store.data[self.source]
        self.run_once()
        self.assertEqual(self.latest()['asset_count'], 0)
        self.assertIn(prior['assets_key'], self.store.data)

    def test_twelve_images_have_balanced_count_groups(self):
        for i in range(11):
            self.store.put_bytes(f'{self.prefix}/originals/images/{i:02}.png', str(i).encode())
        self.run_once()
        latest = self.latest()
        cases = [json.loads(line) for line in self.store.data[latest['cases_key']].splitlines()]
        self.assertEqual(len(cases), 96)
        self.assertEqual(len({c['case_id'] for c in cases}), 96)
        for count in v.IMAGE_COUNTS:
            group = [c for c in cases if c['group'] == 'image_count' and c['image_count'] == count]
            self.assertEqual(len(group), 12)
            counts = Counter(asset for case in group for asset in case['asset_ids'])
            self.assertEqual(set(counts.values()), {count})
        all_paths = [o['key'] for c in cases for o in c['objects']]
        self.assertTrue(all('?' not in p for p in all_paths))


class MediaValidationTests(unittest.TestCase):
    def test_video_rejects_short_small_non_widescreen_and_hdr(self):
        base = dict(width=1920, height=1080, duration_seconds=180,
                    sample_aspect_ratio='1:1', color_transfer='bt709')
        v.validate_video(base)
        for changes in [dict(duration_seconds=179), dict(width=1280, height=720),
                        dict(width=2048), dict(color_transfer='smpte2084')]:
            with self.subTest(changes=changes), self.assertRaises(v.PreparationError):
                v.validate_video(dict(base, **changes))

    def test_vfr_timestamps_use_actual_source_frames_and_nested_subsets(self):
        timestamps = sorted({i / 24 for i in range(1441)} | {1.001, 3.007, 40.002})
        targets, indices = v.select_frame_indices(timestamps)
        self.assertEqual(len(set(indices)), 64)
        for target, index in zip(targets, indices):
            self.assertGreaterEqual(timestamps[index], target)
            self.assertLess(timestamps[index] - target, 1 / 24 + 1e-6)
        targets32, indices32 = v.select_frame_indices(timestamps, 32)
        self.assertEqual(indices[::2], indices32)
        self.assertEqual(targets[::2], targets32)
        with self.assertRaises(v.PreparationError):
            v.select_frame_indices([0, 30, 60])

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'media dependency group required')
    def test_image_sizes_exif_rotation_alpha_and_no_upscaling(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'output'
            output.mkdir()
            source = root / 'original.jpg'
            exif = Image.Exif()
            exif[274] = 6
            Image.new('RGB', (3840, 2160), 'red').save(source, exif=exif)
            metadata, artifacts = v.prepare_image(source, output)
            self.assertEqual(metadata['original_resolution'], [2160, 3840])
            self.assertEqual([(a['width'], a['height']) for a in artifacts],
                             [(288, 512), (576, 1024), (1152, 2048), (2160, 3840)])
            for artifact in artifacts:
                with Image.open(artifact['path']) as image:
                    self.assertEqual(image.getexif().get(274, 1), 1)
            Image.new('RGBA', (3840, 2160), (255, 0, 0, 0)).save(root / 'alpha.png')
            _, artifacts = v.prepare_image(root / 'alpha.png', output)
            with Image.open(artifacts[0]['path']) as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
            Image.new('RGB', (1024, 768)).save(source)
            with self.assertRaises(v.PreparationError):
                v.prepare_image(source, output)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and importlib.util.find_spec('PIL'),
                     'FFmpeg and media dependency group required')
class RealVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.source = root / 'original.mp4'
        # Full 180-second 1080p fixture; low source FPS keeps the integration test inexpensive.
        v.run_tool(['ffmpeg', '-v', 'error', '-nostdin', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=2:duration=180',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=180',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-threads', '2',
                    '-c:a', 'aac', '-shortest', '-output_ts_offset', '5', str(cls.source)])
        cls.output = root / 'output'
        cls.output.mkdir()
        cls.metadata, cls.artifacts = v.prepare_video(cls.source, cls.output)

    def test_full_matrix_video_dimensions_durations_and_audio_removal(self):
        self.assertTrue(self.metadata['has_audio'])
        self.assertAlmostEqual(self.metadata['start_time_seconds'], 5, places=2)
        videos = [a for a in self.artifacts if a['kind'] == 'video']
        self.assertEqual(len(videos), 6)
        self.assertEqual(len(self.artifacts), 190)
        for artifact in videos:
            self.assertFalse(artifact['audio'])
            self.assertAlmostEqual(artifact['duration_seconds'], artifact['target_duration_seconds'], places=2)
            self.assertEqual(artifact['fps'], '2/1')
            self.assertEqual(artifact['width'] * 9, artifact['height'] * 16)
            self.assertEqual(artifact['frame_count'], artifact['target_duration_seconds'] * 2)

    def test_frame_count_sizes_timestamps_and_shared_sampling(self):
        from PIL import Image
        groups = {}
        for count, height in v.FRAME_SPECS:
            frames = [a for a in self.artifacts if a['variant'] == f'60s-{count}frames-{height}p']
            groups[count, height] = frames
            self.assertEqual(len(frames), count)
            for frame in frames:
                self.assertLess(frame['timestamp_seconds'], 60)
                self.assertGreaterEqual(frame['timestamp_seconds'], frame['target_timestamp_seconds'])
                with Image.open(frame['path']) as image:
                    self.assertEqual(image.size, (height * 16 // 9, height))
        times = lambda frames: [f['timestamp_seconds'] for f in frames]
        self.assertEqual(times(groups[32, 360]), times(groups[32, 1080]))
        self.assertEqual(times(groups[64, 720])[::8], times(groups[8, 720]))

    def test_video_cases_separate_native_and_preextracted_frames(self):
        artifacts = [{k: val for k, val in a.items() if k != 'path'} | dict(
            key=str(a['path']), sha256=v.file_hash(a['path']), bytes=a['path'].stat().st_size)
                     for a in self.artifacts]
        asset = dict(asset_id='video1', kind='video', artifacts=artifacts, source_metadata=self.metadata)
        cases, _ = v.build_cases([asset])
        self.assertEqual(Counter(c['group'] for c in cases),
                         {'video_duration': 4, 'video_resolution': 3, 'video_frame_count': 4, 'video_frame_resolution': 3})
        for case in cases:
            self.assertIsNone(case['server_actual_frames'])
            if case['modality'] == 'video_file':
                self.assertIsNone(case['submitted_frame_count'])
            else:
                self.assertEqual(case['submitted_frame_count'], len(case['objects']))


if __name__ == '__main__':
    unittest.main()
