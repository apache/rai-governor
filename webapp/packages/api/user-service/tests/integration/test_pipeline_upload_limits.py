"""A folder upload skips a file over the size limit without reading it,
and the import of the rest goes ahead."""

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app_factory import create_app
from config import settings
from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services import pipeline_io as pio
from services.database_service.memory import MemoryDBService

MANIFEST = "pipeline: demo\nentry: demo_main\nagents:\n  - name: demo_main\n    code: main.py\n"
CODE = "async def run(input_dict, tools):\n    return {'outputText': 'ok'}\n"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", "all_users")
    app = create_app()
    db = MemoryDBService()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"uid": "asf:akm", "isSiteAdmin": True}
    app.dependency_overrides[get_user_service_dep] = lambda: None
    return TestClient(app)


def test_an_oversized_file_is_skipped_unread(client, monkeypatch):
    read = []
    real_read = UploadFile.read

    async def spy(self, *a, **kw):
        read.append(self.filename)
        return await real_read(self, *a, **kw)

    monkeypatch.setattr(UploadFile, "read", spy)
    files = [("files", ("p/pipeline.yaml", MANIFEST.encode())),
             ("files", ("p/main.py", CODE.encode())),
             ("files", ("p/data.bin", b"x" * (pio.MAX_FILE_BYTES + 1)))]
    r = client.post("/pipelines/import/upload", files=files,
                    data={"label": "v1", "dryRun": "true"})
    assert r.status_code == 200, r.text
    assert "p/data.bin" not in read
    assert sorted(read) == ["p/main.py", "p/pipeline.yaml"]


def test_too_many_files_is_refused_before_reading(client):
    files = [("files", (f"p/f{i}.txt", b"x")) for i in range(pio.MAX_FILES + 1)]
    r = client.post("/pipelines/import/upload", files=files, data={"dryRun": "true"})
    assert r.status_code == 400
    assert "too many files" in r.json()["detail"]
