"""Result storage. OSS is authoritative; disk is a staging area and retry outbox."""
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import shutil
import uuid


ARTIFACTS = ("requests.jsonl", "events.jsonl", "results.json", "report.html")


class StorageError(RuntimeError):
    pass


def valid_key(key):
    if not isinstance(key, str) or not key or "\\" in key or key.startswith("/") or any(p in {"", ".", ".."} for p in key.split("/")):
        raise ValueError("无效的结果路径")
    return key


def write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def encoded(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")


class LocalStore:
    remote = False

    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, key):
        path = (self.root / valid_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("无效的结果路径")
        return path

    def read(self, key):
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, key, data):
        write_bytes(self.path(key), data)

    def keys(self, prefix=""):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")
                      if p.is_file() and str(p.relative_to(self.root)).startswith(prefix))


class OSSStore(LocalStore):
    remote = True

    def __init__(self, root, client=None):
        super().__init__(root)
        import alibabacloud_oss_v2 as oss
        self.sdk = oss
        self.bucket = os.environ.get("OSS_BUCKET", "")
        self.prefix = os.environ.get("OSS_PREFIX", "results").strip("/")
        if self.prefix:
            valid_key(self.prefix)
        if not self.bucket or not os.environ.get("OSS_REGION"):
            raise StorageError("OSS_BUCKET 和 OSS_REGION 必须配置")
        if client is None:
            cfg = oss.config.load_default()
            cfg.region = os.environ["OSS_REGION"]
            cfg.endpoint = os.environ.get("OSS_ENDPOINT") or None
            cfg.credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
            cfg.connect_timeout = 5
            cfg.readwrite_timeout = 20
            cfg.retry_max_attempts = 3
            client = oss.Client(cfg)
        self.client = client

    def key(self, key):
        return "/".join(p for p in (self.prefix, valid_key(key)) if p)

    def read(self, key):
        try:
            result = self.client.get_object(self.sdk.GetObjectRequest(bucket=self.bucket, key=self.key(key)))
            with result.body as body:
                return b"".join(body.iter_bytes())
        except (self.sdk.exceptions.ServiceError, self.sdk.exceptions.OperationError) as exc:
            cause = exc.unwrap() if isinstance(exc, self.sdk.exceptions.OperationError) else exc
            if isinstance(cause, self.sdk.exceptions.ServiceError) and cause.code == "NoSuchKey":
                return None
            raise StorageError("OSS 读取失败，请检查网络与存储权限") from exc
        except Exception as exc:
            raise StorageError("OSS 读取失败，请检查网络与存储权限") from exc

    def put(self, key, data):
        try:
            self.client.put_object(self.sdk.PutObjectRequest(
                bucket=self.bucket, key=self.key(key), body=data,
                content_type=mimetypes.guess_type(key)[0] or "application/octet-stream"))
        except Exception as exc:
            raise StorageError("OSS 上传失败，本地工作文件已保留，稍后自动重试") from exc

    def keys(self, prefix=""):
        full_prefix = (self.prefix + "/" if self.prefix else "") + prefix
        try:
            return sorted(o.key[len(self.prefix) + 1:] if self.prefix else o.key
                          for page in self.client.list_objects_v2_paginator().iter_page(
                              self.sdk.ListObjectsV2Request(bucket=self.bucket, prefix=full_prefix))
                          for o in (page.contents or []))
        except Exception as exc:
            raise StorageError("OSS 列表读取失败，请检查网络与存储权限") from exc

    def snapshot(self, output):
        """Read on the event-loop thread between synchronous JSONL writes."""
        return {str((output / name).relative_to(self.root)): (output / name).read_bytes()
                for name in ARTIFACTS if (output / name).is_file()}

    def upload(self, files):
        for key, data in files.items():
            self.put(key, data)

    def archive(self, output, job=None):
        """Persist the retry intent before uploading; publish job metadata last."""
        relative = str(output.relative_to(self.root))
        manifest = {"path": relative, "job": job}
        marker = self.root / ".pending" / (output.name + ".json")
        write_bytes(marker, encoded(manifest))
        self.flush(marker)

    def flush(self, marker):
        manifest = json.loads(marker.read_bytes())
        output = self.path(manifest["path"])
        self.upload(self.snapshot(output))
        job = manifest.get("job")
        if job:
            self.put(".jobs/" + valid_key(job["job_id"]) + ".json", encoded(job))
        # Cleanup happens only after every object and terminal job state are durable.
        if output.exists():
            shutil.rmtree(output)
        if job:
            (self.root / ".jobs" / (job["job_id"] + ".json")).unlink(missing_ok=True)
        marker.unlink()
        return job

    def retry_pending(self):
        recovered = []
        for marker in sorted((self.root / ".pending").glob("*.json")):
            job = self.flush(marker)
            if job:
                recovered.append(job)
        return recovered


def get_store(root):
    backend = os.environ.get("RESULTS_BACKEND", "local")
    if backend == "local":
        return LocalStore(root)
    if backend == "oss":
        return OSSStore(root)
    raise StorageError("RESULTS_BACKEND 必须为 local 或 oss")


def restore_latest(store, name, version):
    """Always fetch the pointer and request history from OSS, never a stale cache."""
    prefix = f"{valid_key(name)}/{valid_key(version)}"
    pointer = store.read(prefix + "/latest.json")
    if pointer is None:
        (store.root / prefix / "latest.json").unlink(missing_ok=True)
        return
    run_id = valid_key(json.loads(pointer)["run_id"])
    if len(PurePosixPath(run_id).parts) != 1:
        raise ValueError("无效的 run ID")
    output = store.path(prefix + "/runs/" + run_id)
    if (output / ".run.lock").exists():
        return  # The server's reservation check rejects another writer.
    if (store.root / ".pending" / (run_id + ".json")).exists():
        raise StorageError("该运行的结果仍待上传，请等待 OSS 恢复后补跑")
    output.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACTS:
        data = store.read(prefix + "/runs/" + run_id + "/" + name)
        if data is None:
            (output / name).unlink(missing_ok=True)
        else:
            write_bytes(output / name, data)
    write_bytes(store.root / prefix / "latest.json", pointer)
