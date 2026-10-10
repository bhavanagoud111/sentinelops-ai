# Contributing to SentinelOps

Background reading: the [README](README.md), [`docs/architecture.md`](docs/architecture.md)
and the decision records in [`docs/adr/`](docs/adr).

## The one rule that never bends
A model may analyze and recommend. It never executes infrastructure operations.
Remediation = allowlisted runbook + authenticated human approval + a policy check at
execution time. Changes that weaken this need an ADR and a security review.

## Setup
```bash
make bootstrap   # Python 3.12, dependencies and tools, all inside .cache/ in the repo
make up          # local stack: kind cluster + control plane + observability
```
Tool caches stay inside the repository (`.cache/`), and Docker resources are namespaced
under the `sentinelops` Compose project, so nothing is installed system-wide.

## Before opening a pull request
- `make check` passes: ruff, mypy `--strict`, unit, security and fixture tests, schema drift.
- If you touched the running pipeline: `make test-integration` against `make up`.
- New behaviour comes with tests, type hints, structured logging and doc updates.
- Event contracts, APIs and migrations stay backward compatible. A breaking change needs
  a new ADR in `docs/adr/` and a bumped `schema_version`.
- Evaluation numbers in docs come from a recorded run (`scripts/run_evaluation.sh`),
  with the raw records committed under `docs/evaluation/`.

## Security expectations
- No secrets, credentials or `.env` files in commits (CI runs gitleaks).
- No code path that turns model output into shell, kubectl or SQL. Runbooks are fixed
  templates; the model only fills validated parameters.
- Kubernetes permissions stay namespaced and least-privilege. Remediation defaults to
  `recommend_only`.
- Logs, traces and tool output are untrusted input and are treated as a prompt-injection
  surface.

## Layout and commands
The repository layout is described in the [README](README.md#repository-layout).
`make help` lists every target; the common ones are `make check`, `make up`,
`make scenario S=A`, `make reset-demo` and `make down`.
