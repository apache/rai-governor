"""A session's ASF memberships are re-read from LDAP during the session
(FINDING-018): one LDAP query; None when LDAP fails, so the session keeps
what it had; a ban since login is reported."""

import pytest

from auth.base import UserInfo
from auth.ldap_client import AsfMembershipSnapshot
from auth.providers.asf import AsfProvider


class FakeLdap:
    def __init__(self, snapshot):
        self.snapshot, self.calls = snapshot, 0

    def get_memberships(self, uid):
        self.calls += 1
        return self.snapshot


def _refresh(snapshot):
    p = AsfProvider({"allowed_projects": ["tooling", "httpd"]})
    p._ldap = FakeLdap(snapshot)
    import asyncio
    out = asyncio.run(p.refresh_memberships(UserInfo(provider_type="asf", external_id="akm",
                                                    display_name="akm", email=None)))
    return out, p._ldap.calls


def test_memberships_come_from_one_ldap_query():
    out, calls = _refresh(AsfMembershipSnapshot(committer_groups={"tooling", "httpd"},
                                                pmc_groups={"tooling"}, query_succeeded=True))
    assert calls == 1
    assert not out.banned
    assert {(m.workspace_id, m.role) for m in out.memberships} == {
        ("project:tooling", "admin"), ("project:httpd", "member")}


def test_an_ldap_failure_is_none_not_no_memberships():
    out, _ = _refresh(AsfMembershipSnapshot(query_succeeded=False))
    assert out is None


def test_a_ban_is_reported():
    out, _ = _refresh(AsfMembershipSnapshot(committer_groups={"tooling"}, is_banned=True,
                                            query_succeeded=True))
    assert out.banned
