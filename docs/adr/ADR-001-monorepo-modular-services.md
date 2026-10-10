# ADR-001: Monorepo with modular Python packages

**Status:** Accepted · 2026-10-09

## Context
SentinelOps has many components (gateway, correlator, investigator, executor, demo
workloads) that share contracts. It is built and maintained by one developer, and
must stay reproducible from a clean clone.

## Decision
One repository. Python code is a **uv workspace**: each component is its own package with
its own `pyproject.toml` (`packages/contracts`, `packages/common`,
`demo/microservices/*`, later `services/*`), sharing one lockfile (`uv.lock`) and one dev
virtualenv. Components start as importable modules and become separate processes or
images only when there is a runtime reason. All Python images build from one
parameterised Dockerfile (`--build-arg PACKAGE=...`).

## Alternatives
- *Polyrepo*: real isolation, but contract changes need coordinated releases — too much
  overhead at this scale.
- *Single package*: simplest, but blurs boundaries (e.g. demo fault code leaking into the
  control plane) and makes per-service images heavier.

## Consequences
+ Atomic changes across contracts and consumers; one `make check`.
+ Package boundaries are enforced by dependencies declared per package.
− A single lockfile means all packages share dependency versions.
− Workspace-aware Docker builds copy more context than a single-package build.
