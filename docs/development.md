# Setup and development

## Start a fresh clone

Install Docker with Linux-container support and Docker Compose. Docker Desktop
is the supported Windows development setup. From a terminal:

```bash
git clone https://github.com/MdFahimBashar/PulseHunter.git
cd PulseHunter
docker compose up --build --detach --wait
```

No `.env` file is required: `compose.yaml` has local defaults. Startup builds
the Python package, starts PostgreSQL/Redis, applies Alembic migrations, seeds
both suites, then starts the API, worker, Beat, and three simulators. Agents
register over HTTP; allow a few seconds after startup for them to appear.

- [Dashboard](http://127.0.0.1:8000/)
- [Swagger](http://127.0.0.1:8000/docs) and [OpenAPI JSON](http://127.0.0.1:8000/openapi.json)
- [Health](http://127.0.0.1:8000/health): PostgreSQL and Redis connectivity

The API binds to `127.0.0.1:8000`; PostgreSQL, Redis, and simulator ports are
internal. `/health` does not certify that a worker or every agent is alive.
Check the fleet in the dashboard and verify worker connectivity separately:

```bash
docker compose ps --all
docker compose exec -T worker celery -A pulsehunter.tasks.celery_app:celery_app inspect ping
docker compose logs --tail 50 beat worker
```

The migration container exiting successfully is expected. Worker/Beat containers
do not have health probes; inspect ping and a real demo run verify execution.

## Shutdown and configuration

```bash
docker compose down
```

This preserves PostgreSQL/Redis volumes and saved runs. Add `--volumes` only
when intentionally deleting that project's development data. Never reset
volumes to solve a routine connection problem.

For configuration, copy `.env.example` to `.env` (`Copy-Item .env.example .env`
in PowerShell, `cp .env.example .env` on Linux/macOS). Review existing files
before copying. Shell variables override `.env`; old database URLs or a LAN
bind override can change startup behavior. Keep database credentials and URLs
consistent. Existing volumes retain the credentials used when initialized.

For a port conflict, change `PULSEHUNTER_API_PORT` in `.env` and use that port
in browser/demo commands. Keep `PULSEHUNTER_API_BIND=127.0.0.1` for local use.
Trusted-LAN Windows-agent testing has separate
[host, firewall, and startup instructions](windows-host-agent.md).

On Windows, Docker must finish starting before Compose can run. If Docker
reports a WSL VM/resource error, resolve Docker/WSL first; changing PulseHunter
or deleting its database is not a repair for the engine.

## Python development

Python 3.14 is the supported runtime. Create and activate a virtual environment:

```bash
python -m venv .venv
```

PowerShell: `.venv\Scripts\Activate.ps1`.
Linux/macOS: `source .venv/bin/activate`.

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev,benchmark]"
ruff check .
ruff format --check .
mypy src benchmarks
pytest -W error
```

Default pytest uses isolated SQLite fixtures for most domain/API tests and
skips seven live-service cases. SQLite does not establish PostgreSQL locking
correctness. The [CI workflow](../.github/workflows/ci.yml) enables all cases
against fresh PostgreSQL/Redis services, and a second job builds the full stack.
To reproduce the six PostgreSQL concurrency cases locally, use the disposable
database and exact commands in the
[concurrency guide](concurrency-deadlocks.md#regression-coverage-and-safe-reproduction).

With the default Compose stack running:

```bash
python scripts/verify_e2e.py --timeout 90
python scripts/verify_ci.py --timeout 90
docker compose run --rm migrate alembic check
docker compose run --rm migrate python -m pulsehunter.db.seed
docker compose run --rm migrate python -m pulsehunter.db.seed
```

The E2E verifier creates a mixed simulator run and checks concurrency, results,
attempts, timeout containment, and device release. The CI verifier exercises
the packaged client: healthy exits 0; slow exits 1. Run them sequentially with
idle simulators. Both scripts return nonzero if expectations are violated.
Seeding is idempotent; it does not delete existing runs.

For a separate Compose project, consistently supply the same project, env file,
and port to startup and verification. `verify_ci.py` invokes Compose internally,
so set `COMPOSE_PROJECT_NAME` and `COMPOSE_ENV_FILES` in that shell too. Its
`--base-url` selects the host API; the packaged client runs inside the selected
API container. The [demo guide](demo.md) includes a concrete isolated example.

## Find the implementation

| Location | Responsibility |
|---|---|
| `src/pulsehunter/main.py`, `web.py`, `templates/`, `static/` | FastAPI routes, dashboard, and result presentation |
| `src/pulsehunter/services/` | Reservations, job ownership, aggregation, HTTP calls, reconciliation |
| `src/pulsehunter/tasks/` | Celery execution tasks and Beat schedule |
| `src/pulsehunter/agent/`, `host_agent/` | Separate simulator and Windows-host agents |
| `src/pulsehunter/ci.py` | Generic HTTP CI client and exit codes |
| `alembic/`, `tests/` | Schema migrations and automated verification |
| `benchmarks/`, `docs/benchmarks/` | Isolated harness and archived measured evidence |

Benchmark tools run from the repository root; they are not installed as part
of the application package. See [benchmark reproduction](benchmarks.md).
