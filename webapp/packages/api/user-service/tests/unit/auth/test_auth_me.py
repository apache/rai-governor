"""/auth/me says whether the user may author (canAuthor), by the same rule
the agent, pipeline, deployment and demo routes apply (ISSUE-023)."""

from types import SimpleNamespace

import pytest

from config import settings
from routes_auth import get_me


class _Sessions:
    def __init__(self, uid, site_admin):
        self.s = SimpleNamespace(user_uid=uid, display_name=uid, email=None, provider_type="dev_stub",
                                 workspaces=[], is_site_admin=site_admin)

    async def get_by_id(self, sid):
        return self.s


@pytest.mark.parametrize("policy,uids,uid,site_admin,expected", [
    ("site_admins", [], "dev_stub:alice", False, False),
    ("site_admins", [], "dev_stub:admin", True, True),
    ("site_admins", ["dev_stub:alice"], "dev_stub:alice", False, True),
    ("all_users", [], "dev_stub:alice", False, True),
])
async def test_can_author_follows_the_agent_exec_rule(monkeypatch, policy, uids, uid, site_admin, expected):
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED", policy)
    monkeypatch.setattr(settings, "AGENT_EXEC_ALLOWED_UIDS", uids)
    me = await get_me(sid="x", session_svc=_Sessions(uid, site_admin))
    assert me.model_dump(by_alias=True)["canAuthor"] is expected
