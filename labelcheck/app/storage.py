"""File storage for uploads and checked images: local disk or S3 (or any S3-compatible store).

The database stores a KEY such as "uploads/12.png", never a full path, so the same
database works with either backend. Older rows that hold an absolute local path are
still read (local backend only).
"""
import logging
from pathlib import Path

from .config import settings

log = logging.getLogger("labelcheck.storage")


class LocalStorage:
    name = "local"

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, key):
        p = Path(key)
        if p.is_absolute():               # rows written by version 0.2
            return p
        full = (self.root / key).resolve()
        if self.root.resolve() not in full.parents:
            raise ValueError("Bad storage key.")
        return full

    def put(self, key, data: bytes, content_type=None):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(p)                    # atomic: readers never see half a file
        return key

    def get(self, key) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key):
        return self._path(key).exists()

    def delete(self, key):
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    def check(self):
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / ".ready-probe"
        probe.write_bytes(b"ok")
        probe.unlink()


class S3Storage:
    name = "s3"

    def __init__(self, bucket, prefix="", endpoint=None, region=None):
        import boto3                       # credentials: IAM role / env / profile (standard AWS chain)
        self.bucket, self.prefix = bucket, prefix
        self.client = boto3.client("s3", endpoint_url=endpoint, region_name=region)

    def _k(self, key):
        return f"{self.prefix}{key}"

    def put(self, key, data: bytes, content_type=None):
        extra = {"ServerSideEncryption": "AES256"}
        if content_type:
            extra["ContentType"] = content_type
        self.client.put_object(Bucket=self.bucket, Key=self._k(key), Body=data, **extra)
        return key

    def get(self, key) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()

    def exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except self.client.exceptions.ClientError:
            return False

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=self._k(key))

    def check(self):
        self.client.head_bucket(Bucket=self.bucket)


_store = None


def store():
    global _store
    if _store is None:
        if settings.storage == "s3":
            _store = S3Storage(settings.s3_bucket, settings.s3_prefix, settings.s3_endpoint, settings.s3_region)
        else:
            _store = LocalStorage(settings.data_dir)
        log.info("storage backend: %s", _store.name)
    return _store


def reset():
    """Tests only."""
    global _store
    _store = None
