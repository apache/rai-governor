"""ISSUE-023 stage 1: only allowed users may run agent code.

Every route that reaches ``_execute_agent_code`` is driven through the real
app. A denied caller gets 403 before any agent code, database or registry
call happens; an allowed one gets past the gate.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import routes
from app_factory import create_app
from auth.agent_exec import DENIED_DETAIL, can_execute_agent_code
from config import settings
from dependencies import get_db, get_logger
from routes import get_current_user

PLAIN = {"uid": "asf:plain", "email": "plain@example.com"}
LISTED = {"uid": "asf:listed", "email": "listed@example.com"}
ADMIN = {"uid": "asf:admin", "email": "admin@example.com", "is_site_admin": True}

RUN_CODE_BODY = {
    "code": "async def run(input_dict, tools):\n    return {'outputText': 'hi'}\n",
    "inputDict": {},
    "tools": {},
    "gofannonAgents": [],
}

# (method, path, json body) for each route that runs agent code.
EXEC_ROUTES = [
    ("POST", "/agents/run-code", RUN_CODE_BODY),
    ("POST", "/agents/run-code/stream", RUN_CODE_BODY),
    ("POST", "/agents/run-code/start", RUN_CODE_BODY),
    ("POST", "/rest/some-agent", {"inputText": "hi"}),
    ("POST", "/rest/some-agent/start", {"inputText": "hi"}),
    (
        "POST",
        "/chat",
        {"messages": [{"role": "user", "content": "hi"}], "provider": "gofannon", "model": "some-agent"},
    ),
]


@pytest.fixture
def policy(monkeypatch):
    def set_policy(allowed="site_admins", uids=()):
        monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", allowed)
        monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED_UIDS", frozenset(uids))

    set_policy()
    return set_policy


def _client(user):
    app = create_app()

    def current(request: Request):
        request.state.user = user
        return user

    app.dependency_overrides[get_current_user] = current
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[get_logger] = lambda: _NullLogger()
    return TestClient(app, raise_server_exceptions=False)


class _NullLogger:
    def log(self, *args, **kwargs):
        pass

    def log_exception(self, *args, **kwargs):
        pass


@pytest.mark.parametrize("method,path,body", EXEC_ROUTES, ids=[p for _, p, _ in EXEC_ROUTES])
def test_plain_user_gets_403_and_no_code_runs(policy, method, path, body):
    with patch.object(routes, "_execute_agent_code", new_callable=AsyncMock) as exec_mock, \
         patch.object(routes, "run_deployed_agent_logic", new_callable=AsyncMock) as deployed_mock, \
         patch.object(routes, "process_chat", new_callable=AsyncMock) as chat_mock:
        r = _client(PLAIN).request(method, path, json=body)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == DENIED_DETAIL
    exec_mock.assert_not_awaited()
    deployed_mock.assert_not_awaited()
    chat_mock.assert_not_awaited()


@pytest.mark.parametrize("method,path,body", EXEC_ROUTES, ids=[p for _, p, _ in EXEC_ROUTES])
def test_site_admin_gets_past_the_gate(policy, method, path, body):
    with patch.object(routes, "_execute_agent_code", new_callable=AsyncMock) as exec_mock, \
         patch.object(routes, "run_deployed_agent_logic", new_callable=AsyncMock) as deployed_mock, \
         patch.object(routes, "process_chat", new_callable=AsyncMock):
        exec_mock.return_value = ({"outputText": "hi"}, [])
        deployed_mock.return_value = {"outputText": "hi"}
        r = _client(ADMIN).request(method, path, json=body)
    # Past the gate the stubs fail in their own ways; only a 403 is a gate failure.
    assert r.status_code != 403, r.text


def test_chat_with_a_model_provider_is_not_gated(policy):
    body = {"messages": [{"role": "user", "content": "hi"}], "provider": "openai", "model": "gpt-4o-mini"}
    with patch.object(routes, "process_chat", new_callable=AsyncMock):
        r = _client(PLAIN).post("/chat", json=body)
    assert r.status_code == 200, r.text


def test_listed_uid_and_all_users(policy):
    assert not can_execute_agent_code(LISTED)
    policy(uids=["asf:listed"])
    assert can_execute_agent_code(LISTED)
    assert not can_execute_agent_code(PLAIN)
    policy(allowed="all_users")
    assert can_execute_agent_code(PLAIN)


def test_unknown_policy_value_fails_closed(policy):
    policy(allowed="everyone")
    assert not can_execute_agent_code(PLAIN)
    assert can_execute_agent_code(ADMIN)


def test_no_user_and_truthy_non_bool_admin_flag_are_denied(policy):
    assert not can_execute_agent_code(None)
    assert not can_execute_agent_code({})
    assert not can_execute_agent_code({"uid": "x", "is_site_admin": "true"})
