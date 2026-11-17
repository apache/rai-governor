# Pipelines: import and export

A pipeline is a set of agents that call each other: one entry agent and every agent it reaches through its Governor agents. A single agent is a pipeline of one. Pipelines live in git as plain files and move in and out of Governor from the home page (**Import** on the Agents card) and an agent's page (**Export pipeline**, beside Run and Deploy).

## The folder

```
pipeline.yaml              which agents, who calls whom, schemas, env
agents/<name>.py           each agent's code
agents/env/<name>.env      an agent's env vars, KEY=VALUE (optional)
```

`pipeline.yaml`:

```yaml
pipeline: asvs                    # lowercase letters, digits, - or _
entry: asvs_orchestrate           # the agent you run (optional)
agents:
  - name: asvs_orchestrate        # the name other agents' code calls it by
    code: agents/asvs_orchestrate.py
    description: Runs the ASVS audit pipeline
    input_schema: {inputText: string}
    calls: [asvs_discover, asvs_audit]
    env_file: agents/env/asvs_orchestrate.env
    env: {ASVS_PASS_CONCURRENCY: "2"}   # wins over env_file
  - name: asvs_discover
    code: agents/asvs_discover.py
```

Every path is relative to `pipeline.yaml`. Other optional fields per agent: `friendly_name`, `docstring`, `output_schema`, `tools` (MCP server URL to tool names), `data_store`, `invokable_models`, `swagger_specs`.

Env files are `KEY=VALUE` lines. `#` starts a comment at the beginning of a line or after whitespace; a quoted value keeps its `#`. Env vars are plaintext (see [API key management](api-key-management.md) for secrets), so never put a token in one.

`calls` may also name an agent that already exists in Governor and isn't in the folder, such as a shared helper; it is linked by name.

## Starting from agents you already have

Open the entry agent and click **Export pipeline**. You get `<pipeline>.zip` with the folder above: the agent, every agent it calls, their schemas and env. Unzip it over the pipeline's folder in your repo, review the diff, and commit.

## Importing

**Import** on the home page's Agents card (also **Import pipeline** on the Saved Agents page) takes either source:

- **GitHub:** repository (`owner/name`), branch, tag or commit, and the folder holding `pipeline.yaml`. A token is needed for private repos and avoids GitHub's limit of 60 requests an hour without one. It is used for that import only and never stored.
- **Folder:** pick the folder holding `pipeline.yaml` (or one above it, if it holds only one). This works in Chrome and Firefox.

Names, labels, paths and sizes are checked as listed in [Limits](security/limits.md).

**Preview** shows what each agent would become (create, update or unchanged, and what changed) and any problem that would stop the import, such as a call to an agent that doesn't exist. **Import** writes it.

**Deploy every agent** is ticked by default: the import also deploys each agent it wrote, at `/rest/<friendly name>` (with `-<label>` for a labelled copy), so the pipeline can be run, published as a workload, or called from outside right away. An agent whose deployment name is already taken by another agent is listed after the import as not deployed; the import itself still stands. Untick it to import without deploying.

## Copies and labels

There are usually two useful versions of a pipeline: the **stable** one you run, and the **new** one you're trying.

**Label this copy** is ticked by default, and the import lands in a copy under a **label**: the short commit hash for GitHub, a UTC timestamp for a folder, or one you type. Agents in the copy are stored as `<name>@<label>`, for example `asvs_audit@3194087`, and shown with an `@label` chip.

Untick it to import into the **stable** copy: the agents keep their plain names (`asvs_audit`), are deployed at `/rest/<name>`, and show a `stable` chip. Each unlabelled import updates stable in place, so its endpoints don't change. The stable import refuses an agent with a plain name that isn't already part of the pipeline's stable copy, such as one made by hand; rename or delete it first.

- Importing the same pipeline with the same label updates that copy in place: same agents, same ids, same deployments.
- A new label makes another copy alongside, for a branch or a big change, without touching the copy you're running.

Agent code doesn't change between copies. It still calls `gofannon_client.call("asvs_audit", …)`, and Governor resolves the plain name to the agent in the caller's own copy.

Deployment paths include the label too (`/rest/asvs-orchestrate-3194087`), so stable and any number of labelled copies can be deployed at once.
