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

import asyncio
from datetime import date
from typing import Annotated, Any, Dict, List, Optional

from typing import Literal

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
from services.memory.triage import MAX_GUIDANCE_CHARS, Kind, Triage

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
    body: str = Field(..., min_length=1, max_length=MAX_GUIDANCE_CHARS)
    summary: str = Field(..., min_length=1, max_length=300)
    title: Optional[str] = Field(None, max_length=200)
    source: List[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=50)
    confidence: Optional[Confidence] = None
    review_by: Optional[date] = None
    supersedes: Optional[str] = None
    origin: Optional[str] = Field(None, max_length=500)


class RecallBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


class ReviewBody(BaseModel):
    action: ReviewAction
    why: str = Field("", max_length=2000)
    review_by: Optional[date] = None


class BatchReviewBody(ReviewBody):
    ids: List[str] = Field(..., min_length=1, max_length=1000)
    restore_superseded: bool = False


class GuidanceBody(BaseModel):
    text: str = Field("", max_length=MAX_GUIDANCE_CHARS)
    summary: Optional[str] = Field(None, max_length=300)
    title: Optional[str] = Field(None, max_length=200)
    scope: Literal["all", "asvs", "glasswing"] = "all"
    always: bool = False
    # Only when the finding's origin doesn't say: owner/repo.
    repo: Optional[str] = Field(None, max_length=200)


class DispositionBody(BaseModel):
    kind: Kind
    why: str = Field("", max_length=2000)
    duplicate_of: Optional[str] = Field(None, max_length=500)
    filed_as: Optional[str] = Field(None, max_length=500)
    guidance: Optional[GuidanceBody] = None


class BatchDispositionBody(DispositionBody):
    ids: List[str] = Field(..., min_length=1, max_length=1000)


class ProposalEdit(BaseModel):
    text: Optional[str] = Field(None, max_length=MAX_GUIDANCE_CHARS)
    summary: Optional[str] = Field(None, max_length=300)
    title: Optional[str] = Field(None, max_length=200)
    scope: Literal["all", "asvs", "glasswing"] = "all"
    always: bool = False


class AcceptProposalsBody(BaseModel):
    ids: List[str] = Field(..., min_length=1, max_length=1000)
    why: str = Field("", max_length=2000)
    edits: Dict[str, ProposalEdit] = Field(default_factory=dict)


class RejectProposalsBody(BaseModel):
    ids: List[str] = Field(..., min_length=1, max_length=1000)
    why: str = Field(..., min_length=1, max_length=2000)


class AcceptDigestBody(BaseModel):
    why: str = Field("", max_length=2000)


def _gates(user: dict):
    """may_read and may_review for a caller, as batch review has them."""
    return (lambda d: access.exists(d) and access.can_read(user, d),
            lambda d: access.can_write(user, d))


def _single(out: Dict[str, Any], key: str = "applied") -> Dict[str, Any]:
    """One entry's result, or the error its skip reason means."""
    if out.get(key):
        return out
    reason = (out.get("skipped") or [{}])[0].get("reason") or "Not applied"
    code = 404 if reason == "No such entry" else 403 if "someone else" in reason else 409
    raise HTTPException(status_code=code, detail=reason)


