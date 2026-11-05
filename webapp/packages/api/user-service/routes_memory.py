"""Memory pilot endpoints: search, get, recall, write.

Mounted only when MEMORY_ENABLED is true. Reads need a session; writes need a
site admin or a uid in MEMORY_WRITERS until workspace scoping (ISSUE-002)
gives domains real owners. Only domains in MEMORY_DOMAINS exist.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dependencies import get_db, get_user_service_dep
from routes import get_current_user
from services.memory.entry import Confidence, EntryError, EntryType
from services.memory.service import (
    NotFound,
    WriteRequest,
    get_memory_service,
    human_author,
)

router = APIRouter(prefix="/memory", tags=["memory"])


def _csv(name: str, default: str = "") -> List[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


def _domain(domain: str) -> str:
    if domain not in _csv("MEMORY_DOMAINS", "dom-scan"):
        raise HTTPException(status_code=404, detail=f"No memory domain {domain!r}")
    return domain


def _require_writer(user: dict) -> None:
    if user.get("is_site_admin") or user.get("uid") in _csv("MEMORY_WRITERS"):
        return
    raise HTTPException(status_code=403, detail="Not allowed to write memory")


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


@router.get("/{domain}/search")
async def search(
    domain: str,
    q: Optional[str] = None,
    type: Optional[EntryType] = None,
    trust_tier: Optional[str] = None,
    due_by: Optional[date] = None,
    limit: int = Query(20, ge=1, le=200),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    filters: Dict[str, Any] = {}
    if type:
        filters["type"] = type.value
    if trust_tier:
        filters["trust_tier"] = trust_tier
    hits = await get_memory_service(db).search(_domain(domain), q, filters, due_by, limit)
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
    _domain(entry.front.domain)
    return {"frontmatter": entry.front.to_dict(), "body": entry.body}


@router.post("/{domain}/entries", status_code=201)
async def write_entry(
    domain: str,
    body: WriteBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    _require_writer(user)
    req = WriteRequest(domain=_domain(domain), **body.model_dump())
    try:
        entry = await get_memory_service(db).write(req, author=human_author(user["uid"]))
    except NotFound:
        raise HTTPException(status_code=404, detail="The superseded entry does not exist")
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
        body.question, _domain(domain), user_service=user_service, user_id=user["uid"]
    )
