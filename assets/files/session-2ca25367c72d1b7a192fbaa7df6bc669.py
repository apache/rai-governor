# webapp/packages/api/user-service/models/session.py
"""Server-side session model for Phase B auth.

A session is created when a user completes an OAuth flow (or dev_stub login),
stored in the ``user_sessions`` CouchDB collection, and referenced by the
``gofannon_sid`` cookie. The session carries the user's identity, provider
origin, current workspace memberships (refreshed periodically), and
site-admin flag.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field
from pydantic.config import ConfigDict

from .workspace import WorkspaceMembership
from time_utils import naive_utc_now


class Session(BaseModel):
    """A server-side session."""

    # The CouchDB _id: the SHA-256 of the gofannon_sid cookie value
    # (services.session_service.session_doc_id), not the value itself.
    id: str = Field(..., alias="_id")
    rev: Optional[str] = Field(None, alias="_rev")

    # The user's globally-unique uid, formatted as "{provider_type}:{external_id}".
    # e.g. "asf:jdoe", "dev_stub:alice".
    user_uid: str = Field(..., alias="userUid")

    # Which provider this session was authenticated against.
    # Useful for logout flows and for refreshing workspaces.
    provider_type: str = Field(..., alias="providerType")

    # Display info captured at login (for UI rendering without re-fetching
    # from the provider on every request).
    display_name: str = Field(..., alias="displayName")
    email: Optional[str] = None

    # Current workspace memberships, refreshed via the provider's
    # get_workspace_memberships hook. Includes the auto-created personal
    # workspace (source="auto_personal") plus any provider-derived ones.
    workspaces: List[WorkspaceMembership] = Field(default_factory=list)

    # True when user_uid is in the site_admins allowlist in AUTH_CONFIG.
    # Recomputed on every login/refresh so removing someone from the
    # allowlist revokes their site-admin powers at the next refresh.
    is_site_admin: bool = Field(default=False, alias="isSiteAdmin")

    # A site admin looking at the site as a regular user: every request on
    # this session drops site admin and the uid allowlists, leaving the
    # LDAP project roles in workspaces. Lasts until switched off or logout.
    viewing_as_user: bool = Field(default=False, alias="viewingAsUser")

    # Timestamps.
    created_at: datetime = Field(default_factory=naive_utc_now, alias="createdAt")
    updated_at: datetime = Field(default_factory=naive_utc_now, alias="updatedAt")
    expires_at: datetime = Field(..., alias="expiresAt")
    # The last time we re-queried the provider for workspace memberships.
    # The background refresher uses this to decide whether it needs to run.
    last_refresh_at: datetime = Field(default_factory=naive_utc_now, alias="lastRefreshAt")
    # When the session was last used, to within workspace_refresh_minutes:
    # written only when the doc is written anyway (login, the membership
    # refresh), never on every request. None on sessions from before it
    # existed; last_refresh_at stands in for it.
    last_used_at: Optional[datetime] = Field(default=None, alias="lastUsedAt")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class SessionUser(BaseModel):
    """User information returned to the frontend from /users/me when
    authenticated via a Phase B session.

    Unlike the raw Session doc (which includes internal state like
    expiry and last_refresh), this is the client-safe projection.
    """
    uid: str
    display_name: str = Field(..., alias="displayName")
    email: Optional[str] = None
    provider_type: str = Field(..., alias="providerType")
    workspaces: List[WorkspaceMembership] = Field(default_factory=list)
    is_site_admin: bool = Field(default=False, alias="isSiteAdmin")
    # May read and write agents, pipelines, deployments and demo apps, and
    # run agent code (ISSUE-023): site admins, AGENT_EXEC_ALLOWED_UIDS, or
    # everyone under AGENT_EXEC_ALLOWED=all_users. The UI leaves those
    # pages' calls out for anyone else rather than collecting 403s.
    can_author: bool = Field(default=False, alias="canAuthor")
    # Viewing as a regular user (isSiteAdmin and canAuthor are then what a
    # regular user with these workspaces gets), and whether this person may
    # switch that on: a real site admin.
    viewing_as_user: bool = Field(default=False, alias="viewingAsUser")
    can_view_as_user: bool = Field(default=False, alias="canViewAsUser")

    model_config = ConfigDict(populate_by_name=True)
