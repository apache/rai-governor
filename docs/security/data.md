# Data

## What Governor holds, and how sensitive it is

Three levels:

- **Secret:** gives access to something else. It must never be logged or shown, and is shown back masked when shown at all.
- **Restricted:** a person's or a project's data. Only they, their project and site admins see it.
- **Internal:** the service's own working data.

| Data | Where | Level | Notes |
|---|---|---|---|
| Saved LLM API keys | CouchDB `users`, `apiKeys` | Secret | Stored in plaintext. Sent back to the browser masked to the last four characters |
| Workload fixed inputs (tokens a site admin sets) | CouchDB `workloads` | Secret | Only site admins see them. A run stores a placeholder in their place and scrubs their values from what it shows |
| Session ids | `gofannon_sid` cookie | Secret | Stored only as a SHA-256 |
| Service credentials | Environment (see [Secrets](#secrets)) | Secret | |
| Agent code, config and env vars | CouchDB `agents`, `deployments` | Restricted | Agent env vars are plaintext and documented as not for secrets |
| Run inputs, outputs and traces | CouchDB `gofannon_runs` | Restricted | Inputs are stored with tokens redacted; trace lines (`print`, logging, errors) go through `redact()` |
| Agent data store | CouchDB `agent_data_store` | Restricted | Per user (or project) and namespace |
| Memory entries | S3 (SeaweedFS in production), `memory/` in the bucket; index in CouchDB `memory_index`, `memory_reviews`, `memory_usage` | Restricted | Per domain (see below); written by agents' runs and by reviewers |
| Profile and usage | CouchDB `users` | Restricted | Name, email, spend |
| Chat sessions | CouchDB `sessions` | Restricted | Owner only |
| Workers, workload catalogue | CouchDB `gofannon_workers`, `project_workloads` | Internal | |
| Logs | stdout, or CloudWatch/GCP when configured | Internal | Passed through `redact()`; no session cookies or authorization headers. Text that `redact()` fails on is replaced with `[redaction failed]`, never passed through; the first failure is logged, later ones counted |

## Who sees which fields

- **A user's saved LLM keys:** the user, masked. Calls use the real key.
- **Fixed inputs:** site admins. Members starting a workload see their own inputs and only the names of the fixed ones.
- **Run inputs and results:** the run's owner and the members of its project (`can_see_run`). Only the owner, the project's PMC or a site admin can stop it.
- **Memory:** a run for a project (every workload run) writes to the project's domain, named for it, and reads only that domain and any `MEMORY_OPEN_DOMAINS`, guidance and earlier decisions included. The project's committers and PMC and site admins read it; its PMC and site admins review it. A run for no project writes to the shared domains (`MEMORY_DOMAINS`, default `dom-scan`), which only site admins and `MEMORY_WRITERS` read unless listed in `MEMORY_OPEN_DOMAINS`. What earlier runs for no project wrote to `dom-scan`, triage included, isn't moved, and a project's runs don't see it unless `dom-scan` is open. Rules in [memory.md](../memory.md).
- **Agents, pipelines, deployments, MCP and specs:** only those allowed to run agent code (`AGENT_EXEC_ALLOWED`), since agent config can hold secrets.

## Where data goes

| Channel | Endpoint | Authentication | What crosses it |
|---|---|---|---|
| Browser → Apache → nginx → API | TLS to Apache; plain HTTP on the host's loopback and the Docker bridge | Session cookie | Everything the UI does |
| API → CouchDB | `http://couchdb:5984` on the Docker bridge | `COUCHDB_USER`/`COUCHDB_PASSWORD` (the CouchDB admin today) | All documents above |
| API → SeaweedFS (S3) | `S3_ENDPOINT_URL`, `http://seaweedfs:8333` on the bridge | `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY` | Memory entries and blobs |
| API → LLM providers | llm.apache.org (llmao), Bedrock, OpenAI, Anthropic, others the user configures | Provider key or token; Anthropic through workload identity (below) | Prompts and responses |
| API → GitHub | api.github.com | The importing user's token, if given; used once and not stored | Pipeline files |
| API → identity providers | oauth.apache.org, LDAP (`ldaps://ldap-eu.apache.org`), GitHub, Google, Microsoft | OAuth client config in `auth.yaml` | Sign-in and memberships |
| API → MCP servers | URL an author gives | Optional bearer token, sent only over https or to localhost | Tool listings and calls |
| API → any URL (`/specs/fetch`) | URL an author gives | None | An OpenAPI spec |

`STORAGE_PROVIDER` can also be `local` or `gcs`. Production uses `s3`.

`/specs/fetch` is limited to authors. It takes `http` or `https`, uses httpx's defaults (5-second timeout, no redirects followed), and checks only that the body parses as JSON or YAML. It doesn't filter internal addresses: authors already run code inside the API process, so blocking them from internal addresses here would protect nothing (see the [trust model](README.md)).

## Secrets

| Secret | Where it lives in production | Scope | Rotation today |
|---|---|---|---|
| CouchDB password | eyaml → environment | CouchDB admin | By hand: change eyaml, then re-run puppet |
| S3 secret key | eyaml → environment | The SeaweedFS root | By hand, the same way |
| `OPENAI_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`, `LLMAO_API_KEY` | eyaml → `/etc/gofannon/llm-providers.env` (0440) | One provider account each | By hand, the same way |
| OAuth client secrets | eyaml → `/etc/gofannon/auth.yaml` (0440) | Sign-in with that provider | By hand, the same way |
| `ADMIN_PANEL_PASSWORD` | Environment, if the admin panel is enabled | The admin panel | By hand. The panel refuses to open while it is unset or `password` |
| Anthropic access | No static secret: the instance role signs a short-lived JWT (AWS STS, RS256), exchanged for a cached Anthropic token | The Anthropic workspace | Automatic |
| Users' saved LLM keys | CouchDB `users` | That user's provider account | The user's own |

Nothing tracks a secret's age. To revoke one urgently, rotate it at its issuer and then here.

**Not decided yet:**

- A rotation interval for each static secret.
- Whether to replace the Bedrock bearer token with the instance role.
- Whether to give the API a CouchDB user limited to its databases instead of the admin.
- Whether to encrypt users' saved keys at rest.

## Cryptography in use

| What | Algorithm | Where | Protects |
|---|---|---|---|
| TLS | TLS 1.2/1.3, ECDHE (and DHE) suites | Apache (puppet) | Browser traffic |
| Session ids | `secrets.token_urlsafe(32)`; stored as SHA-256 | `services/session_service.py` | Sessions |
| OAuth state | `secrets.token_hex(24)`, compared with `compare_digest` | `routes_auth.py` | The sign-in flow against CSRF |
| Admin panel password | `secrets.compare_digest` | `dependencies.py` | The admin panel |
| Anthropic workload identity | RS256 JWT from AWS STS | `services/anthropic_wif.py` | Calls to Anthropic |
| Memory content hashes | SHA-256 | `services/memory/` | Integrity and change detection, not secrecy |
| Ids (runs, batches, audit entries) | `uuid4`, `secrets.token_*` | Various | Uniqueness. They are not credentials: access is checked separately |

Nothing in Governor encrypts or signs data itself. The quantum-sensitive parts are TLS key exchange and the RS256 signature. Both sit in components Governor doesn't implement (Apache/OpenSSL, AWS STS) and change when those offer post-quantum options. SHA-256 and the random tokens have no near-term quantum concern.
