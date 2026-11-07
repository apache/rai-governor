# Memory

Memory is a shared record of what agents and people have found: facts, decisions, conventions, methods and handoffs. Each entry records where it came from and how far it has been checked. It is a pilot. Entries use the format [apache/tooling-memory](https://github.com/apache/tooling-memory) uses, so that project's tools can read what gofannon writes.

## Turning it on

Memory is off unless the API has `MEMORY_ENABLED=true`. While it is off, nothing under `/memory` exists and the **Memory** link doesn't show.

| Variable | Default | |
|---|---|---|
| `MEMORY_ENABLED` | `false` | the switch |
| `MEMORY_DOMAINS` | `dom-scan` | the domains that exist, comma-separated |
| `MEMORY_WRITERS` | none | uids besides site admins who may write and review, e.g. `asf:akm` |
| `MEMORY_RECALL_PROVIDER` | `llmao` | the model that writes Ask's briefs |
| `MEMORY_RECALL_MODEL` | `qwen3.8-27b` | |
| `MEMORY_S3_BUCKET` | `S3_BUCKET_NAME` | where entries are stored; keep it private |
| `MEMORY_S3_ROOT` | `memory/` | |

Entries are stored in the S3-compatible store when `STORAGE_PROVIDER=s3`. Otherwise they are kept in process memory: fine for trying it out, but lost on restart, and each worker has its own copy. The index (`memory_index`) and review records (`memory_reviews`) go in the database and are created on first use.

During the pilot, anyone signed in can read every entry in every domain. Don't record anything that isn't public or already fixed.

## Who can do what

- **Anyone signed in** can browse, read and ask.
- **Site admins and `MEMORY_WRITERS`** can also write entries and review them.
- **Nobody reviews an entry they wrote.** The approver is never the proposer.

## The Memory page

**Memory** in the top bar opens it at `/memories`. Pick a domain at the top right.

- **Browse** searches titles and summaries. Filter by type or trust, show only entries due for review, or include demoted and superseded ones. Pick an entry to read it beside the list.
- **Ask** answers a question from the domain's entries. A model writes a short brief that cites the entries it used; the entries are listed alongside. If any of them is unreviewed, it says so. If nothing matches, no model is called.
- **Review** lists raw entries and entries past their review date, oldest first. The count on the tab is how many are waiting.

Each entry has its own page (`/memories/entries/<id>`) with its provenance, its review history, and the earlier versions it replaced.

## Trust and review

An entry starts **raw** (empty disc). A review moves it on:

| Decision | From | Result | Needs a why |
|---|---|---|---|
| Curate | raw | **curated** (half disc): read and fit to cite | yes |
| Verify | raw or curated | **verified** (full disc): checked against the source, safe to act on | yes |
| Keep | any | same tier; the review date moves on (90 days unless you pick one) | no |
| Demote | any | **demoted**: wrong or no longer true; out of search and Ask, kept in history | yes |

Every review is recorded with who made it, why, and which version of the entry it judged. Each review that changes the entry keeps the version it replaced.

## In agent code

Agent code has a `memory` object beside `data_store`. `memory.enabled` is false unless memory is on, and every call raises while it is off, so check it first:

```python
async def run(input_dict, tools):
    if memory.enabled:
        known = await memory.search("order report", type="fact")      # summaries
        brief = await memory.recall("what is known about the order report?")
        entry_id = await memory.write(
            summary="String-built SQL in the order report query",     # one line
            body="# SQLi in OrderReport\n\nThe sort parameter is concatenated.\n",
            type="fact",
            source=["https://github.com/apache/example/blob/3f9c2e1/OrderReport.java#L88"],
            origin="asvs:apache/example",   # groups entries from one source
            model=model,                    # the model that produced it
        )
```

| Call | Returns |
|---|---|
| `search(query, domain, type, trust_tier, origin, limit)` | summaries of live entries |
| `get(id)` | `{"frontmatter", "body"}`, or `None` |
| `recall(question, domain)` | `{"brief", "cited", "entries", "matched"}`; billed to whoever runs the agent |
| `write(summary, body, type, domain, title, source, confidence, review_by, supersedes, origin, model)` | the new entry's id |

`domain` defaults to the first of `MEMORY_DOMAINS`. Agents write at raw, as `agent:<agent name> (model: <model>)`, and only when the person running them may write memory; otherwise `write` raises `PermissionError`. `memory.written` lists the ids this run wrote.

## API

All under `/memory`, with a session.

| Method and path | |
|---|---|
| `GET /memory/domains` | domains with entry, raw, due and review-queue counts; what you may do |
| `GET /memory/{domain}/search` | summaries. `q`, `type`, `trust_tier`, `due_by`, `include_inactive`, `order=newest\|oldest`, `limit` |
| `GET /memory/entries/{id}` | one entry: frontmatter and body |
| `GET /memory/entries/{id}/history` | the versions it replaced and its reviews |
| `GET /memory/entries/{id}/versions/{version}` | one earlier version |
| `POST /memory/{domain}/recall` | `{question}` → a cited brief |
| `POST /memory/{domain}/entries` | write an entry, always at raw (writers) |
| `POST /memory/entries/{id}/review` | `{action, why, review_by}` (writers, not the author) |
