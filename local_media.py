"""Persistent local storage for user media, with expiring read-only delivery URLs."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time
from urllib.parse import quote, urlencode

import visual_preprocess as v


class LocalMediaStore:
    bucket = 'local-user-media'

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        if not isinstance(key, str) or not key or '\\' in key or any(p in ('', '.', '..') for p in key.split('/')):
            raise ValueError('无效本地素材路径')
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('无效本地素材路径')
        return path

    def _metadata_path(self, key):
        return self.root / '.metadata' / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    def _atomic(self, target, writer):
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=target.parent, prefix='.write-')
        try:
            with os.fdopen(fd, 'wb') as output:
                writer(output)
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)

    def _record(self, key, sha256, content_type):
        info = self.path(key).stat()
        metadata = dict(size=info.st_size, mtime_ns=info.st_mtime_ns, sha256=sha256, content_type=content_type)
        self._atomic(self._metadata_path(key), lambda f: f.write(v.encoded(metadata)))

    def head(self, key):
        path = self.path(key)
        if not path.is_file():
            return None
        info = path.stat()
        metadata_path = self._metadata_path(key)
        metadata = json.loads(metadata_path.read_bytes()) if metadata_path.exists() else {}
        if metadata.get('mtime_ns') != info.st_mtime_ns or metadata.get('size') != info.st_size:
            sha = v.file_hash(path)
            self._record(key, sha, mimetypes.guess_type(key)[0] or 'application/octet-stream')
            metadata = json.loads(metadata_path.read_bytes())
        return dict(size=info.st_size, etag=metadata['sha256'],
                    last_modified=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                    metadata={'sha256': metadata['sha256']}, content_type=metadata['content_type'])

    def objects(self, prefix):
        directory = self.path(prefix.rstrip('/'))
        if not directory.exists():
            return []
        return [dict(key=str(path.relative_to(self.root)), **self.head(str(path.relative_to(self.root))))
                for path in sorted(directory.rglob('*')) if path.is_file() and not path.name.startswith('.write-')]

    def get_json(self, key):
        path = self.path(key)
        return json.loads(path.read_bytes()) if path.is_file() else None

    def put_bytes(self, key, data, content_type='application/json; charset=utf-8'):
        self._atomic(self.path(key), lambda f: f.write(data))
        self._record(key, hashlib.sha256(data).hexdigest(), content_type)

    def put_file(self, key, path, sha256, content_type):
        def copy(output):
            with Path(path).open('rb') as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
        self._atomic(self.path(key), copy)
        if v.file_hash(self.path(key)) != sha256:
            raise v.PreparationError('本地素材校验失败')
        self._record(key, sha256, content_type)

    def download(self, obj, target):
        info = self.head(obj['key'])
        if info is None or info['etag'] != obj['etag']:
            raise v.PreparationError('本地原始素材已改变')
        shutil.copyfile(self.path(obj['key']), target)

    def delete(self, key):
        path = self.path(key)
        path.unlink(missing_ok=True)
        self._metadata_path(key).unlink(missing_ok=True)
        parent = path.parent
        while parent != self.root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def secret(self):
        path = self.root / '.delivery-secret'
        if not path.exists():
            fd, name = tempfile.mkstemp(dir=self.root, prefix='.secret-')
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(secrets.token_bytes(32))
                try:
                    os.link(name, path)
                except FileExistsError:
                    pass
            finally:
                Path(name).unlink(missing_ok=True)
        value = path.read_bytes()
        if len(value) != 32:
            raise v.PreparationError('本地媒体签名密钥不可用')
        return value

    def signature(self, key, expires):
        return hmac.new(self.secret(), f'{key}\n{expires}'.encode(), hashlib.sha256).hexdigest()

    def signed_url(self, origin, key, seconds):
        self.path(key)
        expires = int(time.time()) + seconds
        query = urlencode(dict(expires=expires, signature=self.signature(key, expires)))
        return origin.rstrip('/') + '/api/media/content/' + quote(key, safe='/') + '?' + query

    def verify(self, key, expires, signature):
        if expires < int(time.time()):
            return False
        return hmac.compare_digest(self.signature(key, expires), signature)
