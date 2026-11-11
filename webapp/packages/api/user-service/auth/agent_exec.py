"""Who may run agent code (ISSUE-023, stage 1).

Agent code runs with ``exec()`` inside the API process, so whoever runs it
can read the process environment, use the instance role, and reach the
database with the API's credentials. Until it runs out of process (stage 2),
only trusted users may run it.

Policy, from settings:

- ``AGENT_EXEC_ALLOWED=site_admins`` (default): site admins, plus any uid in
  ``AGENT_EXEC_ALLOWED_UIDS``.
- ``AGENT_EXEC_ALLOWED=all_users``: any signed-in user.

An unknown value is treated as ``site_admins``, so a typo fails closed.
"""

from __future__ import annotations

from typing import Optional

from config import settings

DENIED_DETAIL = (
    "Running agents is limited to site administrators on this server while "
    "agent code runs inside the API process (ISSUE-023). Ask a site "
    "administrator for access."
)


def can_execute_agent_code(user: Optional[dict]) -> bool:
    if not user:
        return False
    if settings.AGENT_EXEC_ALLOWED == "all_users":
        return True
    if user.get("is_site_admin") is True:
        return True
    # A site admin viewing as a regular user gets none of the uid list.
    if user.get("viewing_as_user"):
        return False
    uid = user.get("uid")
    return bool(uid) and uid in settings.AGENT_EXEC_ALLOWED_UIDS
