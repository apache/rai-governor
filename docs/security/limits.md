# Limits

## Input rules

Pydantic models on the routes refuse a body that breaks these with 422. Checks in the services answer 400, or 403 when the input is well formed but not the caller's.

| Input | Rule | Where |
|---|---|---|
| Project name | `^[a-z0-9][a-z0-9-]{0,63}$`, **and** the caller is a member (or a site admin) | `auth/project_access.check_project` |
| Workload id | `^[a-z0-9][a-z0-9-]{0,63}$` | `models/workload.py` |
| Workload input name | `^[A-Za-z_][A-Za-z0-9_]{0,63}$` | `models/workload.py` |
| Workload input value | At most 500 characters, no control characters, then by kind: a choice must be one of its choices; a repository must be on the project's list, or else be `apache/<project>` or `apache/<project>-…`; other text must match the admin's pattern, or else be letters, digits and `. _ / + @ : = -` without `..`, `//` or a trailing `/` | `services/workload_service.check_value` |
| Pipeline name (`pipeline.yaml`) | `^[a-z0-9][a-z0-9_-]{0,62}$` | `services/pipeline_io.py` |
| Agent name in a pipeline | `^[A-Za-z0-9][A-Za-z0-9 _.-]{0,120}$` (no `@`) | ″ |
| Import label | Empty (the stable copy), or `^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$` | ″ |
| Env var key | `^[A-Za-z_][A-Za-z0-9_]*$` | ″ |
| GitHub import | `repo` is `owner/name`; repo and ref at most 256 characters, path 1024, token 4096 | `routes_pipelines.py` |
| Pipeline file paths | Resolved against the manifest's folder; refused if they leave it (`..`) or are absolute | `services/pipeline_io.py` |
| Memory entry | Body up to 32,000 characters, summary 300, title 200, up to 50 sources of 500 each, origin 500 | `routes_memory.py` |
| Triage guidance | Up to 32,000 characters | `services/memory/triage.py` |
| Login `return_to` | A path on this site: one leading `/`, no backslash or control characters, at most 2048 characters | `routes_auth.py` |
| Client log event | Event type of up to 64 characters `[A-Za-z0-9_.:-]` (stored as `client:<type>`), a known level, message up to 2000 characters, metadata up to 8 KB | `routes.py` |
| MCP server | URL up to 2048 characters, token up to 4096; a token only over https or to localhost | `services/mcp_client_service.py` |

There is no list of allowed file types for pipeline uploads: any UTF-8 text file is read and anything else is skipped. Only the files the manifest names are used.

### Rules that depend on who is asking

A well-formed value isn't enough when it names something:

- A project in a request must be one the caller belongs to.
- A repository input must be one of that project's.
- A run is seen by its owner and its project's members, and stopped by its owner, the project's PMC or a site admin.
- A data-store document is returned only when its stored owner and namespace match the request, not just its id.

## Size and count limits

| Limit | Value | Configurable |
|---|---|---|
| Request body (Apache) | 64 MB | `LimitRequestBody` in the vhost |
| Pipeline upload | 500 files; a file over 2 MB is skipped without being read | No |
| Trace events per run | 2,000, each message up to 4,096 bytes | No |
| Agent runs going at once, site-wide (all workers) | Unlimited; past the limit a run queues | `RUN_MAX_CONCURRENT` (0 = unlimited) |
| Agent runs going at once, per person | Unlimited; past the limit a run queues | `RUN_MAX_PER_USER` (0 = unlimited) |
| Memory recall | 5 entries, 6,000 characters | `MEMORY_RECALL_MAX_ENTRIES`, `MEMORY_RECALL_MAX_CHARS` |
| Memory search hits | 1,000 | No |
| Session lifetime | 24 hours | `session_ttl_hours` in `auth.yaml` |
| Session idle timeout | 8 hours, to within the membership refresh interval | `session_idle_hours` in `auth.yaml` (0 = off) |
| Membership refresh | Every 15 minutes | `workspace_refresh_minutes` in `auth.yaml` |
| Spend | A per-user monthly allowance, checked before each LLM call | Per user, by an admin |

