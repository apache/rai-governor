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

For scans that remember (triage): ``search`` takes ``include_inactive`` (to
see findings a person demoted as false positives), ``origins`` and
``disposition``, and ``domain="*"`` for every domain the run reads; each hit
carries ``disposition``, ``why``, ``decided_by`` and ``decided_at``.
``guidance(repo, scanner)`` returns the guidance a scan loads,
``used(ids, how)`` records that the run loaded or cited it, and
``annotate(id, evidence)`` puts what the scan found about a finding where
triage shows it.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional

from . import access
from .entry import Confidence, EntryType
from .service import NotFound, WriteRequest, _CURRENT_RUN, agent_author, get_memory_service


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
        viewing_as_user: bool = False,
    ) -> None:
        self.enabled = memory_enabled()
        self.project = project if project and access.project_of(project) else None
        if self.project:
            # Not the launcher's reach: what a project run reads ends up in
            # the project's output.
            domains = [self.project] + access.open_domains()
            writable = [self.project]
        else:
            # A site admin viewing as a regular user isn't a MEMORY_WRITER.
            shared_writer = is_site_admin or (
                not viewing_as_user and (user_id or "") in _csv("MEMORY_WRITERS"))
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
        include_inactive: bool = False,
        origins: Optional[List[str]] = None,
        disposition: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Summaries, best match first. Never bodies. Live entries only,
        unless include_inactive. ``domain="*"``: every domain the run reads,
        each hit with its ``domain``."""
        filters: Dict[str, Any] = {}
        if type:
            filters["type"] = EntryType(type).value
        if trust_tier:
            filters["trust_tier"] = trust_tier
        if origin:
            filters["origin"] = origin
        service = self._service()
        targets = list(self.domains) if domain == "*" else [self._domain(domain)]
        out: List[Dict[str, Any]] = []
        for d in targets:
            hits = await service.search(d, query, filters, None, limit,
                                        include_inactive=include_inactive,
                                        origins=list(origins) if origins else None,
                                        disposition=disposition)
            out += [{**h, "domain": d} for h in hits]
        return out[:max(1, limit)] if domain == "*" else out

    async def guidance(self, repo: str, scanner: Optional[str] = None,
                       min_trust: str = "human") -> Dict[str, Any]:
        """The guidance a scan of ``repo`` (owner/repo) by ``scanner`` (asvs,
        glasswing) loads, from every domain the run reads: ``{"entries":
        [...], "digest": {...} or None}``. Entries carry body and chars,
        Always use (policy) first. A person's guidance counts at any tier and
        an agent's once curated; min_trust="curated" asks curation of all."""
        from .triage import Triage
        return await Triage(self._service()).guidance_for(list(self.domains), repo, scanner, min_trust)

    async def used(self, entry_ids: List[str], how: str = "loaded") -> int:
        """Record that this run loaded (or cited) these guidance entries."""
        from .triage import Triage
        ids = [i for i in entry_ids if isinstance(i, str)]
        readable = []
        for i in ids:
            if await self.get(i) is not None:
                readable.append(i)
        return await Triage(self._service()).used(readable, _CURRENT_RUN.get(), how,
                                                  self._agent_name)

    async def annotate(self, entry_id: str, evidence: Dict[str, Any]) -> None:
        """Put what the scan found about a finding (matches, the patch
        verdict, a suggested disposition) on its index row, for triage.
        Only in a domain this run writes."""
        from .triage import Triage
        entry = await self.get(entry_id)
        if entry is None:
            raise ValueError(f"no entry {entry_id} this run can read")
        if entry["frontmatter"]["domain"] not in self._writable:
            raise PermissionError(f"this run may not annotate entries in {entry['frontmatter']['domain']}")
        await Triage(self._service()).annotate(entry_id, evidence)

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
