# SentinelOps AI

AI-assisted SRE platform: detect incidents from telemetry, investigate them with bounded
agents that cite evidence, and remediate only through allowlisted, human-approved
runbooks.

> **Core invariant:** an LLM can analyze and recommend, but never independently executes
> infrastructure operations.

Blueprint: [`SentinelOps_AI_System_Design.md`](SentinelOps_AI_System_Design.md) ·
Architecture status: [`docs/architecture.md`](docs/architecture.md) ·
ADRs: [`docs/adr`](docs/adr) · Threat model: [`docs/threat-model.md`](docs/threat-model.md)

## Status
| Phase | Scope | State |
|---|---|---|
| 0 | Repo, contracts, CI, ADRs, threat model | built — `make check` green locally; CI not yet run |
| 1 | Demo services, OTel, Prometheus/Loki/Tempo/Grafana, alerts | built — exit gate verified locally 2026-10-09 (metric, log, trace, alert); scenario B alert also verified |
| 2 | Gateway, outbox, Redpanda, correlator, Postgres, incidents API | built — exit gate verified locally: dedup, consumer replay, restart+full-topic replay, real alert → incident in 110 s |
| 3 | Bounded LangGraph investigation, evidence citations, critic, rules baseline + optional Claude | built — fixtures pass in mock mode; live investigation + crash-orphan recovery verified |
| 4 | kind workload, approvals, policy engine, scoped executor, verifier, audit | built — all five scenarios verified live on Kubernetes (A, B, D, E resolved after approval; C escalated) |
| 5 | React dashboard, SSE, Grafana deep links, experiments | built — 5 Playwright e2e tests pass against the live stack |
| 6–7 | Hardening/evaluation, Helm/Terraform | in progress |

## Quick start
Prerequisites: [uv](https://docs.astral.sh/uv/), Docker (Desktop/OrbStack/Colima), `make`, `curl`.
Python 3.12 is installed by uv into `.cache/` inside the repo.

```bash
make bootstrap        # toolchain + deps (repo-local), creates .env
make check            # lint, mypy --strict, unit tests, schema drift
make up               # start the stack (~650 MB RAM measured under demo load)
make inject-fault     # scenario A: checkout 5xx via bad config
make verify-telemetry # Phase 1 exit gate: metric + log + trace + alert observed
make reset-demo       # clear faults, restart demo services
make down             # stop (make destroy also removes volumes)
```

| UI | URL |
|---|---|
| Grafana (Service Overview) | http://127.0.0.1:13000 |
| Prometheus | http://127.0.0.1:19090 |
| Alertmanager | http://127.0.0.1:19093 |
| Checkout API docs | http://127.0.0.1:18081/docs |

## Demo scenarios
| | Scenario | Inject | Alert |
|---|---|---|---|
| A | checkout 5xx from bad config | `make inject-fault [P=0.8]` | `CheckoutHigh5xxRate` |
| B | inventory latency from slow dependency | `make inject-latency [DELAY=1200]` | `InventoryHighLatencyP95` |
| C–E | payment timeouts, CrashLoopBackOff, saturation | — | planned |

## Repository layout
See [docs/architecture.md](docs/architecture.md).
