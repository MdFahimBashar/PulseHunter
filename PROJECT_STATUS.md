# Project Status

## Current state

The end-to-end MVP is implemented with three standalone simulators and a
physical Windows host agent. PulseHunter reserves networked devices, dispatches
concurrent jobs through Redis/Celery, persists results in PostgreSQL, recovers
from transient failures, and exposes runs through REST, a dashboard, and a
build-gating CI client. The Windows laptop workflow has been verified manually
end to end on real hardware.

Target runtime: Python 3.14. Latest automated verification: 2026-10-09.

## Implemented architecture

- FastAPI control plane and server-rendered Jinja dashboard.
- PostgreSQL source of truth with SQLAlchemy 2 and Alembic migration
  `20260917_0001`, plus additive run-source migration `20260924_0002`.
- Redis as Celery broker only; no Celery result backend.
- Celery worker with concurrency 4 and Celery Beat maintenance scheduler.
- Database-backed claims, attempt ownership, leases, reconciliation, retries,
  exponential backoff, timeouts, terminal persistence, aggregation, and release.
- Separate healthy, slow, and unreliable HTTP device-agent processes.
- Optional non-containerized Windows host agent using the same HTTP contract.
- Docker Compose topology, automated tests, and GitHub Actions quality plus
  Docker end-to-end jobs, including CI-client pass/fail gating.

## Implemented features

- meaningful `/health` for PostgreSQL and Redis
- device registration, idempotent re-registration, heartbeats, and stale cutoff
- device list/detail with online, offline, and busy state
- predefined idempotently seeded simulator and Windows-host suites
- run creation/list/detail and per-job result APIs
- all-available or explicit-device reservation
- passing results, validation failures, transient failures, and timeouts
- bounded attempts and capped exponential backoff
- persisted structured result, logs, error details, and duration
- automatic durable-job and expired-worker-lease reconciliation
- dashboard fleet metrics, compatible-device selection, run creation, recent
  runs, readable physical-host results, and raw technical detail
- generated Swagger/OpenAPI at `/docs`
- `pulsehunter-ci` run creation, polling, exit codes, per-device summaries, and
  optional caller-supplied source-commit metadata stored with each run
- `pulsehunter-host-agent` registration, heartbeat, job-id idempotency, and
  bounded real host inventory, memory, disk, network, and battery checks

## Verification status

Physical acceptance was verified on 2026-09-24; automated verification was
repeated on 2026-10-09:

- On a physical Windows laptop, the agent registered as `windows-host` with
  `simulated=false`, sent heartbeats over the LAN, and accepted an HTTP job from
  the desktop worker. Run `8f990ccc-c1c7-4ad2-9c63-4af627e19758` passed
  `host-health` on attempt 1. Real CPU, memory, storage, network, battery,
  uptime, and system results were persisted; memory and storage integrity
  checks passed. The dashboard rendered the saved result, and heartbeat-driven
  online/offline/reconnect behavior was observed.
- Locally, Python 3.14.5 passed Ruff, Ruff format (76 files), Mypy (46 source
  and benchmark files), and 66 pytest tests with seven live-service cases skipped.
  A disposable Python 3.14.8 container passed all 73 tests against real
  PostgreSQL and Redis. Six PostgreSQL concurrency cases also passed ten
  repetitions (60/60).
- Compose config, image build, and isolated startup passed. PostgreSQL, Redis,
  API, and simulators were healthy; Celery answered ping and Beat executed
  maintenance. Alembic reported no schema drift; repeated suite seeding
  succeeded. Only disposable test volumes were removed; production data stayed
  untouched.
- The Docker simulator E2E run passed healthy on attempt 1 and unreliable on
  attempt 2; slow timed out on attempt 3, so the aggregate failed as designed.
  The CI client returned exit 0 for healthy and exit 1 for slow. `/health`,
  Swagger/OpenAPI, the dashboard, and a saved simulator run-detail page loaded.

The preserved simulator baseline exposed a PostgreSQL foreign-key lock-upgrade
cycle in run aggregation. A deterministic PostgreSQL regression reproduced it;
aggregation now uses FOR NO KEY UPDATE without weakening job/device ownership.
The identical 5 × 16-job rerun completed 80/80 jobs at each of 1/2/4/8 slots,
with zero observed deadlocks versus 249 baseline load/warm-up reports. All four
repeated fault scenarios retained expected behavior (5/5 each); worker-kill
recovery averaged 9.27 s versus 8.16 s before. See the
[investigation, comparison and limitations](docs/concurrency-deadlocks.md).
Baseline samples were not overwritten. This remains a simulator benchmark, not
a physical-hardware capacity or production reliability claim.

GitHub Actions covers automated tests and Docker simulator flows; it does not
physically exercise the Windows laptop. Pull-request workflow checks provide
release verification separately from the recorded benchmark measurements.

## Public presentation verification

The 2026-10-09 presentation update adds authentic simulator dashboard/run
screenshots, a narrated API demonstration, and focused setup/API/CI guides.
The deadlock benchmark captures and their original verification counts above
are unchanged. With three additional demo checks, the complete suite passed
**76 tests** against isolated Docker PostgreSQL/Redis; local pytest passed
69 with seven live-service skips. Ruff, formatting, and Mypy passed. The real
demo, simulator E2E, packaged CI success/failure paths, migration drift check,
and worker ping passed. Documentation explains how to record a demo; no video
or new physical-hardware test is claimed.

## Known issues and limitations

- Trusted-local-development security model: no users, agent auth, TLS, or RBAC.
- Agent URL registration creates an SSRF risk on an untrusted deployment.
- Agent execution idempotency is in memory and is lost on restart.
- No cancellation, priority queue, capability matching, artifact storage,
  streaming logs, metrics dashboards, or cloud deployment.
- CI tests the host-agent contract, not the physical laptop.
- The slow simulator intentionally makes an all-device run fail with a timeout.
- Exactly one Celery Beat process should run.

## Commands

```bash
docker compose up --build --detach --wait
python scripts/verify_e2e.py --timeout 90
```

```bash
ruff check .
ruff format --check .
mypy src
pytest -W error
docker compose run --rm migrate alembic check
docker compose run --rm migrate python -m pulsehunter.db.seed
```

## Future work

Authentication, API-level capability-aware scheduling, and additional agent
integrations such as Raspberry Pi or microcontroller gateways are not yet
implemented.
