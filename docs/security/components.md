# Components and risky code

## Third-party components

The dependency inventory is in the repository, pinned to exact versions:

| Part | Constraints | Exact tree, as built |
|---|---|---|
| API (Python) | `webapp/packages/api/user-service/requirements.txt` | `requirements.lock`; the image installs from it alone |
| Web UI (npm) | `webapp/packages/*/package.json` | `webapp/pnpm-lock.yaml`, installed with `--frozen-lockfile` |
| Containers | `webapp/infra/docker/Dockerfile.*` | Base images by tag. Puppet pins CouchDB and SeaweedFS by digest |

Packages come from PyPI and the npm registry, and images from Docker Hub. Dependabot opens weekly updates for pip, npm and Docker (`.github/dependabot.yml`), and GitHub's advisories flag vulnerable versions. No SBOM is published. If one is wanted, CycloneDX can be generated from the two lock files in CI.

**Not decided yet:** remediation times by severity (for example, critical within 7 days), who owns the triage, and whether to publish an SBOM.

### Components that need extra care

| Component | Why | What limits it |
|---|---|---|
| `litellm` | Large, fast-moving. It sees every prompt and every provider key | Pinned to a minor range (`>=1.100.4,<1.106`). The lock is updated deliberately, and retries and timeouts are Governor's own |
| `fastmcp` | Connects to servers an author names, possibly with a token | Author only. URL and token length caps. A token only over https or to localhost. Pinned `<4` |
| `CouchDB` (python-couchdb) | Little maintenance, and no socket timeout, so a stalled database blocks the calling thread | Session lookups run off the event loop, but some calls still block it. gunicorn restarts a worker whose loop stops answering for 30 minutes (`GUNICORN_TIMEOUT`) |
| `ldap3` | Every ASF sign-in and membership refresh goes through it | LDAPS. A failure keeps the session's memberships instead of revoking them |
| `PyYAML` | Parses pipeline manifests and memory entries from users | `safe_load` everywhere |
| `firebase-admin` | Legacy sign-in path | Used only when `APP_ENV=firebase`; production isn't |

Each is reviewed when Dependabot proposes a new version.

## Code that needs extra care

| Where | What | Risk | Control |
|---|---|---|---|
| `dependencies.py`, `_execute_agent_code` | `compile()` and `exec()` of agent code, with the real builtins, inside the API process | Agent code can do anything the API can | Only `AGENT_EXEC_ALLOWED` users may run or author agents. Moving it out of process is ISSUE-023 stage 2 |
| `services/environ_proxy.py` | Replaces `os.environ` with a per-run overlay so an agent's env vars don't leak between runs | An agent writing the environment directly affects the process | The same author-only boundary |
| `config/routes_config.py` | `importlib.import_module` of router modules named in config | Imports code the config names | The config is the operator's, not a user's |
| `services/pipeline_io.py`, `services/memory/entry.py`, `config/__init__.py`, `agent_factory/swagger_parser.py`, `dependencies.fetch_spec_content` | `yaml.safe_load` of user or operator input | YAML bombs, unexpected types | `safe_load` only. Pipeline files are capped at 2 MB, and results are validated after parsing |
| `services/pipeline_io.py` | Builds paths from a manifest | Path traversal | Normalised against the manifest's folder; `..` and absolute paths refused |
| `dependencies.fetch_spec_content`, `services/mcp_client_service.py`, `agent_factory/remote_mcp_client.py` | Outbound requests to URLs a user gives | SSRF | Author only (see the [trust model](README.md)); a 5-second timeout for specs |
| `pages/ViewDemoAppPage.jsx`, `pages/DemoCreationFlow/CanvasScreen.jsx` | Run generated HTML and JavaScript in a `srcdoc` iframe with `allow-scripts allow-same-origin` | The generated code runs on the app's origin with the viewer's session | Demo code is generated from an author's prompt. Moving demos to their own origin is not decided yet; until it is, the CSP can't restrict scripts |
| `routes_auth.py`, dev-stub picker | Writes HTML by hand | XSS | Values are HTML-escaped; the route exists only when `APP_ENV` is `local`, `dev` or `test` |

A change to any of these files should get a second reviewer.

## Finding cryptography and risky calls

There is no automated check yet. To list the cryptography in use, or the calls above:

```
grep -rnE "hashlib|hmac|secrets\.|jwt|cryptography|ssl\." webapp/packages/api/user-service --include=*.py
grep -rnE "\bexec\(|\beval\(|compile\(|importlib|yaml\.(load|safe_load)|subprocess|pickle" webapp/packages/api/user-service --include=*.py
```

[Data](data.md#cryptography-in-use) has the current inventory. Whether to run these in CI is not decided yet.
