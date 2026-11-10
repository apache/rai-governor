"""Pipeline import and export (ISSUE-011). See services/pipeline_io.py.

  POST /pipelines/import/github   {repo, ref, path, token?, label?, dryRun}
  POST /pipelines/import/upload   multipart: files (a picked folder), label?, dryRun
  GET  /agents/{agent_id}/export  zip of the agent and every agent it calls

Imports run as a dry run first: the response is the plan (create, update or
unchanged per agent, with what changed) and any problem that would stop it.
A GitHub token is used for that request only and never stored.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from dependencies import get_db, get_logger
from routes import get_author_user
from services import pipeline_io as pio
from services.database_service import DatabaseService
from services.observability_service import ObservabilityService

router = APIRouter(tags=["pipelines"])


class GithubImport(BaseModel):
    repo: str = Field(..., description="owner/name")
    ref: str = "main"
    path: str = ""
    token: Optional[str] = None
    label: Optional[str] = None
    dry_run: bool = Field(True, alias="dryRun")

    model_config = {"populate_by_name": True}


def _all_agents(db: DatabaseService) -> List[dict]:
    return db.list_all("agents")


def _run_import(files, label, source, dry_run, db, user, logger):
    try:
        manifest = pio.load_manifest(files)
        label = pio.check_label(label)
        existing = _all_agents(db)
        if dry_run:
            return pio.plan_import(manifest, label, existing)

        def save(doc):
            saved = db.save("agents", doc["_id"], doc)
            return saved.get("rev")

        result = pio.apply_import(manifest, label, source, existing, save,
                                  lambda: str(uuid.uuid4()))
    except pio.PipelineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.log(
        "user_action",
        f"Pipeline {result['pipeline']}@{result['label']} imported "
        f"({len(result['written'])} agents).",
        user_id=user.get("uid"),
        metadata={"source": source, "agents": list(result["written"])},
    )
    return result


@router.post("/pipelines/import/github")
async def import_from_github(
    body: GithubImport,
    db: DatabaseService = Depends(get_db),
    user: dict = Depends(get_author_user),
    logger: ObservabilityService = Depends(get_logger),
):
    try:
        files, sha = await pio.files_from_github(body.repo, body.ref, body.path, body.token)
    except pio.PipelineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    source = {"kind": "github", "repo": body.repo, "ref": body.ref,
              "path": body.path, "commit": sha}
    label = body.label or pio.default_label(commit_sha=sha)
    result = _run_import(files, label, source, body.dry_run, db, user, logger)
    result["source"] = source
    return result


@router.post("/pipelines/import/upload")
async def import_from_upload(
    files: List[UploadFile] = File(...),
    label: Optional[str] = Form(None),
    dry_run: bool = Form(True, alias="dryRun"),
    db: DatabaseService = Depends(get_db),
    user: dict = Depends(get_author_user),
    logger: ObservabilityService = Depends(get_logger),
):
    entries = []
    for f in files:
        entries.append((f.filename or "", await f.read()))
    try:
        file_map = pio.files_from_upload(entries)
    except pio.PipelineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    label = label or pio.default_label()
    source = {"kind": "upload", "files": len(file_map)}
    result = _run_import(file_map, label, source, dry_run, db, user, logger)
    result["source"] = source
    return result


@router.get("/agents/{agent_id}/export")
async def export_pipeline(
    agent_id: str,
    db: DatabaseService = Depends(get_db),
    user: dict = Depends(get_author_user),
):
    def get_agent(aid):
        try:
            return db.get("agents", aid)
        except HTTPException as e:
            if e.status_code == 404:
                return None
            raise

    try:
        docs = pio.collect_pipeline(agent_id, get_agent)
        pipeline, files = pio.build_export(docs)
    except pio.PipelineError as e:
        raise HTTPException(status_code=404 if "not found" in str(e) else 400, detail=str(e))
    data = pio.zip_files(files, pipeline)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{pipeline}.zip"'},
    )
