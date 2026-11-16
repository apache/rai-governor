# Contributing to Governor

Thanks for helping. Governor is an Apache Software Foundation project:
contributions are accepted under the [Apache License 2.0](LICENSE), and
everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Find something to work on

- Issues labelled
  [`good first issue`](https://github.com/apache/rai-governor/issues?q=is%3Aissue%20state%3Aopen%20label%3A%22good%20first%20issue%22)
  are small and self-contained: a good way to learn the workflow from fork to
  merged pull request.
- For a bug, check the [open issues](https://github.com/apache/rai-governor/issues)
  first, then open one with steps to reproduce.
- For anything bigger than a small fix, open an issue describing the change
  before writing it, so the approach can be agreed first.

## Set up

The [developer quickstart](docs/developers-quickstart.md) covers cloning the
repository, setting up the backend and frontend, and running the stack locally
with Docker Compose. [Architecture](docs/architecture.md) and the
[API reference](docs/api.md) explain how the pieces fit together.

## Before you open a pull request

Run the checks CI runs on pull requests:

```bash
# Frontend: lint, then tests with the coverage floor
cd webapp/packages/webui
pnpm lint
pnpm test:coverage

# Backend: unit tests with the coverage floor
cd webapp/packages/api/user-service
python -m pytest tests/unit
```

Depending on what you changed:

- **`requirements.txt`**: regenerate the lock, or CI fails:
  ```bash
  cd webapp/packages/api/user-service
  uv pip compile requirements.txt --universal --python-version 3.12 \
    --no-header -o requirements.lock
  ```
- **`docs/` or `website/`**: build the docs site:
  ```bash
  cd website && npm ci && npm run build
  ```
- **Anything touching the running stack**: `./run-all-tests.sh` from the
  repository root runs every suite, including integration and end-to-end
  tests, against the Docker Compose stack.

The [testing guide](docs/testing/contributing.md) explains what tests a change
needs and where they go. Coverage floors live in
`webapp/packages/webui/vitest.config.ts` and
`webapp/packages/api/user-service/pytest.ini`; a change should not lower them.

## Pull request requirements

- Code changes need:
  - Passing CI
  - Approval from a reviewer
  - Updated documentation when behaviour or an API changes
- Documentation changes need a successful docs site build. Review is welcome
  but not required.
- Changes that mix both must meet both sets of requirements.

## Dependencies

Dependabot proposes updates weekly and only for releases at least seven days
old; see `.github/dependabot.yml`. The `overrides` in `website/package.json` and
`pnpm.overrides` in `webapp/package.json` pin transitive dependencies for
security fixes and are updated by hand.
