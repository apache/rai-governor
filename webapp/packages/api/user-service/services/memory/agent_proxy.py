"""``memory`` in agent code: search, get, recall and write (ISSUE-033).

Every agent run gets one. ``memory.enabled`` is False unless the API has
MEMORY_ENABLED on, and then every call raises, so agent code checks it first:

    if memory.enabled:
        await memory.write(summary="...", body="...", model=model)

Agents write at raw trust like everyone else, as ``agent:<name> (model: <id>)``.

Which domains a run reaches (ISSUE-002, rules in access.py):

- A run for a project reads and writes that project's domain, and it is the
  default. Its launcher was checked as a member when the run started. Of
  the shared domains it reads only the MEMORY_OPEN_DOMAINS ones, whoever
  launched it: its output is the project's, and a closed shared domain
  holds what admin runs found in other projects' private repositories.
- A run for no project, as before: the shared domains the launcher could
  reach by hand (site admins and MEMORY_WRITERS read and write them, anyone
  reads the open ones), first one default.

A write to a domain the run can't write raises PermissionError.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional

from . import access
from .entry import Confidence, EntryType
from .service import NotFound, WriteRequest, agent_author, get_memory_service


def _csv(name: str, default: str = "") -> List[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


def memory_enabled() -> bool:
    return os.getenv("MEMORY_ENABLED", "false").lower() == "true"


class MemoryOff(RuntimeError):
    pass


class AgentMemory:
    def __init__(
        self,
        db,
        agent_name: str,
        user_id: Optional[str] = None,
        is_site_admin: bool = False,
        user_service=None,
        project: Optional[str] = None,
    ) -> None:
        self.enabled = memory_enabled()
        self.project = project if project and access.project_of(project) else None
        if self.project:
            # Not the launcher's reach: what a project run reads ends up in
            # the project's output.
            domains = [self.project] + access.open_domains()
            writable = [self.project]
        else:
            shared_writer = is_site_admin or (user_id or "") in _csv("MEMORY_WRITERS")
            domains = [d for d in access.shared_domains()
                       if shared_writer or d in access.open_domains()]
            writable = access.shared_domains() if shared_writer else []
        # The default domain is first.
        self.domains = domains if self.enabled else []
        self._writable = set(writable)
        self.can_write = self.enabled and bool(self._writable)
        self._db = db
        self._agent_name = agent_name
        self._user_id = user_id
        self._user_service = user_service
        # What this run wrote, for a summary at the end of a pipeline.
        self.written: List[str] = []

    def _service(self):
        if not self.enabled:
            raise MemoryOff("memory is off on this deployment (MEMORY_ENABLED)")
        return get_memory_service(self._db)

    def _domain(self, domain: Optional[str]) -> str:
        if not self.enabled:
            raise MemoryOff("memory is off on this deployment (MEMORY_ENABLED)")
        if not self.domains:
            raise PermissionError("this run can reach no memory domain")
        domain = domain or self.domains[0]
        if domain not in self.domains:
            if access.exists(domain):
                raise PermissionError(f"this run may not reach memory domain {domain!r}; it has {self.domains}")
            raise ValueError(f"no memory domain {domain!r}; this run has {self.domains}")
        return domain

    async def search(
        self,
        query: Optional[str] = None,
        domain: Optional[str] = None,
        type: Optional[str] = None,
        trust_tier: Optional[str] = None,
        origin: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Summaries of live entries, best match first. Never bodies."""
        filters: Dict[str, Any] = {}
        if type:
            filters["type"] = EntryType(type).value
        if trust_tier:
            filters["trust_tier"] = trust_tier
        if origin:
            filters["origin"] = origin
        return await self._service().search(self._domain(domain), query, filters, None, limit)

    async def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """``{"frontmatter", "body"}``, or None if there is no such entry."""
        try:
            entry = await self._service().get(entry_id)
        except NotFound:
            return None
        if entry.front.domain not in self.domains:
            return None
        return {"frontmatter": entry.front.to_dict(), "body": entry.body}

    async def recall(self, question: str, domain: Optional[str] = None) -> Dict[str, Any]:
        """A cited brief: ``{"brief", "cited", "entries", "matched"}``. Billed to the run's user."""
        return await self._service().recall(
            question, self._domain(domain), user_service=self._user_service, user_id=self._user_id
        )

    async def write(
        self,
        summary: str,
        body: str,
        type: str = "fact",
        domain: Optional[str] = None,
        title: Optional[str] = None,
        source: Optional[List[str]] = None,
        confidence: Optional[str] = None,
        review_by: Optional[str] = None,
        supersedes: Optional[str] = None,
        origin: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Write an entry at raw trust and return its id.

        ``model`` is the model that produced it, for the author line.
        ``origin`` groups entries from one source (a run, a repo) so a later
        run can find and supersede them. ``review_by`` is YYYY-MM-DD.
        """
        domain = self._domain(domain)
        if domain not in self._writable:
            raise PermissionError(
                f"this run may not write to {domain}: a project's runs write to its "
                "domain, and shared domains take site admins' and MEMORY_WRITERS' runs"
            )
        req = WriteRequest(
            domain=domain,
            type=EntryType(type),
            body=body,
            summary=" ".join((summary or "").split())[:300],
            title=title,
            source=list(source or []),
            confidence=Confidence(confidence) if confidence else None,
            review_by=date.fromisoformat(review_by) if review_by else None,
            supersedes=supersedes,
            origin=origin,
        )
        entry = await self._service().write(req, author=agent_author(self._agent_name, model))
        self.written.append(entry.front.id)
        return entry.front.id
