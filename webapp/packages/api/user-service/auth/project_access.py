"""Who may do what with a project's runs, data and memory (ISSUE-002).

The rules are llmao's (``seam.Identity``): a member of a project is a
committer or PMC member of it, an admin is a PMC member, and a site admin is
both, everywhere. Memberships come from the session's workspaces: LDAP gives
``project:<name>`` with role ``member`` for committers and ``admin`` for the
PMC. Project names are the LDAP names, the same ones llmao uses.

Agents, pipelines, deployments and demo apps are not here: those are site
admin work (``routes.get_author_user``).
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Set

from fastapi import HTTPException

from models.workspace import is_project_workspace, make_project_workspace_id

# LDAP project names: lowercase letters, digits and hyphens. \Z, not $:
# $ also matches before a trailing newline.
PROJECT_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")


def _roles(user: Optional[dict]) -> Dict[str, str]:
    """project name -> role, from the session's workspace memberships."""
    out: Dict[str, str] = {}
    for w in (user or {}).get("workspaces") or []:
        wid = w.get("workspaceId") or w.get("workspace_id") or ""
        if is_project_workspace(wid):
            name = wid.split(":", 1)[1]
            # admin wins if a project somehow appears twice
            if out.get(name) != "admin":
                out[name] = w.get("role") or "member"
    return out


def is_site_admin(user: Optional[dict]) -> bool:
    return bool((user or {}).get("is_site_admin"))


def member_projects(user: Optional[dict]) -> Set[str]:
    """Projects the user belongs to by membership. Site admins get no
    extras here, so lists don't fill with every project's runs; their
    access to any one project comes from is_member."""
    return set(_roles(user))


def is_member(user: Optional[dict], project: Optional[str]) -> bool:
    if not project:
        return False
    return is_site_admin(user) or project in _roles(user)


def is_admin(user: Optional[dict], project: Optional[str]) -> bool:
    if not project:
        return False
    return is_site_admin(user) or _roles(user).get(project) == "admin"


def check_project(user: Optional[dict], project: Optional[str]) -> Optional[str]:
    """Validate a project a request names. None passes through (a run for
    nobody but its owner). 400 on a malformed name, 403 when the caller
    isn't a member."""
    if project is None or project == "":
        return None
    if not PROJECT_RE.match(project):
        raise HTTPException(status_code=400, detail=f"Not a project name: {project!r}")
    if not is_member(user, project):
        raise HTTPException(status_code=403, detail=f"Not a member of {project}")
    return project


def data_owner(user: Optional[dict], project: Optional[str]) -> str:
    """Whose data-store records a run reads and writes: the project's when
    it runs for one, else the user's own, as before."""
    if project:
        return make_project_workspace_id(project)
    return (user or {}).get("uid") or "anonymous"


def can_see_run(user: Optional[dict], run_user_id: Optional[str], run_project: Optional[str]) -> bool:
    uid = (user or {}).get("uid")
    if uid and run_user_id == uid:
        return True
    return is_member(user, run_project)
