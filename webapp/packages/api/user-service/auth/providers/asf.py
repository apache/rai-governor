# webapp/packages/api/user-service/auth/providers/asf.py
"""ASF auth provider: oauth.apache.org + ldap-eu.apache.org.

Flow:
    1. Browser redirected to ``OAUTH_URL_INIT`` with state.
    2. User approves at oauth.apache.org.
    3. Browser lands back on ``/auth/callback/asf?code=...&state=...``.
    4. ``exchange_code`` POSTs the code to ``OAUTH_URL_CALLBACK`` and
       parses the user info from the response.
    5. ``get_workspace_memberships`` queries LDAP for committer and PMC
       groups, and the banned list.

The two steps (OAuth + LDAP) happen sequentially in the auth route:
first ``exchange_code`` → then ``get_workspace_memberships``. ASF is
the outlier here — most providers put group info in the OAuth response
itself — but the cost is one extra LDAP query per login (~50ms).
"""
import asyncio
from typing import List, Optional
from urllib.parse import urlencode

import httpx

from config import settings

from ..base import (
    AuthProvider,
    LoginAllow,
    LoginDecision,
    LoginDeny,
    Membership,
    MembershipRefresh,
    UserInfo,
)
from ..ldap_client import LdapClient

# Shown to a committer none of whose projects is in ``allowed_projects``.
# Names no project: neither theirs nor the pilot's.
NOT_IN_PILOT = (
    "Governor is open to a few pilot projects for now, and you are not a "
    "committer or PMC member of one of them."
)

# Default endpoints; overridable via provider config.
OAUTH_URL_INIT = "https://oauth.apache.org/auth"
OAUTH_URL_CALLBACK = "https://oauth.apache.org/token"

# Optional per-project display-name overrides. Callers can extend via
# AUTH_CONFIG["providers"][asf]["config"]["project_display_names"].
# Keys are project slugs; values are human-readable names.
DEFAULT_PROJECT_DISPLAY_NAMES = {
    # A small seed set; extend via config.
    "httpd":   "Apache HTTP Server",
    "tomcat":  "Apache Tomcat",
    "kafka":   "Apache Kafka",
    "spark":   "Apache Spark",
    "beam":    "Apache Beam",
    "airflow": "Apache Airflow",
}


