# API and simulator reference

The running server publishes [Swagger](http://127.0.0.1:8000/docs) and
[OpenAPI JSON](http://127.0.0.1:8000/openapi.json). Those schemas describe
request bodies and responses; default host port is 8000.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | PostgreSQL and Redis connectivity |
| `GET` | `/devices` | Fleet metadata and heartbeat-derived state |
| `GET` | `/devices/{device_id}` | One device |
| `GET` | `/test-suites` | Predefined validation suites |
| `GET` | `/runs` | Recent runs and aggregate counts |
| `POST` | `/runs` | Reserve devices, persist jobs, and queue a run |
| `GET` | `/runs/{run_id}` | Aggregate state and source context |
| `GET` | `/runs/{run_id}/jobs` | Results, logs, errors, attempts, and timings |
| `POST` | `/internal/devices/register` | Register or refresh an agent by name |
| `POST` | `/internal/devices/{device_id}/heartbeat` | Update liveness and capabilities |

`POST /runs` takes a `test_suite_id`, optional non-empty `device_ids`, and
optional validated `source` context. Without IDs it selects all fresh available
devices. API-level suite compatibility matching is not implemented; select
appropriate devices explicitly. Unavailable requested devices yield `409`.
The dashboard applies its own advertised-suite filter and sends explicit IDs.

## PowerShell example: one healthy simulator

```powershell
$server = 'http://127.0.0.1:8000'
$suite = Invoke-RestMethod "$server/test-suites" | Where-Object slug -eq 'smoke'
$device = Invoke-RestMethod "$server/devices" | Where-Object name -eq 'sim-healthy'
$body = @{ test_suite_id = $suite.id; device_ids = @($device.id) } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri "$server/runs" -ContentType 'application/json' -Body $body
Invoke-RestMethod "$server/runs/$($run.id)"
Invoke-RestMethod "$server/runs/$($run.id)/jobs"
```

Submission is asynchronous. Use the [CI client](ci-integration.md) to wait and
gate a build, or open `/runs/{run_id}/view` in the dashboard.

## Default simulation modes

- **Healthy:** waits 0.4 seconds and returns passing checks with logs.
- **Unreliable:** deterministically returns HTTP 503 once per job, then passes
  after the scheduled retry. The injected fault is part of the agent's configured
  outcome script, not random network loss.
- **Slow:** executes for 20 seconds while worker HTTP requests time out after
  3 seconds; three bounded attempts end in `timed_out` and the run fails.

Within a live agent process, accepted executions are cached by job UUID.
Duplicate requests attach to the same task; changed payloads for that UUID are
rejected. Work continues if the HTTP caller times out. Restarting the agent
loses this cache. These simulators are not physical hardware measurements.
