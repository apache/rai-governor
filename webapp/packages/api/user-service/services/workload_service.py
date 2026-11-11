"""Published workloads (B11): storage, who sees them, and the inputs a run gets.

Kept in the ``workloads`` collection. Committers see a workload only for
projects it is published to, and never its fixed inputs; site admins see
and change everything.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from models.workload import Workload, WorkloadBody

WORKLOADS_DB = "workloads"

# What a run record keeps in place of an admin-set input's value.
FIXED_PLACEHOLDER = "(set by a site admin)"

MAX_TEXT_INPUT = 500

# A text input's value without an admin pattern: something an agent can put
# in a URL path or a config line without changing what it addresses. Run
# inputs reach agents that build GitHub URLs and line-based configs from
# them, with the admin's tokens: "../../other/tarball/main" or a newline and
# "repo: apache/other" would aim those tokens elsewhere.
TEXT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/+@:=-]{0,199}\Z")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")

# Path segments that name the API's own routes, not a workload.
RESERVED_IDS = {"admin", "projects"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "workload"
    return s if s[0].isalnum() else "w" + s


def list_workloads(db) -> List[Workload]:
    out = []
    for doc in db.list_all(WORKLOADS_DB):
        try:
            out.append(Workload(**_strip(doc)))
        except Exception:
            continue
    out.sort(key=lambda w: w.name.lower())
    return out


def get_workload(db, workload_id: str) -> Workload:
    try:
        return Workload(**_strip(db.get(WORKLOADS_DB, workload_id)))
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(status_code=404, detail="No such workload")
        raise


def _strip(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _dump(w: Workload) -> Dict[str, Any]:
    return w.model_dump(mode="json", by_alias=True)


def create_workload(db, body: WorkloadBody, uid: str) -> Workload:
    base = _slug(body.name)
    if base in RESERVED_IDS:
        base = f"w-{base}"
    existing = {w.id for w in list_workloads(db)}
    wid = base if base not in existing else f"{base}-{secrets.token_hex(2)}"
    now = _now()
    w = Workload(**body.model_dump(by_alias=True), id=wid, publishedBy=uid, publishedAt=now, updatedAt=now)
    db.save(WORKLOADS_DB, wid, _dump(w))
    return w


def update_workload(db, workload_id: str, body: WorkloadBody) -> Workload:
    old = get_workload(db, workload_id)
    w = Workload(**body.model_dump(by_alias=True), id=old.id, publishedBy=old.published_by,
                 publishedAt=old.published_at, updatedAt=_now())
    doc = _dump(w)
    try:
        doc["_rev"] = db.get(WORKLOADS_DB, workload_id).get("_rev")
    except Exception:
        pass
    db.save(WORKLOADS_DB, workload_id, doc)
    return w


def delete_workload(db, workload_id: str) -> None:
    get_workload(db, workload_id)
    db.delete(WORKLOADS_DB, workload_id)


def published_for(w: Workload, project: str) -> bool:
    return w.enabled and ("*" in w.projects or project in w.projects)


def repository_rule(project: str) -> re.Pattern:
    """A project's repositories by ASF naming: apache/<project> and
    apache/<project>-<anything>."""
    return re.compile(rf"\Aapache/{re.escape(project)}(-[A-Za-z0-9._-]+)?\Z")


def public_view(w: Workload, project: str) -> Dict[str, Any]:
    """What a project member sees: no deployment, no fixed values."""
    repos = list(w.repositories.get(project) or [])
    inputs = []
    for i in w.inputs:
        d = i.model_dump()
        if i.kind == "repository":
            d["choices"] = repos
            d["pattern"] = repository_rule(project).pattern[2:-2]
            if d.get("default") and repos and d["default"] not in repos:
                d["default"] = repos[0]
            if not d.get("default") and repos:
                d["default"] = repos[0]
        inputs.append(d)
    return {
        "id": w.id,
        "name": w.name,
        "description": w.description,
        "inputs": inputs,
        "fixedInputNames": sorted(w.fixed_inputs),
        "produces": w.produces,
        "memoryNote": w.memory_domain_note,
        "typicalDuration": w.typical_duration,
    }


SECRET_NAME_RE = re.compile(r"token|secret|password|passwd|key|pat\b|credential|auth", re.I)


def secret_values(w: Workload) -> List[str]:
    """The fixed inputs to keep out of everything a run shows its project
    (result, error, ops log, trace): those named as secrets. The rest (an
    output repository, a directory) stay readable, so members can find what
    the run published; all of them are names only in the run's inputs."""
    return [v for k, v in w.fixed_inputs.items()
            if SECRET_NAME_RE.search(k) and isinstance(v, str) and len(v) >= 6]


def admin_view(w: Workload) -> Dict[str, Any]:
    return _dump(w)


def resolve_inputs(w: Workload, project: str, given: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The run's input dict, and what its record keeps.

    Refuses inputs the workload doesn't list (so a committer can't set or
    override a fixed one), fills defaults, checks choices and repositories.
    """
    names = {i.name for i in w.inputs}
    extra = sorted(set(given) - names)
    if extra:
        raise HTTPException(status_code=400, detail=f"Not inputs of this workload: {', '.join(extra)}")
    out: Dict[str, Any] = {}
    for i in w.inputs:
        v = given.get(i.name)
        if v is None or (isinstance(v, str) and not v.strip()):
            v = i.default
            if i.kind == "repository" and not v and w.repositories.get(project):
                v = w.repositories[project][0]
        if v is None or (isinstance(v, str) and not v.strip()):
            if i.required:
                raise HTTPException(status_code=400, detail=f"{i.label} is required")
            continue
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail=f"{i.label} must be text")
        v = v.strip()
        if len(v) > MAX_TEXT_INPUT:
            raise HTTPException(status_code=400, detail=f"{i.label} is too long")
        if CONTROL_RE.search(v):
            raise HTTPException(status_code=400, detail=f"{i.label} has a control character")
        if i.kind == "choice":
            if v not in i.choices:
                raise HTTPException(status_code=400, detail=f"{i.label} must be one of {', '.join(i.choices)}")
        elif i.kind == "repository":
            listed = w.repositories.get(project)
            # An admin's list is the whole rule; without one, the naming rule.
            ok = (v in listed) if listed else bool(repository_rule(project).match(v))
            if not ok:
                raise HTTPException(status_code=403, detail=f"{v} is not one of {project}'s repositories")
        elif i.pattern:
            if not re.fullmatch(i.pattern, v):
                raise HTTPException(status_code=400, detail=f"{i.label} is not in the form this workload takes")
        elif not TEXT_RE.match(v) or ".." in v or "//" in v or v.endswith("/"):
            raise HTTPException(status_code=400,
                                detail=f"{i.label} may hold letters, digits and . _ / + @ : = - only, without .. or //")
        out[i.name] = v
    run_inputs = {**out, **w.fixed_inputs}
    # (The run scrubs the fixed values from what it shows: secret_values.)
    stored = {**out, **{k: FIXED_PLACEHOLDER for k in w.fixed_inputs}}
    return run_inputs, stored