@router.get("/status")
def status(user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Memory is on: these routes are mounted only when it is. Pages ask
    this, not /domains, to decide whether to show memory: it counts
    nothing."""
    return {"enabled": True}


async def _readable_domains(user: dict, service) -> List[str]:
    """Every domain the caller may read: the shared ones, their projects',
    and for a site admin every project domain that has entries."""
    names = [d for d in access.shared_domains() if access.can_read(user, d)]
    projects = set(member_projects(user))
    if user.get("is_site_admin"):
        projects |= {d for d in await service.domains() if access.project_of(d)}
    names += sorted(p for p in projects if access.project_of(p))
    return [n for n in names if access.exists(n) and access.can_read(user, n)]


ALL_DOMAINS = "*"


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
        # A site admin's list is every domain with entries: one read of the
        # index finds them and counts them all.
        counted = await service.counts_by_domain()
        projects |= {d for d in counted if access.project_of(d)}
    names += sorted(p for p in projects if access.project_of(p))
    if not user.get("is_site_admin"):
        # A domain at a time, all at once rather than in turn.
        counted = dict(zip(names, await asyncio.gather(*(service.counts(n) for n in names))))
    none = {"entries": 0, "raw": 0, "due": 0, "review": 0}
    out = []
    for name in names:
        can = access.can_write(user, name)
        out.append({
            "domain": name,
            "kind": "shared" if access.is_shared(name) else "project",
            "canWrite": can,
            "canReview": can,
            **counted.get(name, none),
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
    limit: int = Query(20, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    run: Optional[str] = Query(None, max_length=64),
    queue: bool = False,
    origin: Optional[List[str]] = Query(None, max_length=50),
    disposition: Optional[str] = Query(None, pattern="^(none|any|valid|duplicate|fp_wrong|fp_context|by_design)$"),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """include_inactive adds demoted and superseded entries; order=oldest is the review queue's;
    run limits it to the entries one run wrote; queue keeps only what's waiting for review
    (raw, or past its review date). origin (repeatable) keeps entries from any of those
    origins; disposition is a kind, any (decided) or none (not decided).
    domain "*" searches every domain the caller may read. total is how many
    match; offset and limit pick the page."""
    filters: Dict[str, Any] = {}
    if run:
        filters["run"] = run
    if type:
        filters["type"] = type.value
    if trust_tier:
        filters["trust_tier"] = trust_tier
    service = get_memory_service(db)
    targets = await _readable_domains(user, service) if domain == ALL_DOMAINS else [_domain(domain, user)]
    page = await service.search_page(
        targets, q, filters, due_by, limit, offset,
        include_inactive=include_inactive, oldest_first=order == "oldest", review_queue=queue,
        origins=origin, disposition=disposition,
    )
    return {"domain": domain, "query": q, "offset": offset, **page}


def run_summary(run_id: str, user: dict, admin_sees_all: bool = False) -> Dict[str, Any]:
    """What the caller may know about the run that wrote some entries: the
    run's summary if they can see the run, else its id alone. Reading a
    domain doesn't grant the run: its inputs are the launcher's.
    ``admin_sees_all``: a site admin sees every run, not only project ones."""
    from auth.project_access import can_see_run
    from services.run_registry import get_run_registry

    try:
        rec = get_run_registry().get(run_id)
    except Exception:
        rec = None
    if rec is None or not (can_see_run(user, rec.user_id, rec.project)
                           or (admin_sees_all and user.get("is_site_admin"))):
        return {"runId": run_id, "visible": False}
    s = rec.to_summary()
    s.pop("inputDict", None)
    return {**s, "visible": True}


@router.post("/reviews/batch")
async def batch_review(
    body: BatchReviewBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """One decision over many entries (B11). Each entry gets its own review
    record with the batch id; entries that can't take it are skipped and
    listed: the caller wrote them, they aren't live, the tier is wrong, or
    the caller may not review their domain (reported as missing)."""
    try:
        return await get_memory_service(db).batch_review(
            body.ids, body.action, human_author(user["uid"]), body.why, body.review_by,
            restore_superseded=body.restore_superseded,
            may_read=lambda d: access.exists(d) and access.can_read(user, d),
            may_review=lambda d: access.can_write(user, d),
        )
    except Refused as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/reviews/batch/{batch_id}/undo")
async def undo_batch(
    batch_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """Put back what a batch changed, entry by entry, where nothing has
    changed it since. Needs the right to review each entry's domain."""
    try:
        return await get_memory_service(db).undo_batch(
            batch_id, human_author(user["uid"]),
            may_review=lambda d: access.exists(d) and access.can_read(user, d) and access.can_write(user, d),
        )
    except NotFound:
        raise HTTPException(status_code=404, detail="No such batch")


@router.get("/runs")
async def all_runs(
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """Runs with entries in the domains the caller reads, newest first,
    with each domain's counts for triage. A site admin sees every run; anyone
    else the runs they started and their projects' runs."""
    service = get_memory_service(db)
    admin = bool(user.get("is_site_admin"))
    out = []
    for r in await service.run_overview(await _readable_domains(user, service)):
        info = run_summary(r["run"], user, admin_sees_all=True)
        if info["visible"] or admin:
            out.append({**r, "info": info})
        if len(out) >= limit:
            break
    return {"runs": out}


@router.get("/by-run/{run_id}")
async def run_memories(
    run_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """What one run wrote, tallied per domain, in the domains the caller
    may read. The run page's memories panel."""
    tallies = await get_memory_service(db).for_run(run_id)
    domains = [
        {"domain": d, "kind": "shared" if access.is_shared(d) else "project",
         "canReview": access.can_write(user, d), **t}
        for d, t in sorted(tallies.items())
        if access.exists(d) and access.can_read(user, d)
    ]
    return {"run": run_summary(run_id, user), "domains": domains}


@router.get("/{domain}/runs")
async def domain_runs(
    domain: str,
    limit: int = Query(20, ge=1, le=200),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """Runs that wrote into the domain, newest first, with their entries
    counted by type and tier."""
    runs = await get_memory_service(db).runs(_domain(domain, user))
    return {"domain": domain, "runs": [{**r, "info": run_summary(r["run"], user)} for r in runs[:limit]]}


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


# ----------------------------------------------------------------------
# Triage (scans that remember): dispositions, proposals, guidance, digest
# ----------------------------------------------------------------------


def _triage(db) -> Triage:
    return Triage(get_memory_service(db))


async def _dispose(db, user, ids, body: DispositionBody) -> Dict[str, Any]:
    may_read, may_review = _gates(user)
    try:
        return await _triage(db).dispose(
            ids, body.kind, human_author(user["uid"]), body.why,
            duplicate_of=body.duplicate_of, filed_as=body.filed_as,
            guidance=body.guidance.model_dump() if body.guidance else None,
            may_read=may_read, may_review=may_review)
    except Refused as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/entries/{entry_id}/disposition")
async def dispose_entry(
    entry_id: str,
    body: DispositionBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """A person's decision on one finding: the trust action it implies, the
    reason, and for "needs context" and "by design" a guidance entry. Undo
    with the batch id it returns."""
    return _single(await _dispose(db, user, [entry_id], body))


@router.post("/dispositions/batch")
async def dispose_batch(
    body: BatchDispositionBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """One disposition and one reason for many findings. Guidance written
    with it is one entry per repo, citing every finding it covers."""
    return await _dispose(db, user, body.ids, body)


@router.post("/proposals/accept")
async def accept_proposals(
    body: AcceptProposalsBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """Each proposal becomes guidance written by the caller, optionally as
    edited, and is superseded by it."""
    may_read, may_review = _gates(user)
    return await _triage(db).accept_proposals(
        body.ids, human_author(user["uid"]), body.why,
        edits={k: v.model_dump() for k, v in body.edits.items()},
        may_read=may_read, may_review=may_review)


@router.post("/proposals/reject")
async def reject_proposals(
    body: RejectProposalsBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    may_read, may_review = _gates(user)
    try:
        return await _triage(db).reject_proposals(
            body.ids, human_author(user["uid"]), body.why,
            may_read=may_read, may_review=may_review)
    except Refused as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{domain}/guidance-repos")
async def guidance_repos(
    domain: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """Repos with guidance or proposals in the domain, for the Browse filter."""
    return {"domain": domain, "repos": await _triage(db).guidance_repos(_domain(domain, user))}


@router.get("/{domain}/guidance")
async def guidance(
    domain: str,
    repo: str = Query(..., pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"),
    scanner: Optional[str] = Query(None, pattern="^(asvs|glasswing)$"),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """What every scan of a repo is given from this domain, with how many
    runs loaded and cited each entry, and the proposals still waiting."""
    t = _triage(db)
    d = _domain(domain, user)
    out = await t.guidance_for([d], repo, scanner)
    props = await get_memory_service(db).search(
        d, None, None, None, 1000, origins=[f"proposal:guidance:{repo}"])
    ids = [e["id"] for e in out["entries"]] + ([out["digest"]["id"]] if out["digest"] else [])
    out["usage"] = await t.usage(ids)
    out["proposals"] = props
    return out


@router.get("/{domain}/digest")
async def digest(
    domain: str,
    repo: str = Query(..., pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """The accepted digest, the newest draft, what it changes, and how many
    decisions are newer than the accepted version."""
    return await _triage(db).digest_view(_domain(domain, user), repo)


@router.post("/digests/{entry_id}/accept")
async def accept_digest(
    entry_id: str,
    body: AcceptDigestBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    may_read, may_review = _gates(user)
    try:
        return await _triage(db).accept_digest(entry_id, human_author(user["uid"]), body.why,
                                               may_read=may_read, may_review=may_review)
    except NotFound:
        raise HTTPException(status_code=404, detail="No such entry")
    except OwnEntry as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc))
