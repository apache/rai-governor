"""The CouchDB and object-store clients give up on a stalled server instead
of holding a worker until gunicorn kills it."""

import socket
import threading
from types import SimpleNamespace

import pytest

from config import settings
from services import storage_service
from services.database_service.couchdb import CouchDBService
from services.memory import blob_store


@pytest.fixture
def silent_server():
    """Accepts connections and never answers."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    held = []
    stop = threading.Event()

    def accept():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(sock.accept()[0])
            except OSError:
                pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{sock.getsockname()[1]}/"
    stop.set()
    t.join(2)
    for c in held:
        c.close()
    sock.close()


def test_couchdb_gives_up_on_a_server_that_never_answers(silent_server):
    outcome = []

    def connect():
        try:
            CouchDBService(silent_server, "u", "p", SimpleNamespace(COUCHDB_TIMEOUT_SECONDS=0.5))
            outcome.append("connected")
        except ConnectionError as e:
            outcome.append(e)

    t = threading.Thread(target=connect, daemon=True)
    t.start()
    t.join(10)
    # Without a timeout the thread is still waiting.
    assert not t.is_alive(), "CouchDB call did not time out"
    assert isinstance(outcome[0], ConnectionError)


def test_couchdb_timeout_defaults_to_five_minutes(monkeypatch):
    monkeypatch.setattr("couchdb.Server.version", lambda self: "3.3.3")
    svc = CouchDBService("http://couch.invalid:5984/", "u", "p", SimpleNamespace())
    assert svc.server.resource.session.connection_pool.timeout == 300
    assert settings.COUCHDB_TIMEOUT_SECONDS == 300


def test_object_store_clients_have_connect_and_read_timeouts(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_PROVIDER", "s3")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "http://seaweed.invalid:8333")
    monkeypatch.setattr(settings, "AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", "k")
    monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", "s")
    assert (settings.STORAGE_CONNECT_TIMEOUT_SECONDS, settings.STORAGE_READ_TIMEOUT_SECONDS) == (10, 60)
    monkeypatch.setattr(settings, "STORAGE_CONNECT_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(settings, "STORAGE_READ_TIMEOUT_SECONDS", 7.0)

    for client in (blob_store.get_blob_store().client, storage_service.S3StorageService().s3_client):
        assert (client.meta.config.connect_timeout, client.meta.config.read_timeout) == (3.0, 7.0)
