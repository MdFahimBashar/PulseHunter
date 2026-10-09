# Live demonstration and recording guide

This walkthrough uses the real Docker/PostgreSQL/Redis/Celery stack and its
three existing simulators. It submits real runs and leaves their results in
the selected development database. It does not contact a physical host or
alter application settings. No demonstration video is currently checked in.

## Repeat the demonstration

From the repository root, with Docker running:

```bash
docker compose up --build --detach --wait
python -m scripts.demo --timeout 90
```

The narration needs Python 3.14, uses only its standard library, and requires
the simulators to be idle. For another API port, add
`--base-url http://127.0.0.1:18081`. It verifies these steps:

1. Wait for `sim-healthy`, `sim-unreliable`, and `sim-slow` to register online;
   reject any selected device that does not identify itself as a simulator.
2. Submit a healthy-only `smoke` run: pass on attempt 1.
3. Submit an unreliable-only run: the configured agent injects HTTP 503, then
   the existing worker retry path recovers on attempt 2.
4. Submit all three concurrently: healthy passes, unreliable recovers, and
   slow reaches `timed_out` on attempt 3. The aggregate is deliberately failed.
5. Check persisted passing results/logs and that terminal jobs released devices.

It prints links to the actual saved runs. Polling may miss fast intermediate
states; persisted attempt counts and terminal results determine verification.
Printed duration is the last execution attempt, not end-to-end latency. Exit
0 means all expected demo behaviors occurred, including intentional failure;
the [CI client](ci-integration.md) instead exits nonzero for that failed run.
A demo wait timeout does not cancel jobs on the server.

## Recording checklist (about 90 seconds)

1. Open `http://127.0.0.1:8000/` at 1440 × 1000 browser-content pixels. Start
   screen recording with your preferred recorder; hide unrelated windows.
2. Show the three **SIMULATOR** badges, fresh heartbeats, and online state.
   Explain that these are independent HTTP agent processes.
3. Select **Simulated Device Smoke Test**, then **Choose devices** and only
   `sim-healthy`. Launch from the dashboard and show its passing result.
4. Return to the dashboard. Run `python -m scripts.demo` in a visible terminal.
   Point out the unreliable job's second attempt and open its printed run link.
5. Open the mixed-run link. Show two passing devices, one timeout, attempts,
   and **Technical details** with saved logs/error context. The failure is expected.
6. Return to the dashboard and show released devices online. Explain that
   PostgreSQL keeps the results while Redis only delivers tasks.
7. Show the README comparison chart and identify the 53 unfinished baseline
   jobs. Explain the lock correction and the simulator-only measurement scope.
8. Stop recording. Review it for visible secrets, unrelated browser tabs, and
   unreadable text before sharing. Do not label this a physical-laptop demo.

Optional physical footage must use a live Windows host and `host-health` via
the [Windows setup guide](windows-host-agent.md). Label it separately. For
worker-kill and missed-heartbeat injection, use the isolated
[benchmark harness](benchmarks.md); the short demo does not kill containers.

## Screenshots in this repository

- [Dashboard](images/dashboard.png): the actual fleet and saved runs.
- [Run detail](images/run-detail.png): actual healthy/retry/timeout outcomes.

Both captures were taken on 2026-10-09 from an isolated local Compose instance
of the existing UI. They contain only simulator data generated through the API;
no device rows, results, or HTML were fabricated. Browser screenshots exclude
browser chrome and are losslessly stored as PNG. The dashboard uses a
1440 × 1000 viewport with a full-page capture; run detail uses the same viewport.
No physical-machine identifiers or production database contents are shown.

To refresh them, run the demo, open the dashboard and the final three-device
run, select the smoke suite for the dashboard capture, and use your browser's
full-page screenshot command. Keep the viewport at 1440 × 1000, device scale
1, and capture the default collapsed Technical details. Save only the two PNGs;
keep recording files and transient demo logs outside the repository.

## Isolated capture setup (PowerShell)

Use an unused project name and port when you already have personal development
data. These variables apply to this terminal only:

```powershell
$env:COMPOSE_PROJECT_NAME='pulsehunter-showcase'
$env:COMPOSE_ENV_FILES='.env.example'
$env:PULSEHUNTER_API_BIND='127.0.0.1'
$env:PULSEHUNTER_API_PORT='18081'
$env:PULSEHUNTER_IMAGE_TAG='showcase'
docker compose up --build --detach --wait
python -m scripts.demo --base-url http://127.0.0.1:18081
python scripts/verify_e2e.py --base-url http://127.0.0.1:18081 --timeout 90
python scripts/verify_ci.py --base-url http://127.0.0.1:18081 --timeout 90
```

Start from a terminal without other `PULSEHUNTER_*` or `POSTGRES_*` overrides.
The project has its own named volumes; this does not reuse the default
`pulsehunter` database. After recording, `docker compose down` preserves these
demo runs. Only if this is the disposable showcase project you created, use
the explicit cleanup below to delete its demo data:

```powershell
docker compose --env-file .env.example -p pulsehunter-showcase down --volumes
Remove-Item Env:COMPOSE_PROJECT_NAME, Env:COMPOSE_ENV_FILES, Env:PULSEHUNTER_API_BIND, Env:PULSEHUNTER_API_PORT, Env:PULSEHUNTER_IMAGE_TAG
```
