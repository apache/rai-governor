"""Memory pilot endpoints: search, get, recall, write, and review.

Mounted only when MEMORY_ENABLED is true. Who may read, write and review a
domain is services/memory/access.py: shared domains (MEMORY_DOMAINS) are for
site admins and MEMORY_WRITERS unless listed in MEMORY_OPEN_DOMAINS; a
project's domain, named for it, is for its members to read and its PMC to
write and review (ISSUE-002). Nobody reviews an entry they wrote. A domain
the caller can't read answers 404, like one that doesn't exist, and so does
an entry in one, with the same message as a missing entry.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dependencies import get_db, get_user_service_dep
from auth.project_access import member_projects
from routes import get_current_user
from services.memory import access
from services.memory.entry import Confidence, EntryError, EntryType
from services.memory.service import (
    NotFound,
    OwnEntry,
    Refused,
    ReviewAction,
    WriteRequest,
    get_memory_service,
    human_author,
)

router = APIRouter(prefix="/memory", tags=["memory"])


def _domain(domain: str, user: dict) -> str:
    """The domain, if it exists and the caller may read it; else 404."""
    if not access.exists(domain) or not access.can_read(user, domain):
        raise HTTPException(status_code=404, detail=f"No memory domain {domain!r}")
    return domain


def _entry_domain(domain: str, user: dict, missing: str = "No such entry") -> str:
    """An entry's domain, if the caller may read it; else the 404 a missing
    entry gets, so the answer doesn't say the id exists elsewhere."""
    if not access.exists(domain) or not access.can_read(user, domain):
        raise HTTPException(status_code=404, detail=missing)
    return domain


def _writable(domain: str, user: dict) -> str:
    """For writing by hand and reviewing: 404 if unreadable, 403 if read-only."""
    _domain(domain, user)
    if not access.can_write(user, domain):
        raise HTTPException(status_code=403, detail=f"Not allowed to write or review {domain}")
    return domain


class WriteBody(BaseModel):
    type: EntryType = EntryType.FACT
    body: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1, max_length=300)
    title: Optional[str] = None
    source: List[str] = Field(default_factory=list)
    confidence: Optional[Confidence] = None
    review_by: Optional[date] = None
    supersedes: Optional[str] = None
    origin: Optional[str] = None


class RecallBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


class ReviewBody(BaseModel):
    action: ReviewAction
    why: str = Field("", max_length=2000)
    review_by: Optional[date] = None


@router.get("/domains")
async def domains(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """The domains the caller may read, with counts and what they may do in each.

    Shared domains first, then project domains: the caller's projects, and
    for site admins every project domain that has entries.
    """
    service = get_memory_service(db)
    names = [d for d in access.shared_domains() if access.can_read(user, d)]
    projects = set(member_projects(user))
    if user.get("is_site_admin"):
        projects |= {d for d in await service.domains() if access.project_of(d)}
    names += sorted(p for p in projects if access.project_of(p))
    out = []
    for name in names:
        can = access.can_write(user, name)
        out.append({
            "domain": name,
            "kind": "shared" if access.is_shared(name) else "project",
            "canWrite": can,
            "canReview": can,
            **await service.counts(name),
        })
    any_can = any(d["canWrite"] for d in out)
    return {
        "domains": out,
        "you": {"author": human_author(user["uid"]), "canWrite": any_can, "canReview": any_can},
    }


@router.get("/{domain}/search")
async def search(
    domain: str,
    q: Optional[str] = None,
    type: Optional[EntryType] = None,
    trust_tier: Optional[str] = None,
    due_by: Optional[date] = None,
    include_inactive: bool = False,
    order: str = Query("newest", pattern="^(newest|oldest)$"),
    limit: int = Query(20, ge=1, le=200),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """include_inactive adds demoted and superseded entries; order=oldest is the review queue's."""
    filters: Dict[str, Any] = {}
    if type:
        filters["type"] = type.value
    if trust_tier:
        filters["trust_tier"] = trust_tier
    hits = await get_memory_service(db).search(
        _domain(domain, user), q, filters, due_by, limit,
        include_inactive=include_inactive, oldest_first=order == "oldest",
    )
    return {"domain": domain, "query": q, "hits": hits}


@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    try:
        entry = await get_memory_service(db).get(entry_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="No such entry")
    _entry_domain(entry.front.domain, user)
    return {"frontmatter": entry.front.to_dict(), "body": entry.body}


@router.get("/entries/{entry_id}/history")
async def entry_history(
    entry_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    try:
        out = await get_memory_service(db).history(entry_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="No such entry")
    _entry_domain(out["domain"], user)
    return out


@router.get("/entries/{entry_id}/versions/{version}")
async def entry_version(
    entry_id: str,
    version: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    try:
        entry = await get_memory_service(db).get_version(entry_id, version)
    except NotFound:
        raise HTTPException(status_code=404, detail="No such version")
    _entry_domain(entry.front.domain, user, missing="No such version")
    return {"version": version, "frontmatter": entry.front.to_dict(), "body": entry.body}


@router.post("/entries/{entry_id}/review")
async def review_entry(
    entry_id: str,
    body: ReviewBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    service = get_memory_service(db)
    try:
        domain = _entry_domain((await service.get(entry_id)).front.domain, user)
        _writable(domain, user)
        return await service.review(
            entry_id, body.action, human_author(user["uid"]), body.why, body.review_by
        )
    except NotFound:
        raise HTTPException(status_code=404, detail="No such entry")
    except OwnEntry as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{domain}/entries", status_code=201)
async def write_entry(
    domain: str,
    body: WriteBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    req = WriteRequest(domain=_writable(domain, user), **body.model_dump())
    service = get_memory_service(db)
    gone = HTTPException(status_code=404, detail="The superseded entry does not exist")
    if req.supersedes:
        # One answer for missing and elsewhere, so this doesn't say an id
        # exists in a domain the caller can't read.
        try:
            if (await service.get(req.supersedes)).front.domain != domain:
                raise gone
        except NotFound:
            raise gone
    try:
        entry = await service.write(req, author=human_author(user["uid"]))
    except NotFound:
        raise gone
    except EntryError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"frontmatter": entry.front.to_dict()}


@router.post("/{domain}/recall")
async def recall(
    domain: str,
    body: RecallBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    user_service=Depends(get_user_service_dep),
) -> Dict[str, Any]:
    return await get_memory_service(db).recall(
        body.question, _domain(domain, user), user_service=user_service, user_id=user["uid"]
    )
