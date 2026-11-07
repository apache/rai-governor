"""Where entries live: one object per entry, laid out as tooling-memory lays them out.

    <root>record/<domain>/<id>.md                      the entry as it stands
    <root>record/<domain>/.history/<id>/<stamp>-<digest>.md   each version it replaced

``<root>`` is ``MEMORY_S3_ROOT`` (default ``memory/``) in the bucket the rest of
gofannon uses, so pointing tooling-memory's OpenDAL S3 operator at the same
bucket and root reads this record as its own.

Every method here is blocking. Callers on the event loop wrap them in
``asyncio.to_thread``; see ``services/memory/service.py``.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol

from .entry import Entry, EntryError, valid_name

RECORD = "record"
HISTORY = ".history"


def _stamp(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S.%fZ")


def _check(*names: str) -> None:
    for n in names:
        if not valid_name(n):
            raise EntryError(f"unsafe path segment {n!r}")


def entry_key(domain: str, entry_id: str) -> str:
    _check(domain, entry_id)
    return f"{RECORD}/{domain}/{entry_id}.md"


def history_prefix(domain: str, entry_id: str) -> str:
    _check(domain, entry_id)
    return f"{RECORD}/{domain}/{HISTORY}/{entry_id}/"


class BlobStore(Protocol):
    def put(self, entry: Entry) -> Optional[str]:
        """Write the entry. Returns the history key of the version it replaced, if any."""

    def get_text(self, domain: str, entry_id: str) -> Optional[str]: ...

    def history(self, domain: str, entry_id: str) -> List[str]: ...

    def get_version_text(self, domain: str, entry_id: str, version: str) -> Optional[str]: ...


class _KeyValue:
    """The four object operations both stores share."""

    def _read(self, key: str) -> Optional[str]:
        raise NotImplementedError

    def _write(self, key: str, text: str) -> None:
        raise NotImplementedError

    def _list(self, prefix: str) -> List[str]:
        raise NotImplementedError

    def put(self, entry: Entry) -> Optional[str]:
        domain, entry_id = entry.front.domain, entry.front.id
        key = entry_key(domain, entry_id)
        archived = None
        previous = self._read(key)
        if previous is not None:
            # tooling-memory's version name: <stamp>-<first 8 hex of sha256(text)>.
            digest = hashlib.sha256(previous.encode("utf-8")).hexdigest()[:8]
            archived = f"{history_prefix(domain, entry_id)}{_stamp()}-{digest}.md"
            self._write(archived, previous)
        self._write(key, entry.render())
        return archived

    def get_text(self, domain: str, entry_id: str) -> Optional[str]:
        return self._read(entry_key(domain, entry_id))

    def history(self, domain: str, entry_id: str) -> List[str]:
        return sorted(self._list(history_prefix(domain, entry_id)))

    def get_version_text(self, domain: str, entry_id: str, version: str) -> Optional[str]:
        """A replaced version by its name, ``<stamp>-<digest>`` as history() lists it."""
        _check(version)
        return self._read(f"{history_prefix(domain, entry_id)}{version}.md")


class InMemoryBlobStore(_KeyValue):
    """For tests and for running locally with no object store."""

    def __init__(self) -> None:
        self.objects: Dict[str, str] = {}

    def _read(self, key: str) -> Optional[str]:
        return self.objects.get(key)

    def _write(self, key: str, text: str) -> None:
        self.objects[key] = text

    def _list(self, prefix: str) -> List[str]:
        return [k for k in self.objects if k.startswith(prefix)]


class S3BlobStore(_KeyValue):
    """The workbench's existing S3-compatible store (AD-15)."""

    def __init__(self, client, bucket: str, root: str = "memory/") -> None:
        self.client = client
        self.bucket = bucket
        self.root = root if not root or root.endswith("/") else root + "/"

    def _read(self, key: str) -> Optional[str]:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.root + key)
        except self.client.exceptions.NoSuchKey:
            return None
        return obj["Body"].read().decode("utf-8")

    def _write(self, key: str, text: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.root + key,
            Body=text.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )

    def _list(self, prefix: str) -> List[str]:
        keys: List[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.root + prefix):
            for item in page.get("Contents", []):
                keys.append(item["Key"][len(self.root):])
        return keys


def get_blob_store() -> BlobStore:
    """S3 when the workbench has an object store configured, memory otherwise."""
    from config import settings

    if settings.STORAGE_PROVIDER != "s3":
        return _default_in_memory
    import boto3
    from botocore.client import Config

    # S3_* credentials, never the AWS_* names: those would shadow the instance
    # role for every other boto3 client in the process (ISSUE-020).
    client = boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name=settings.AWS_DEFAULT_REGION,
    )
    bucket = os.getenv("MEMORY_S3_BUCKET") or settings.S3_BUCKET_NAME
    root = os.getenv("MEMORY_S3_ROOT", "memory/")
    return S3BlobStore(client, bucket, root)


_default_in_memory = InMemoryBlobStore()
