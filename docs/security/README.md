# Security

How Governor protects what it holds, written against the code. Where something is a decision not yet taken, the page says so.

| Page | Covers |
|---|---|
| [Authentication and sessions](authentication.md) | Sign-in pathways, what each checks, session lifetime and refresh, cookies, sign-in events |
| [Data](data.md) | What data Governor holds and how sensitive it is, who sees which fields, where data goes, secrets and keys |
| [Limits](limits.md) | Input rules, size and count limits, timeouts, and the operations that cost the most |
| [Browsers](browsers.md) | What the UI expects of a browser, and what happens without each feature |
| [Components and risky code](components.md) | Third-party dependencies, how they are tracked, and the code that needs extra care |

## The trust model in one paragraph

Agent code runs with `exec()` inside the API process. Whoever can run it can read the process environment, use the instance's AWS role and reach the database with the API's credentials. So running agent code is limited by `AGENT_EXEC_ALLOWED` (default: site admins only, plus `AGENT_EXEC_ALLOWED_UIDS`), and every "author" route (agents, pipelines, deployments, specs, MCP) is limited the same way. Inside that boundary, controls against a malicious author (SSRF from `/specs/fetch`, environment writes) cross no boundary and are not attempted. Moving agent code out of process (ISSUE-023 stage 2) is what would change this.