## Timeouts and retries

| What | Value | Configurable |
|---|---|---|
| Apache request read | headers 20–40 s, body 60 s, with minimum rates (`RequestReadTimeout`) | Vhost |
| Apache and nginx proxy | 3,600 s, for long agent runs and SSE | Vhost, `nginx.conf` |
| gunicorn worker | 1,800 s without a heartbeat | `GUNICORN_TIMEOUT` |
| LLM call | 600 s; connect 10 s; stream idle 300 s | `LLM_TIMEOUT_SECONDS`, `LLM_CONNECT_TIMEOUT_SECONDS`, `LLM_STREAM_IDLE_TIMEOUT_SECONDS` |
| LLM retries | Each kind has its own budget: timeouts 2, rate limits 5, overloaded 5, auth re-mint 1, unrouted model 4. Backoff is capped at 60 s | `LLM_*_RETRIES`, `LLM_RATE_LIMIT_BACKOFF_CAP` |
| LLM connections | litellm sends over aiohttp: at most 1,000 connections per worker process, 500 to one host. A call that finds the pool full waits for a connection inside its own timeout, then fails as a timeout and takes the timeout retries. Pipelines' own concurrency settings keep real use far below the limit | `AIOHTTP_CONNECTOR_LIMIT`, `AIOHTTP_CONNECTOR_LIMIT_PER_HOST` |
| `/specs/fetch` | 5 s | No |
| CouchDB | 300 s, connecting and each wait for data (python-couchdb has one socket timeout for both). Long because CouchDB answers a view or indexed query only once its index is built, which takes minutes on this data after a design doc changes | `COUCHDB_TIMEOUT_SECONDS` |
| Object store (S3/SeaweedFS) | connect 10 s, read 60 s. The read timeout is the wait for the next bytes, not the whole transfer; the objects are memory entries of tens of KB. botocore retries a timed-out call up to 4 times | `STORAGE_CONNECT_TIMEOUT_SECONDS`, `STORAGE_READ_TIMEOUT_SECONDS` |

## The operations that cost the most

| Operation | What protects availability |
|---|---|
| Agent runs (LLM calls, data store, memory) | Only authors run agent code. Spend allowance. LLM timeouts and retry budgets. `RUN_MAX_CONCURRENT` and `RUN_MAX_PER_USER` queue runs (below). A streamed run gets its own thread: at most `AGENT_MAX_THREADS` (20) per worker, and past that a run fails at once |
| Long requests holding a worker | 4 gunicorn workers (`WEB_CONCURRENCY`) of async uvicorn. Blocking database calls run on worker threads |
| Pipeline import | Author only; the file count and size limits above |
| Uploads | Apache's 64 MB body limit |
| Memory recall and search | The entry, character and hit caps above |

### Run queue

With `RUN_MAX_CONCURRENT` or `RUN_MAX_PER_USER` set, a run that would go over is recorded as `queued` and starts by itself when a slot frees, oldest first; a queued run of someone at their own limit is passed over, not made to hold up the others. The run pages show how many queued runs are ahead. Stopping a queued run takes it off the queue. The count is kept in the run records in CouchDB, so it holds across all workers. Every recorded run counts: workloads, `/rest/{name}/start`, `/agents/run-code/start` and `/stream`. A pipeline's calls to its sub-agents run inside its run and do not count. The synchronous `/rest/{name}` and `/agents/run-code` keep no record and are not limited. If a worker dies, its queued and running runs are closed by the orphan sweep once its heartbeat is `WORKER_DEAD_AFTER_SECONDS` (120 s) old, which frees their slots. Keep `RUN_MAX_CONCURRENT` at or below `AGENT_MAX_THREADS` times the worker count.

Rate limiting isn't done anywhere, neither in Apache nor in the app. Where to add it (Apache `mod_ratelimit`/`mod_evasive`, or per user in the app) is not decided yet. Neither are a circuit breaker for providers that keep failing, nor alerting on resource exhaustion beyond the logs.
