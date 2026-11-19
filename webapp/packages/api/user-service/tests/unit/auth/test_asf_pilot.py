"""ASF sign-in for a pilot: ``allowed_projects`` in the provider's config
lists the projects whose committers and PMC members may sign in. Without
it, only site admins get in, as before. A pilot member's workspaces are the
pilot's projects only; a ban still refuses everyone."""

import asyncio

import pytest

from auth.base import LoginAllow, LoginDeny, UserInfo
from auth.ldap_client import AsfMembershipSnapshot
from auth.providers.asf import AsfProvider
from config import settings

ADMIN = "asf:root"


class FakeLdap:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_memberships(self, uid):
        return self.snapshot


@pytest.fixture(autouse=True)
def site_admins(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_CONFIG", {"site_admins": [ADMIN]})


def _snap(committer=(), pmc=(), banned=False):
    return AsfMembershipSnapshot(committer_groups=set(committer), pmc_groups=set(pmc),
                                 is_banned=banned, query_succeeded=True)


def _login(snapshot, uid="alice", **config):
    """The memberships a login gets and the provider's decision."""
    p = AsfProvider(config)
    p._ldap = FakeLdap(snapshot)
    who = UserInfo(provider_type="asf", external_id=uid, display_name=uid, email=None)
    memberships = asyncio.run(p.get_workspace_memberships(who))
    decision = asyncio.run(p.evaluate_login(who, memberships, [ADMIN]))
    return {(m.workspace_id, m.role) for m in memberships}, decision


def _refresh(snapshot, uid="alice", **config):
    p = AsfProvider(config)
    p._ldap = FakeLdap(snapshot)
    return asyncio.run(p.refresh_memberships(
        UserInfo(provider_type="asf", external_id=uid, display_name=uid, email=None)))


@pytest.mark.parametrize("config", [{}, {"allowed_projects": []}, {"allowed_projects": None}])
def test_without_a_pilot_only_site_admins_get_in(config):
    _, d = _login(_snap(committer={"tooling"}, pmc={"tooling"}), **config)
    assert isinstance(d, LoginDeny)
    _, d = _login(_snap(committer={"tooling"}), uid="root", **config)
    assert isinstance(d, LoginAllow) and d.site_admin


def test_a_pilot_projects_committer_gets_in_as_a_regular_user_with_only_pilot_workspaces():
    ws, d = _login(_snap(committer={"tooling", "httpd"}, pmc={"httpd"}),
                   allowed_projects=["tooling", "whimsy"])
    assert isinstance(d, LoginAllow) and not d.site_admin
    assert ws == {("project:tooling", "member")}


def test_a_pilot_projects_pmc_member_gets_in_as_its_admin():
    ws, d = _login(_snap(committer={"whimsy"}, pmc={"whimsy"}), allowed_projects=["tooling", "whimsy"])
    assert isinstance(d, LoginAllow) and not d.site_admin
    assert ws == {("project:whimsy", "admin")}


def test_a_member_of_other_projects_only_is_refused_without_naming_any():
    ws, d = _login(_snap(committer={"httpd", "tomcat"}, pmc={"tomcat"}),
                   allowed_projects=["tooling", "whimsy"])
    assert ws == set()
    assert isinstance(d, LoginDeny)
    assert "pilot projects" in d.reason
    for name in ("tooling", "whimsy", "httpd", "tomcat"):
        assert name not in d.reason.lower()


def test_a_banned_pilot_member_and_a_banned_site_admin_are_refused():
    _, d = _login(_snap(committer={"tooling"}, banned=True), allowed_projects=["tooling"])
    assert isinstance(d, LoginDeny) and "suspended" in d.reason
    _, d = _login(_snap(committer={"tooling"}, banned=True), uid="root", allowed_projects=["tooling"])
    assert isinstance(d, LoginDeny) and "suspended" in d.reason


def test_site_admins_keep_all_their_projects():
    ws, d = _login(_snap(committer={"httpd"}, pmc={"httpd"}), uid="root", allowed_projects=["tooling"])
    assert isinstance(d, LoginAllow) and d.site_admin
    assert ws == {("project:httpd", "admin")}


def test_project_names_are_matched_without_case_or_spaces():
    _, d = _login(_snap(committer={"tooling"}), allowed_projects=[" Tooling "])
    assert isinstance(d, LoginAllow)


def test_allowed_projects_must_be_a_list():
    # A bare string would otherwise be read letter by letter.
    with pytest.raises(ValueError, match="allowed_projects"):
        AsfProvider({"allowed_projects": "tooling"})


def test_a_session_whose_user_left_the_pilot_is_refused_at_refresh():
    out = _refresh(_snap(committer={"httpd"}), allowed_projects=["tooling"])
    assert out.refused and out.memberships == []
    out = _refresh(_snap(committer={"tooling", "httpd"}), allowed_projects=["tooling"])
    assert not out.refused and [m.workspace_id for m in out.memberships] == ["project:tooling"]
    # A site admin's session never is.
    out = _refresh(_snap(), uid="root", allowed_projects=["tooling"])
    assert not out.refused
