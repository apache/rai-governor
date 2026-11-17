"""Pipeline import and export (ISSUE-011).

A pipeline is a set of agents that call each other: one entry agent and
everything it reaches through gofannon_agents. It lives in a folder:

    pipeline.yaml              which agents, who calls whom, schemas, env
    agents/<name>.py           each agent's code
    agents/env/<name>.env      an agent's env vars, KEY=VALUE (optional)

The layout matches tooling-agents (ASVS/agents/*.py), so an exported zip
unpacks over a pipeline's folder there. A single agent is a pipeline of one.

pipeline.yaml:

    pipeline: asvs                       # [a-z0-9_-], required
    description: ...                     # optional
    entry: asvs_orchestrate              # optional, the agent you run
    agents:
      - name: asvs_orchestrate           # required, the name code calls it by
        code: agents/asvs_orchestrate.py # required, relative to pipeline.yaml
        description: ...
        friendly_name: ...               # optional
        docstring: ...                   # optional
        input_schema: {...}              # optional
        output_schema: {...}             # optional
        calls: [asvs_discover]           # gofannon agents it calls, by name
        tools: {<mcp url>: [tool, ...]}  # optional
        data_store: [{namespace, access, description}]  # optional
        invokable_models: [...]          # optional
        swagger_specs: [{name, content}] # optional
        env_file: agents/env/x.env       # optional
        env: {KEY: value}                # optional, wins over env_file

IMPORTED COPIES. Each import makes, or updates, one copy of the pipeline
under a label: the short commit hash for GitHub, a UTC timestamp for a
folder upload, or one the user types. Agents in the copy are stored as
"<name>@<label>" with pipeline, label and base name recorded, so several
copies (a branch, a big change) live side by side. Agent code still calls
the plain name: GofannonClient resolves it to the agent in the caller's own
gofannon_agents, which point into the same copy. Importing the same
pipeline and label again updates that copy in place.

The stable copy has no label (STABLE, ""): its agents keep their own names
and are deployed at /rest/<name>, and each unlabeled import updates it in
place. So there are usually two copies worth having: stable, and the newest
labelled one.
"""

from __future__ import annotations

import base64
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from posixpath import dirname, join, normpath
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

MANIFEST = "pipeline.yaml"
_PIPELINE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# Agent names made in the UI can have spaces; code calls them by that name.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,120}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")

# The stable copy's label: none.
STABLE = ""
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_FILES = 500
MAX_FILE_BYTES = 2_000_000


class PipelineError(ValueError):
    """A manifest or source that cannot be imported. The message is for users."""


# --- env files --------------------------------------------------------------


