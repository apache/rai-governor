"""ISSUE-020: only the object store uses the S3_* keys.

The object store (SeaweedFS locally, S3 elsewhere) takes S3_ACCESS_KEY_ID /
S3_SECRET_ACCESS_KEY. Real AWS services (DynamoDB, CloudWatch, Bedrock, STS)
take boto3's default chain, which is the instance role on EC2. Handing the
object store's keys to a real AWS client would sign its requests as the
wrong principal.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import services.database_service as dbs
import services.observability_service as obs

ROOT = Path(__file__).resolve().parents[2]
S3_KEYS = {"S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"}
# Files allowed to read the object store's keys.
ALLOWED = {"config/__init__.py", "services/storage_service.py", "services/memory/blob_store.py"}


def test_only_the_object_store_reads_s3_keys():
    readers = set()
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", ".venv/", "venv/")) or "site-packages" in rel:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
            if name in S3_KEYS:
                readers.add(rel)
    assert readers - ALLOWED == set()


def test_dynamodb_gets_no_explicit_keys(monkeypatch):
    seen = {}

    class FakeDynamo:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    settings = SimpleNamespace(
        DATABASE_PROVIDER="dynamodb",
        DYNAMODB_REGION="us-east-1",
        DYNAMODB_ENDPOINT_URL=None,
        S3_ACCESS_KEY_ID="seaweed-key",
        S3_SECRET_ACCESS_KEY="seaweed-secret",
    )
    monkeypatch.setattr(dbs, "DynamoDBService", FakeDynamo)
    monkeypatch.setattr(dbs, "_db_instance", None)
    dbs.get_database_service(settings)
    assert seen == {"region_name": "us-east-1", "endpoint_url": None}


def test_cloudwatch_uses_the_default_chain(monkeypatch):
    seen = {}

    class FakeCloudWatch:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def log(self, payload):
            pass

    monkeypatch.setattr(obs, "AWSCloudWatchLogsProvider", FakeCloudWatch)
    monkeypatch.setattr(obs.settings, "APP_ENV", "production")
    monkeypatch.setattr(obs.settings, "GCP_PROJECT_ID", None)
    monkeypatch.setattr(obs.settings, "AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(obs.settings, "CLOUDWATCH_LOG_GROUP_NAME", "gofannon-logs")
    # No S3 keys at all: CloudWatch must still come up.
    monkeypatch.setattr(obs.settings, "S3_ACCESS_KEY_ID", None)
    monkeypatch.setattr(obs.settings, "S3_SECRET_ACCESS_KEY", None)

    service = obs.ObservabilityService()
    assert any(isinstance(p, FakeCloudWatch) for p in service.providers)
    assert seen == {"region_name": "us-east-1", "log_group_name": "gofannon-logs"}
