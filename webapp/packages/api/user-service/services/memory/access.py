"""Who may read, write and review which memory domain (ISSUE-002).

Two kinds of domain:

- **Shared** domains are the ones in ``MEMORY_DOMAINS`` (default
  ``dom-scan``). Site admins and ``MEMORY_WRITERS`` read, write and review
  them. Everyone else signed in reads only those also listed in
  ``MEMORY_OPEN_DOMAINS`` (default none): what admin runs record there can
  come from private repositories.
- A **project** domain is named for the project, by its LDAP name
  (``whimsy``), the same name llmao uses. It exists from its first write.
  The project's members read it; its PMC and site admins write and review
  it; an agent running for the project writes into it at raw.

Nobody reviews an entry they wrote; that rule is the service's.
"""

from __future__ import annotations

import os
from typing import List, Optional

from auth.project_access import PROJECT_RE, is_admin, is_member, is_site_admin


def _csv(name: str, default: str = "") -> List[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


def shared_domains() -> List[str]:
    return _csv("MEMORY_DOMAINS", "dom-scan")


def open_domains() -> List[str]:
    return [d for d in _csv("MEMORY_OPEN_DOMAINS") if d in shared_domains()]


def is_shared(domain: str) -> bool:
    return domain in shared_domains()


def project_of(domain: str) -> Optional[str]:
    """The project a domain belongs to, or None for a shared or bad name.
    ``dom-`` names are kept for shared domains, so a mistyped one is a 404,
    not a new project domain; no ASF project name starts with it."""
    if not domain or is_shared(domain) or domain.startswith("dom-") or not PROJECT_RE.match(domain):
        return None
    return domain


def exists(domain: str) -> bool:
    return is_shared(domain) or project_of(domain) is not None


def _shared_writer(user: Optional[dict]) -> bool:
    if is_site_admin(user):
        return True
    # Viewing as a regular user drops MEMORY_WRITERS too.
    return not (user or {}).get("viewing_as_user") and (user or {}).get("uid") in _csv("MEMORY_WRITERS")


def can_read(user: Optional[dict], domain: str) -> bool:
    if is_shared(domain):
        return _shared_writer(user) or domain in open_domains()
    project = project_of(domain)
    return project is not None and is_member(user, project)


def can_write(user: Optional[dict], domain: str) -> bool:
    """Writing by hand, and reviewing."""
    if is_shared(domain):
        return _shared_writer(user)
    project = project_of(domain)
    return project is not None and is_admin(user, project)


can_review = can_write