class AsfProvider(AuthProvider):
    type = "asf"

    def __init__(self, config: dict):
        super().__init__(config)
        self.oauth_init = config.get("oauth_init_url", OAUTH_URL_INIT)
        self.oauth_callback = config.get("oauth_callback_url", OAUTH_URL_CALLBACK)
        self.display_names = {
            **DEFAULT_PROJECT_DISPLAY_NAMES,
            **(config.get("project_display_names") or {}),
        }
        # The pilot: projects whose committers and PMC members may sign in.
        # Empty means site admins only.
        allowed = config.get("allowed_projects") or []
        if not isinstance(allowed, list):
            raise ValueError("asf: allowed_projects must be a list of project names")
        self.allowed_projects = {str(p).strip().lower() for p in allowed if str(p).strip()}
        # LDAP config; the client's defaults match production ldap-eu.apache.org.
        ldap_cfg = config.get("ldap") or {}
        self._ldap = LdapClient(
            server=ldap_cfg.get("server", "ldaps://ldap-eu.apache.org"),
            committer_base=ldap_cfg.get(
                "committer_base", "ou=project,ou=groups,dc=apache,dc=org"
            ),
            pmc_base=ldap_cfg.get(
                "pmc_base", "ou=pmc,ou=project,ou=groups,dc=apache,dc=org"
            ),
            banned_group=ldap_cfg.get(
                "banned_group", "cn=asf-banned,ou=groups,dc=apache,dc=org"
            ),
            bind_dn=ldap_cfg.get("bind_dn"),
            bind_password=ldap_cfg.get("bind_password"),
            timeout_seconds=ldap_cfg.get("timeout_seconds", 10),
        )

    @property
    def display_name(self) -> str:
        return self.config.get("display_name", "Apache Software Foundation")

    @property
    def icon_hint(self) -> Optional[str]:
        return "asf"

    def get_authorize_url(self, state: str, redirect_uri: str) -> str:
        """Redirect URL for the ASF OAuth authorize leg.

        oauth.apache.org uses a non-standard flow: it takes ``state`` and
        ``redirect_uri`` directly as query params (no client_id / scopes).
        """
        params = urlencode({
            "state": state,
            "redirect_uri": redirect_uri,
        })
        return f"{self.oauth_init}?{params}"

    async def exchange_code(self, code: str, redirect_uri: str) -> UserInfo:
        """Exchange the OAuth ``code`` for user identity.

        oauth.apache.org returns JSON with at least ``uid``, ``fullname``,
        ``email``. Field shape has varied historically — we defend against
        missing fields with sensible fallbacks and raise on a missing uid.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                self.oauth_callback,
                params={"code": code},
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"ASF OAuth token exchange failed: {resp.status_code} {resp.text}"
                )
            data = resp.json()

        uid = data.get("uid")
        if not uid:
            raise RuntimeError(f"ASF OAuth response missing 'uid': {data}")

        return UserInfo(
            provider_type=self.type,
            external_id=uid,
            display_name=data.get("fullname") or uid,
            email=data.get("email"),
        )

    async def get_workspace_memberships(self, user_info: UserInfo) -> List[Membership]:
        """Query LDAP for committer + PMC memberships.

        The actual LDAP call is synchronous (ldap3 has no async API);
        we run it in a thread executor so it doesn't block the event
        loop. Ordering inside the returned list doesn't matter; the
        session service sorts by display_name when rendering.
        """
        loop = asyncio.get_event_loop()
        snapshot = await loop.run_in_executor(
            None,
            self._ldap.get_memberships,
            user_info.external_id,
        )

        # Soft-fail: LDAP unavailable. Return empty — evaluate_login
        # handles the policy (deny-on-no-memberships unless site admin).
        # The on-refresh path in SessionService has its own fallback
        # that preserves existing memberships when query_succeeded=False.
        if not snapshot.query_succeeded:
            return []
        return self._memberships_from(snapshot, user_info.uid)

    def _in_pilot(self, project: str) -> bool:
        return project in self.allowed_projects

    @staticmethod
    def _site_admin(uid: str) -> bool:
        return uid in ((settings.AUTH_CONFIG or {}).get("site_admins") or [])

    def _memberships_from(self, snapshot, uid: str) -> List[Membership]:
        """The user's projects as workspaces. For anyone but a site admin,
        only the pilot's: a pilot member's other projects get no runs,
        workloads or memory here."""
        site_admin = self._site_admin(uid)
        memberships: List[Membership] = []
        # A PMC member is normally in the committer group too; either counts.
        for project in sorted(snapshot.committer_groups | snapshot.pmc_groups):
            if not site_admin and not self._in_pilot(project):
                continue
            # Intersection with PMC groups upgrades role to admin.
            role = "admin" if project in snapshot.pmc_groups else "member"
            source = "ldap_pmc" if role == "admin" else "ldap_committer"
            display = self.display_names.get(project, f"Apache {project.title()}")
            memberships.append(Membership(
                workspace_id=f"project:{project}",
                role=role,
                display_name=display,
                source=source,
            ))
        return memberships

    async def refresh_memberships(self, user_info: UserInfo) -> Optional[MembershipRefresh]:
        """LDAP needs no user token, so a session's memberships, site-admin
        flag and ban are re-read in the background. None if LDAP fails."""
        loop = asyncio.get_event_loop()
        snapshot = await loop.run_in_executor(None, self._ldap.get_memberships, user_info.external_id)
        if not snapshot.query_succeeded:
            return None
        if snapshot.is_banned:
            return MembershipRefresh(memberships=[], banned=True)
        memberships = self._memberships_from(snapshot, user_info.uid)
        # No longer in a pilot project (or the pilot changed): login would
        # be refused now, so the session ends.
        return MembershipRefresh(memberships=memberships,
                                 refused=not memberships and not self._site_admin(user_info.uid))

    async def evaluate_login(
        self,
        user_info: UserInfo,
        memberships: List[Membership],
        site_admins: List[str],
    ) -> LoginDecision:
        """ASF-specific login policy.

        Layered on top of the default policy:
          - banned users are always denied (site-admin allowlist does not
            override a ban — we never want site admin bypass of bans)
          - emeritus committers (uid valid but no memberships and not
            site-admin) are denied with a specific message
          - site admin without memberships: allowed with site_admin=True
            (so ASF infra can still log in even if they're not on any PMC)
          - anyone else only as a committer or PMC member of a project in
            ``allowed_projects`` (the pilot); none listed, nobody else
        """
        # Re-query the banned group. We already did the full snapshot in
        # get_workspace_memberships, but we don't have the is_banned
        # field on the Membership list alone, so we run one more check.
        loop = asyncio.get_event_loop()
        snapshot = await loop.run_in_executor(
            None,
            self._ldap.get_memberships,
            user_info.external_id,
        )
        # Soft-fail: don't deny on LDAP outage. The refresh cycle will
        # catch a ban the next time LDAP is reachable.
        if snapshot.query_succeeded and snapshot.is_banned:
            return LoginDeny(reason="Your account has been suspended by ASF Infrastructure.")

        if user_info.uid in site_admins:
            return LoginAllow(site_admin=True)
        # Memberships are already only the pilot's projects (_memberships_from).
        if any(self._in_pilot(m.workspace_id.split(":", 1)[-1]) for m in memberships):
            return LoginAllow(site_admin=False)
        if snapshot.query_succeeded and (snapshot.committer_groups or snapshot.pmc_groups):
            return LoginDeny(reason=NOT_IN_PILOT)
        return LoginDeny(
            reason=(
                "Your apache.org account is valid, but you're not currently "
                "a committer on any project. Contact ASF Infrastructure if "
                "this is in error."
            )
        )
