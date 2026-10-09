# CI integration

The CI client follows the same API → PostgreSQL → Redis/Celery → agent path as
manual runs. It resolves a suite slug, creates a run, polls for terminal state,
and prints run ID, suite, devices, attempts, duration, and failure reasons.

Install Python 3.14 and the package from a clone:

```bash
python -m pip install .
pulsehunter-ci run --server "$PULSEHUNTER_URL" --suite smoke \
  --device-id "$PULSEHUNTER_DEVICE_ID" --wait --timeout 120
```

The example above uses POSIX shell variables. In PowerShell, use
`$env:PULSEHUNTER_URL` and `$env:PULSEHUNTER_DEVICE_ID`, or supply literal values.
Use `pulsehunter-ci run --help` for every option. No Python installation is
needed on the desktop when invoking the already packaged client with
`docker compose exec -T api pulsehunter-ci ...`.

| Exit | Meaning |
|---|---|
| 0 | With `--wait`: validation passed. Without it: submission accepted only. |
| 1 | Validation failed or was cancelled; a timed-out job makes its run fail. |
| 2 | Client, API, or run-creation error. |
| 3 | Client wait deadline expired; the server run may still be active. |

`--suite` uses a slug from `GET /test-suites`. Repeat `--device-id` to select
several devices. Omitting it reserves all fresh available devices without
API-level capability matching. For mixed physical/simulated fleets, select
compatible devices explicitly: `smoke` for simulators, `host-health` for the
Windows host. The default simulator fleet includes an intentional timeout;
choose only `sim-healthy` for a green build-gating example.

Optional `--repository`, `--commit`, `--ref`, and `--build-id` persist source
context. A commit SHA is required if any source field is supplied.
`GET /runs/{run_id}` returns that context. It is caller-supplied, not verified
by a signature or by fetching the source repository. The client does not deploy
or flash the referenced build onto the agent.

## GitHub Actions

Copy [the example workflow](../examples/github-actions-device-validation.yml)
into another repository's `.github/workflows/` directory. Configure a trusted
self-hosted Linux runner with the `pulsehunter-lab` label and repository
variables `PULSEHUNTER_URL` and `PULSEHUNTER_DEVICE_ID`. The runner must reach
the server and must not execute untrusted fork code on the lab network.
For a fixed client revision, replace the example's `@main` install reference
with a reviewed commit SHA.

PulseHunter's own CI uses an isolated local stack. It does not depend on an
external PulseHunter server. The MVP has no authentication or TLS; see the
[security policy](../SECURITY.md) before configuring network access.
