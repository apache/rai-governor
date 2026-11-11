"""Workloads (B11): deployed agents a site admin publishes for projects.

Project members list the workloads published to their project and launch
them for it; the run, its data-store records and its memory entries are the
project's (ISSUE-002). Launching needs no right to run agent code: the code
is the admin's, the committer sets only the listed inputs. Site admins
publish, change and unpublish workloads, and alone see their fixed inputs.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

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


def _published(db, workload_id: str, project: str):
    w = ws.get_workload(db, workload_id)
    if not ws.published_for(w, project):
        # Same answer as a missing one: a member learns nothing about
        # workloads that aren't theirs.
        raise HTTPException(status_code=404, detail="No such workload")
    return w


@router.get("")
async def list_for_project(
    project: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    """The workloads published to a project the caller belongs to."""
    project = check_project(user, project)
    return {
        "project": project,
        "role": "admin" if is_admin(user, project) else "member",
        "workloads": [ws.public_view(w, project) for w in ws.list_workloads(db) if ws.published_for(w, project)],
    }


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
    return {"projects": [{"project": p, "role": "admin" if is_admin(user, p) else "member"}
                         for p in sorted(names)]}


@router.get("/admin")
async def list_all(user: dict = Depends(_site_admin), db=Depends(get_db)) -> Dict[str, Any]:
    return {"workloads": [ws.admin_view(w) for w in ws.list_workloads(db)]}


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


@router.get("/{workload_id}")
async def get_one(
    workload_id: str,
    project: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Dict[str, Any]:
    project = check_project(user, project)
    return ws.public_view(_published(db, workload_id, project), project)


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
    project = check_project(user, body.project)
    w = _published(db, workload_id, project)
    run_inputs, stored = ws.resolve_inputs(w, project, body.inputs)
    out = start_deployment_run(
        w.deployment, run_inputs, user, db, logger,
        project=project, workload_id=w.id, stored_inputs=stored,
        secrets=ws.secret_values(w),
    )
    return {"runId": out["runId"], "status": out["status"], "workloadId": w.id, "project": project}
