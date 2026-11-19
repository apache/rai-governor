"""Pipeline import and export (ISSUE-011). See services/pipeline_io.py.

  POST /pipelines/import/github   {repo, ref, path, token?, label?, labeled, dryRun, deploy}
  POST /pipelines/import/upload   multipart: files (a picked folder), label?, labeled, dryRun, deploy
  GET  /agents/{agent_id}/export  zip of the agent and every agent it calls

Imports run as a dry run first: the response is the plan (create, update or
unchanged per agent, with what changed) and any problem that would stop it.
A GitHub token is used for that request only and never stored.

labeled (default true) imports into a copy under the label: the given one,
else the commit hash or a timestamp. labeled false imports into the stable
copy, which has none: its agents keep their own names and deploy at
/rest/<name>.

An import also deploys every agent it wrote (deploy, default true), each at
/rest/{its friendly name}, which carries the label. A deployment that can't
be made is reported in deployProblems; the import itself still stands.
"""

from __future__ import annotations

import uuid
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from dependencies import deploy_agent, get_db, get_logger
from routes import get_author_user
from services import pipeline_io as pio
from services.database_service import DatabaseService
from services.observability_service import ObservabilityService
from services.rate_limit import per_user

router = APIRouter(tags=["pipelines"])


class GithubImport(BaseModel):
    repo: str = Field(..., description="owner/name", max_length=256)
    ref: str = Field("main", max_length=256)
    path: str = Field("", max_length=1024)
    token: Optional[str] = Field(None, max_length=4096)
    label: Optional[str] = Field(None, max_length=128)
    labeled: bool = True
    dry_run: bool = Field(True, alias="dryRun")
    deploy: bool = True

    model_config = {"populate_by_name": True}


def _all_agents(db: DatabaseService) -> List[dict]:
    return db.list_all("agents")


async def _deploy_all(result, db):
    """Deploy every agent the import wrote. One that can't be deployed (its
    friendly name taken by another agent, say) is reported, not raised."""
    deployed, problems = [], []
    for name, w in result["written"].items():
        try:
            out = await deploy_agent(w["id"], db)
            deployed.append({"agent": name, "endpoint": out.get("endpoint")})
        except HTTPException as e:
            problems.append({"agent": name, "reason": str(e.detail)})
    result["deployed"] = deployed
    result["deployProblems"] = problems
    return result


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
        f"Pipeline {result['pipeline']}@{result['label'] or 'stable'} imported "
        f"({len(result['written'])} agents).",
        user_id=user.get("uid"),
        metadata={"source": source, "agents": list(result["written"])},
    )
    return result


@router.post("/pipelines/import/github", dependencies=[per_user("pipeline_import", get_author_user)])
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
    label = (body.label or pio.default_label(commit_sha=sha)) if body.labeled else pio.STABLE
    result = _run_import(files, label, source, body.dry_run, db, user, logger)
    if not body.dry_run and body.deploy:
        result = await _deploy_all(result, db)
    result["source"] = source
    return result


@router.post("/pipelines/import/upload", dependencies=[per_user("pipeline_import", get_author_user)])
async def import_from_upload(
    files: List[UploadFile] = File(...),
    label: Optional[str] = Form(None),
    labeled: bool = Form(True),
    dry_run: bool = Form(True, alias="dryRun"),
    deploy: bool = Form(True),
    db: DatabaseService = Depends(get_db),
    user: dict = Depends(get_author_user),
    logger: ObservabilityService = Depends(get_logger),
):
    if len(files) > pio.MAX_FILES:
        raise HTTPException(status_code=400,
                            detail=f"too many files ({len(files)}); pick the pipeline's own folder")
    entries = []
    for f in files:
        # A file over the size limit is skipped, as files_from_upload skips
        # it, but without reading it into memory first.
        if f.size is not None and f.size > pio.MAX_FILE_BYTES:
            continue
        entries.append((f.filename or "", await f.read()))
    try:
        file_map = pio.files_from_upload(entries)
    except pio.PipelineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    label = (label or pio.default_label()) if labeled else pio.STABLE
    source = {"kind": "upload", "files": len(file_map)}
    result = _run_import(file_map, label, source, dry_run, db, user, logger)
    if not dry_run and deploy:
        result = await _deploy_all(result, db)
    result["source"] = source
    return result


@router.get("/agents/{agent_id}/export")
def export_pipeline(
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
    zip_name = f"{pipeline}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=\"{zip_name}\"; filename*=UTF-8''{quote(zip_name, safe='')}"},
    )
