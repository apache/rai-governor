"""Workloads (B11): deployed agents a site admin publishes for projects.

Two steps. A site admin publishes a deployed agent to the catalog, saying
which projects may use it, which inputs committers set and which the admin
fixes. A project's PMC then adds it to the project's Workloads page, with
the project's own defaults if it likes. Members see and run what their
project added; the run, its data-store records and its memory entries are
the project's (ISSUE-002). Running needs no right to run agent code: the
code is the admin's, the member sets only the listed inputs. Site admins
alone see fixed inputs.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.project_access import check_project, is_admin, member_projects
from dependencies import get_db, get_logger
from models.workload import RunWorkloadBody, WorkloadBody
from routes import get_current_user, start_deployment_run
from services import workload_service as ws

router = APIRouter(prefix="/workloads", tags=["workloads"])


def _site_admin(user: dict = Depends(get_current_user)) -> dict:
    if not user.get("is_site_admin"):
        raise HTTPException(status_code=403, detail="Publishing workloads is for site administrators")
    return user


class AddBody(BaseModel):
    project: str = Field(..., min_length=1, max_length=64)
    defaults: Dict[str, Any] = Field(default_factory=dict)


def _published(db, workload_id: str, project: str):
    w = ws.get_workload(db, workload_id)
    if not ws.published_for(w, project):
        # Same answer as a missing one: a member learns nothing about
        # workloads that aren't theirs.
        raise HTTPException(status_code=404, detail="No such workload")
    return w


def _on_page(db, workload_id: str, project: str):
    """A workload the project has added, with the project's entry."""
    w = _published(db, workload_id, project)
    entry = ws.added_one(db, project, w.id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such workload on this project's page")
    return w, entry


def _project(user: dict, project: str) -> str:
    """A project the caller is a member of. Unlike check_project, a blank
    name is refused: these routes are always for one project."""
    project = check_project(user, project)
    if not project:
        raise HTTPException(status_code=400, detail="Which project?")
    return project


def _pmc(user: dict, project: str) -> str:
    project = _project(user, project)
    if not is_admin(user, project):
        raise HTTPException(status_code=403, detail=f"Adding and removing workloads is for {project}'s PMC")
    return project


@router.get("")
async def list_for_project(
    project: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """The workloads on a project's page, for a member: the ones its PMC
    added, with the project's defaults. ``available`` is the rest of the
    catalog published to the project, which the PMC can add."""
    project = _project(user, project)
    published = {w.id: w for w in ws.list_workloads(db) if ws.published_for(w, project)}
    page = []
    for entry in ws.added(db, project):
        w = published.get(entry["workload_id"])
        if w is not None:
            # Only defaults that still hold: an admin may have changed the
            # inputs, choices or repositories since the PMC set them.
            defaults = ws.live_defaults(w, project, entry.get("defaults"))
            page.append({**ws.public_view(w, project, defaults),
                         "addedBy": entry.get("added_by"), "addedAt": entry.get("added_at"),
                         "projectDefaults": defaults})
    on_page = {x["id"] for x in page}
    manage = is_admin(user, project)
    return {
        "project": project,
        "role": "admin" if manage else "member",
        "canManage": manage,
        "workloads": page,
        # What the PMC could add; members don't need the rest of the catalog.
        "available": [ws.catalog_entry(w, project) for wid, w in sorted(published.items(), key=lambda kv: kv[1].name.lower())
                      if wid not in on_page] if manage else [],
    }


# The /admin routes come before /{workload_id}/...: "PUT /admin/add" must
# reach change(), not set_page_defaults() for a workload called "admin".
@router.get("/admin")
async def list_all(user: dict = Depends(_site_admin), db=Depends(get_db)) -> Dict[str, Any]:
    on_pages = ws.adopters(db)
    return {"workloads": [{**ws.admin_view(w), "addedTo": on_pages.get(w.id, [])} for w in ws.list_workloads(db)]}


@router.post("/admin", status_code=201)
async def publish(body: WorkloadBody, user: dict = Depends(_site_admin), db=Depends(get_db)):
    _check_deployment(db, body.deployment)
    return ws.admin_view(ws.create_workload(db, body, user["uid"]))


@router.put("/admin/{workload_id}")
async def change(workload_id: str, body: WorkloadBody, user: dict = Depends(_site_admin), db=Depends(get_db)):
    _check_deployment(db, body.deployment)
    return ws.admin_view(ws.update_workload(db, workload_id, body))


@router.delete("/admin/{workload_id}", status_code=204)
async def unpublish(workload_id: str, user: dict = Depends(_site_admin), db=Depends(get_db)):
    ws.delete_workload(db, workload_id)


def _check_deployment(db, name: str) -> None:
    try:
        db.get("deployments", name)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(status_code=400, detail=f"No deployment named {name!r}")
        raise


@router.post("/{workload_id}/add", status_code=201)
async def add_to_page(
    workload_id: str,
    body: AddBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """Add a published workload to the project's page (its PMC, or a site
    admin), with the project's defaults for its inputs."""
    project = _pmc(user, body.project)
    w = _published(db, workload_id, project)
    return ws.add_to_project(db, w, project, user["uid"], body.defaults)


@router.put("/{workload_id}/add")
async def set_page_defaults(
    workload_id: str,
    body: AddBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    project = _pmc(user, body.project)
    w = _published(db, workload_id, project)
    return ws.set_project_defaults(db, w, project, body.defaults)


@router.delete("/{workload_id}/add", status_code=204)
async def remove_from_page(
    workload_id: str,
    project: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Take it off the project's page. Its runs, data and memory stay."""
    project = _pmc(user, project)
    ws.remove_from_project(db, project, workload_id)


@router.get("/projects")
async def my_projects(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """The projects the caller can pick on the Workloads page, with their
    role. Site admins also get every project a workload names."""
    names = set(member_projects(user))
    if user.get("is_site_admin"):
        for w in ws.list_workloads(db):
            names |= {p for p in w.projects if p != "*"}
        names |= {p for ps in ws.adopters(db).values() for p in ps}
    return {"projects": [{"project": p, "role": "admin" if is_admin(user, p) else "member"}
                         for p in sorted(names)],
            # Whether to offer the catalog and opening any project.
            "isSiteAdmin": bool(user.get("is_site_admin"))}


@router.get("/{workload_id}")
async def get_one(
    workload_id: str,
    project: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    project = _project(user, project)
    w, entry = _on_page(db, workload_id, project)
    return ws.public_view(w, project, entry.get("defaults"))


@router.post("/{workload_id}/run", status_code=202)
async def run_workload(
    workload_id: str,
    body: RunWorkloadBody,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    logger=Depends(get_logger),
) -> Dict[str, Any]:
    """Start the workload for a project the caller belongs to. The record
    keeps the caller's inputs and only the names of the fixed ones."""
    project = _project(user, body.project)
    w, entry = _on_page(db, workload_id, project)
    run_inputs, stored = ws.resolve_inputs(w, project, body.inputs, entry.get("defaults"))
    out = start_deployment_run(
        w.deployment, run_inputs, user, db, logger,
        project=project, workload_id=w.id, stored_inputs=stored,
        secrets=ws.secret_values(w),
    )
    return {"runId": out["runId"], "status": out["status"], "workloadId": w.id, "project": project}