def parse_env(text: str, where: str = "env file") -> List[Tuple[str, str]]:
    """KEY=VALUE lines. Blank lines and # comments are skipped, as is a
    trailing comment after whitespace (`X=80   # note`). Values keep
    everything else, including an empty value (`X=`)."""
    out: List[Tuple[str, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise PipelineError(f"{where}:{n}: expected KEY=VALUE, got {raw!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            raise PipelineError(f"{where}:{n}: {key!r} is not a valid variable name")
        value = value.strip()
        if value[:1] in ("'", '"') and value[0] in value[1:]:
            # Quoted: everything inside the quotes, "#" included.
            value = value[1:value.index(value[0], 1)]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out.append((key, value))
    return out


def render_env(env_vars: List[Dict[str, Any]]) -> str:
    lines = []
    for ev in env_vars:
        desc = (ev.get("description") or "").strip()
        if desc:
            lines.extend(f"# {d}" for d in desc.splitlines())
        lines.append(f"{ev['key']}={ev.get('value', '')}")
    return "\n".join(lines) + "\n"


# --- manifest ---------------------------------------------------------------


@dataclass
class ManifestAgent:
    name: str
    code: str
    description: str = ""
    friendly_name: Optional[str] = None
    docstring: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    calls: List[str] = field(default_factory=list)
    tools: Dict[str, List[str]] = field(default_factory=dict)
    data_store: List[Dict[str, Any]] = field(default_factory=list)
    invokable_models: Optional[List[Dict[str, Any]]] = None
    swagger_specs: List[Dict[str, Any]] = field(default_factory=list)
    env: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class Manifest:
    pipeline: str
    description: str
    entry: Optional[str]
    agents: List[ManifestAgent]


def find_manifest(files: Dict[str, str]) -> str:
    """The path of the one pipeline.yaml in `files` (shallowest wins)."""
    found = sorted(
        (p for p in files if p == MANIFEST or p.endswith("/" + MANIFEST)),
        key=lambda p: (p.count("/"), p),
    )
    if not found:
        raise PipelineError(f"no {MANIFEST} found")
    if len(found) > 1 and found[0].count("/") == found[1].count("/"):
        raise PipelineError(
            f"more than one {MANIFEST} at the same depth: {found[0]}, {found[1]}; "
            f"choose the pipeline's own folder")
    return found[0]


def _rel(base: str, path: str) -> str:
    p = normpath(join(base, path)) if base else normpath(path)
    if p.startswith("../") or p == ".." or p.startswith("/"):
        raise PipelineError(f"{path!r} points outside the pipeline folder")
    return p


def load_manifest(files: Dict[str, str]) -> Manifest:
    """Parse and check a pipeline folder given as {relative path: text}."""
    mpath = find_manifest(files)
    base = dirname(mpath)
    try:
        doc = yaml.safe_load(files[mpath]) or {}
    except yaml.YAMLError as e:
        raise PipelineError(f"{mpath}: not valid YAML: {e}") from e
    if not isinstance(doc, dict):
        raise PipelineError(f"{mpath}: expected a mapping at the top level")

    pipeline = str(doc.get("pipeline") or "").strip()
    if not _PIPELINE_RE.match(pipeline):
        raise PipelineError(
            f"{mpath}: 'pipeline' must be lowercase letters, digits, - or _ "
            f"(got {pipeline!r})")
    raw_agents = doc.get("agents")
    if not isinstance(raw_agents, list) or not raw_agents:
        raise PipelineError(f"{mpath}: 'agents' must be a non-empty list")

    agents: List[ManifestAgent] = []
    seen = set()
    for i, a in enumerate(raw_agents):
        where = f"{mpath}: agents[{i}]"
        if not isinstance(a, dict):
            raise PipelineError(f"{where}: expected a mapping")
        name = str(a.get("name") or "").strip()
        if not _NAME_RE.match(name) or "@" in name:
            raise PipelineError(f"{where}: invalid name {name!r}")
        if name in seen:
            raise PipelineError(f"{where}: duplicate agent {name!r}")
        seen.add(name)
        code_path = a.get("code")
        if not code_path:
            raise PipelineError(f"{where} ({name}): 'code' is required")
        code_rel = _rel(base, str(code_path))
        if code_rel not in files:
            raise PipelineError(f"{name}: code file {code_path!r} not found")

        env: Dict[str, str] = {}
        if a.get("env_file"):
            env_rel = _rel(base, str(a["env_file"]))
            if env_rel not in files:
                raise PipelineError(f"{name}: env file {a['env_file']!r} not found")
            env.update(parse_env(files[env_rel], env_rel))
        inline = a.get("env") or {}
        if not isinstance(inline, dict):
            raise PipelineError(f"{where} ({name}): 'env' must be a mapping")
        for k, v in inline.items():
            if not _ENV_KEY_RE.match(str(k)):
                raise PipelineError(f"{name}: {k!r} is not a valid variable name")
            env[str(k)] = "" if v is None else str(v)

        calls = a.get("calls") or []
        if not isinstance(calls, list) or not all(isinstance(c, str) for c in calls):
            raise PipelineError(f"{where} ({name}): 'calls' must be a list of names")
        agents.append(ManifestAgent(
            name=name,
            code=files[code_rel],
            description=str(a.get("description") or ""),
            friendly_name=a.get("friendly_name"),
            docstring=a.get("docstring"),
            input_schema=a.get("input_schema"),
            output_schema=a.get("output_schema"),
            calls=[c.strip() for c in calls],
            tools=a.get("tools") or {},
            data_store=a.get("data_store") or [],
            invokable_models=a.get("invokable_models"),
            swagger_specs=a.get("swagger_specs") or [],
            env=list(env.items()),
        ))

    entry = doc.get("entry")
    if entry is not None and entry not in seen:
        raise PipelineError(f"{mpath}: entry {entry!r} is not one of the agents")
    return Manifest(pipeline=pipeline, description=str(doc.get("description") or ""),
                    entry=entry, agents=agents)


# --- labels and names -------------------------------------------------------


def default_label(commit_sha: Optional[str] = None, now: Optional[datetime] = None) -> str:
    if commit_sha:
        return commit_sha[:7]
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")


def check_label(label: str) -> str:
    label = (label or "").strip()
    if label == STABLE:
        return STABLE
    if not _LABEL_RE.match(label):
        raise PipelineError(
            f"label {label!r}: letters, digits, '.', '_' or '-', up to 40 characters")
    return label


def copy_name(base: str, label: str) -> str:
    return f"{base}@{label}" if label else base


def copy_friendly_name(friendly: Optional[str], base: str, label: str) -> str:
    """Friendly names are deployment URL paths (/rest/{friendly_name}), so the
    label is joined with '-' and the result kept URL-safe."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", friendly or base).strip("-") or base
    if not label:
        return stem
    return f"{stem}-{re.sub(r'[^A-Za-z0-9_-]+', '-', label)}"


# --- import plan ------------------------------------------------------------


def _manifest_fields(m: ManifestAgent, label: str) -> Dict[str, Any]:
    return {
        "name": copy_name(m.name, label),
        "description": m.description,
        "code": m.code,
        "docstring": m.docstring,
        "friendlyName": copy_friendly_name(m.friendly_name, m.name, label),
        "tools": m.tools,
        "swaggerSpecs": m.swagger_specs,
        "inputSchema": m.input_schema,
        "outputSchema": m.output_schema,
        "invokableModels": m.invokable_models,
        "dataStoreConfig": m.data_store,
        "envVars": [{"key": k, "value": v, "description": None} for k, v in m.env],
    }


_COMPARED = {
    "code": "code",
    "description": "description",
    "docstring": "docstring",
    "inputSchema": "input schema",
    "outputSchema": "output schema",
    "tools": "tools",
    "dataStoreConfig": "data store",
    "invokableModels": "models",
    "swaggerSpecs": "swagger specs",
}


def _env_map(env_vars: Any) -> Dict[str, str]:
    return {e.get("key"): e.get("value", "") for e in (env_vars or []) if isinstance(e, dict)}


def plan_import(manifest: Manifest, label: str, existing_agents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What an import would do, without writing anything.

    existing_agents: every stored agent document (camelCase keys, `_id`).
    """
    label = check_label(label)
    in_copy = {
        a.get("baseName"): a for a in existing_agents
        if a.get("pipeline") == manifest.pipeline and a.get("pipelineLabel") == label
    }
    by_name = {a.get("name"): a for a in existing_agents}
    local = {m.name for m in manifest.agents}

    rows, problems = [], []
    for m in manifest.agents:
        target = copy_name(m.name, label)
        current = in_copy.get(m.name)
        if current is None and target in by_name:
            problems.append(
                f"{target}: an agent with this name exists but is not part of "
                + (f"{manifest.pipeline}@{label}; rename it or use another label" if label else
                   f"{manifest.pipeline}'s stable copy; rename or delete it, or import with a label"))
        want = _manifest_fields(m, label)
        changes: List[str] = []
        if current is not None:
            for key, label_text in _COMPARED.items():
                if (current.get(key) or None) != (want.get(key) or None):
                    changes.append(label_text)
            if _env_map(current.get("envVars")) != _env_map(want["envVars"]):
                changes.append("env")
        external = []
        for c in m.calls:
            if c in local:
                continue
            if c in by_name:
                external.append(c)
            else:
                problems.append(f"{m.name}: calls {c!r}, which is neither in the "
                                f"pipeline nor an existing agent")
        if current is not None:
            old_calls = sorted(_calls_by_name(current, existing_agents))
            if old_calls != sorted(m.calls):
                changes.append("calls")
        rows.append({
            "agent": m.name,
            "name": target,
            "action": "create" if current is None else ("update" if changes else "unchanged"),
            "changes": changes,
            "calls": m.calls,
            "externalCalls": external,
            "envCount": len(m.env),
        })
    return {
        "pipeline": manifest.pipeline,
        "label": label,
        "entry": manifest.entry or (manifest.agents[0].name if len(manifest.agents) == 1 else None),
        "agents": rows,
        "problems": problems,
    }


def _calls_by_name(agent_doc: Dict[str, Any], all_agents: List[Dict[str, Any]]) -> List[str]:
    by_id = {a.get("_id"): a for a in all_agents}
    names = []
    for dep_id in agent_doc.get("gofannonAgents") or []:
        dep = by_id.get(dep_id)
        if dep is not None:
            names.append(dep.get("baseName") or dep.get("name"))
    return names


def apply_import(
    manifest: Manifest,
    label: str,
    source: Dict[str, Any],
    existing_agents: List[Dict[str, Any]],
    save,
    new_id,
) -> Dict[str, Any]:
    """Create or update the copy. `save(doc)` stores one agent document and
    returns its new `_rev`; `new_id()` makes an id. Returns the plan plus
    the ids written."""
    plan = plan_import(manifest, label, existing_agents)
    if plan["problems"]:
        raise PipelineError("; ".join(plan["problems"]))
    label = plan["label"]
    in_copy = {
        a.get("baseName"): a for a in existing_agents
        if a.get("pipeline") == manifest.pipeline and a.get("pipelineLabel") == label
    }
    by_name = {a.get("name"): a for a in existing_agents}
    ids = {m.name: (in_copy[m.name]["_id"] if m.name in in_copy else new_id())
           for m in manifest.agents}
    stamp = datetime.now(timezone.utc).isoformat()
    written = {}
    for m in manifest.agents:
        current = in_copy.get(m.name)
        doc = dict(current) if current else {"_id": ids[m.name]}
        doc.update(_manifest_fields(m, label))
        doc["gofannonAgents"] = [ids[c] if c in ids else by_name[c]["_id"] for c in m.calls]
        doc["pipeline"] = manifest.pipeline
        doc["pipelineLabel"] = label
        doc["baseName"] = m.name
        doc["pipelineSource"] = {**source, "importedAt": stamp}
        written[m.name] = {"id": doc["_id"], "rev": save(doc)}
    plan["written"] = written
    plan["entryId"] = written.get(plan["entry"], {}).get("id") if plan["entry"] else None
    return plan


# --- export -----------------------------------------------------------------


def collect_pipeline(root_id: str, get_agent) -> List[Dict[str, Any]]:
    """The root agent and every agent it reaches, root first."""
    order, seen, stack = [], set(), [root_id]
    while stack:
        aid = stack.pop(0)
        if aid in seen:
            continue
        seen.add(aid)
        doc = get_agent(aid)
        if doc is None:
            raise PipelineError(f"agent {aid} not found")
        order.append(doc)
        stack.extend(doc.get("gofannonAgents") or [])
    return order


def _export_name(doc: Dict[str, Any]) -> str:
    return doc.get("baseName") or doc["name"]


def build_export(docs: List[Dict[str, Any]], pipeline: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """Files for a pipeline folder, from agent documents (root first)."""
    root = docs[0]
    name_of = {d["_id"]: _export_name(d) for d in docs}
    if len(set(name_of.values())) != len(name_of):
        raise PipelineError("two agents in this pipeline export under the same name")
    pipeline = pipeline or root.get("pipeline") or re.sub(
        r"[^a-z0-9_-]+", "-", _export_name(root).lower()).strip("-") or "pipeline"
    files: Dict[str, str] = {}
    entries = []
    for d in docs:
        name = name_of[d["_id"]]
        code_path = f"agents/{name}.py"
        files[code_path] = d.get("code") or ""
        e: Dict[str, Any] = {"name": name, "code": code_path}
        if d.get("description"):
            e["description"] = d["description"]
        base_friendly = d.get("friendlyName")
        if base_friendly and d.get("pipelineLabel"):
            suffix = "-" + re.sub(r"[^A-Za-z0-9_-]+", "-", d["pipelineLabel"])
            if base_friendly.endswith(suffix):
                base_friendly = base_friendly[: -len(suffix)]
        if base_friendly:
            e["friendly_name"] = base_friendly
        for key, out in (("docstring", "docstring"), ("inputSchema", "input_schema"),
                         ("outputSchema", "output_schema"), ("tools", "tools"),
                         ("dataStoreConfig", "data_store"),
                         ("invokableModels", "invokable_models"),
                         ("swaggerSpecs", "swagger_specs")):
            if d.get(key):
                e[out] = d[key]
        calls = [name_of.get(i) for i in d.get("gofannonAgents") or [] if i in name_of]
        if calls:
            e["calls"] = calls
        env = d.get("envVars") or []
        if env:
            env_path = f"agents/env/{name}.env"
            files[env_path] = render_env(env)
            e["env_file"] = env_path
        entries.append(e)
    manifest = {"pipeline": pipeline, "entry": name_of[root["_id"]], "agents": entries}
    if root.get("pipelineSource"):
        manifest["description"] = (
            f"Exported from gofannon: {pipeline}@{root.get('pipelineLabel') or 'stable'}")
    files[MANIFEST] = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=100)
    return pipeline, files


def zip_files(files: Dict[str, str], folder: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(files):
            z.writestr(f"{folder}/{path}", files[path])
    return buf.getvalue()


# --- sources ----------------------------------------------------------------


def files_from_upload(entries: List[Tuple[str, bytes]]) -> Dict[str, str]:
    """{relative path: text} from a folder upload. The browser sends paths as
    "<picked folder>/<sub>/<file>"; the picked folder's own name is dropped
    so paths are relative to it. Binary files are skipped."""
    if len(entries) > MAX_FILES:
        raise PipelineError(f"too many files ({len(entries)}); pick the pipeline's own folder")
    files: Dict[str, str] = {}
    for path, data in entries:
        path = path.replace("\\", "/").lstrip("/")
        if len(data) > MAX_FILE_BYTES:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        files[path] = text
    tops = {p.split("/", 1)[0] for p in files if "/" in p}
    if len(tops) == 1 and all("/" in p for p in files):
        top = tops.pop() + "/"
        files = {p[len(top):]: t for p, t in files.items()}
    return files


GITHUB_API = "https://api.github.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


async def files_from_github(repo: str, ref: str, path: str, token: Optional[str],
                            client: Optional[httpx.AsyncClient] = None) -> Tuple[Dict[str, str], str]:
    """({relative path: text}, commit sha) for one folder of a GitHub repo at
    a ref. Reads the folder's tree once, then fetches only the files the
    manifest needs: pipeline.yaml, then each code and env file."""
    if not _REPO_RE.match(repo or ""):
        raise PipelineError(f"repo must be owner/name (got {repo!r})")
    ref = (ref or "").strip() or "HEAD"
    folder = (path or "").strip().strip("/")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        r = await client.get(f"{GITHUB_API}/repos/{repo}/commits/{ref}", headers=headers)
        if r.status_code in (401, 403, 404):
            try:
                said = r.json().get("message") or ""
            except Exception:
                said = r.text[:200]
            hint = ("the repo or ref does not exist, or it is private and needs a token"
                    if r.status_code != 403 or "rate limit" not in said.lower()
                    else "GitHub's rate limit for requests without a token; add one")
            raise PipelineError(
                f"GitHub {r.status_code} for {repo}@{ref}: {hint}"
                + (f" (GitHub said: {said})" if said else ""))
        r.raise_for_status()
        sha = r.json()["sha"]
        r = await client.get(f"{GITHUB_API}/repos/{repo}/git/trees/{sha}",
                             params={"recursive": "1"}, headers=headers)
        r.raise_for_status()
        prefix = folder + "/" if folder else ""
        blobs = {e["path"][len(prefix):]: e["sha"] for e in r.json().get("tree", [])
                 if e.get("type") == "blob" and e["path"].startswith(prefix)}

        async def fetch(rel: str) -> str:
            if rel not in blobs:
                raise PipelineError(f"{prefix}{rel} not found in {repo}@{sha[:7]}")
            b = await client.get(f"{GITHUB_API}/repos/{repo}/git/blobs/{blobs[rel]}",
                                 headers=headers)
            b.raise_for_status()
            return base64.b64decode(b.json()["content"]).decode("utf-8")

        if MANIFEST not in blobs:
            raise PipelineError(f"no {MANIFEST} in {repo}@{sha[:7]}:{folder or '/'}")
        files = {MANIFEST: await fetch(MANIFEST)}
        doc = yaml.safe_load(files[MANIFEST]) or {}
        for a in (doc.get("agents") or []) if isinstance(doc, dict) else []:
            for key in ("code", "env_file"):
                if isinstance(a, dict) and a.get(key):
                    rel = _rel("", str(a[key]))
                    if rel not in files:
                        files[rel] = await fetch(rel)
        return files, sha
    except httpx.HTTPError as e:
        raise PipelineError(f"GitHub request failed: {type(e).__name__}: {e}") from e
    finally:
        if own:
            await client.aclose()
