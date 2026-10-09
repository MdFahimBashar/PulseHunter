# PostgreSQL run-aggregation deadlock investigation

## Confirmed cause, not a throughput-only inference

The original benchmark and a synchronized regression both raised PostgreSQL
SQLSTATE `40P01` while locking a `test_runs` tuple. The isolated regression log
at `2026-10-09 04:05:05.041 UTC` contained:

```text
Process 501 waits for ShareLock on transaction 760; blocked by process 509.
Process 509 waits for ShareLock on transaction 759; blocked by process 501.
Both statements: SELECT ... FROM <isolated-schema>.test_runs WHERE id = $1::UUID FOR UPDATE
CONTEXT: while locking tuple (0,1) in relation "test_runs"
```

The test paused both real `JobExecutor._claim` transactions immediately before
their run-lock queries, after ORM autoflush had already executed their job
updates. `pgrowlocks` in this disposable database reported the same parent run
held by both PIDs in `For Key Share` mode. The captured SQL sequence was:

1. Lock the worker's own `test_jobs` row with `FOR UPDATE`.
2. Update job status/attempts; lazy device loading triggers an autoflush.
3. Update lease/owner fields; run selection triggers another autoflush.
4. Request `FOR UPDATE` on the shared parent `test_runs` row.

The child-row updates in these transactions had already obtained foreign-key
key-share protection on the parent. Each stronger run lock conflicted with the
other transaction's existing key-share lock. Neither could commit and release
its lock: this was a **lock-upgrade cycle**, even though both workers followed
the same explicit job-before-run order. The transaction `ShareLock` waits in
the server log are not evidence that the application requested SQL `FOR SHARE`.

The regression failed before the correction with `DeadlockDetected`. The same
barrier remains in the passing test, so success does not rely on avoiding the
overlap through sleep timing. Optional `pgrowlocks` observation requires the
extension only in the diagnostic database; normal tests and production do not.

## Small correction and correctness argument

Only `recompute_run_status` changes in the execution code: run aggregation now
uses `FOR NO KEY UPDATE`, compiled by SQLAlchemy's
`with_for_update(key_share=True)` with the default `read=False`.

| Existing/desired lock on parent run | Another worker's FK KEY SHARE | Another run-status writer |
|---|---|---|
| Previously: FOR UPDATE | Conflicts | Excluded |
| Now: FOR NO KEY UPDATE | Compatible | Still excluded |

This matches aggregation's actual responsibility: it changes status and timing,
not the referenced run ID. PostgreSQL documents these compatibility rules in
[explicit row locking, Table 13.3](https://www.postgresql.org/docs/17/explicit-locking.html).
Sibling job transitions can proceed, but the short aggregation sections for
one run remain mutually exclusive. Under the existing READ COMMITTED isolation,
the status query after acquiring that lock sees committed sibling updates plus
its own transaction's autoflushed update. The final finisher can therefore
persist the terminal aggregate. Terminal run states remain immutable.

No global worker mutex, queue serialization, timeout increase, schema change,
or new automatic transaction retry was added. HTTP execution was already
outside the claim/finish transactions and stays there. Reordering every worker
to lock the parent before its child would require matching maintenance paths
and would broaden the change unnecessarily. Relaxing the run lock to a plain
read would lose the writer exclusion required for correct aggregation.

## Transaction-path audit

| Path | Locks and transaction boundary | Preserved protection |
|---|---|---|
| Run creation | Available devices, ordered by name, FOR UPDATE SKIP LOCKED; insert a new run and its jobs; commit before queue publication | Busy devices cannot be reserved by a second run |
| Claim | Own job FOR UPDATE; status, attempt, owner and lease; parent run NO KEY UPDATE; commit before HTTP | Only a queued/due-retrying job can be claimed |
| Retry | Own job FOR UPDATE; verify running owner; bounded retry state; parent run NO KEY UPDATE; commit before publication | Stale worker cannot schedule another owner's attempt |
| Finish | Own job FOR UPDATE; verify owner; persist result/error/log; device UPDATE; parent run NO KEY UPDATE; commit | Device release and durable result are atomic |
| Reconciliation | Expired jobs FOR UPDATE SKIP LOCKED; inspect lease/attempt bound; retry or fail/release; aggregate affected runs; commit before publication | Locked jobs are skipped, expired ownership is invalidated |
| Heartbeat/registration | Device FOR UPDATE; inspect active jobs without locking those jobs; commit | Heartbeats do not overwrite BUSY devices |
| Offline sweep | Conditional device UPDATE for stale heartbeats; commit without acquiring job/run locks | Database heartbeat age remains authoritative |

Worker leases are fields on the locked job, not separate lease-table locks.
Reconciliation can process multiple jobs/runs in a transaction; that scope and
selection order were not changed by this narrowly evidenced correction. The
new parent mode resolves its sibling FK upgrade conflict through the same
central helper. This audit/rerun does not claim immunity to every possible future
multi-run transaction cycle. A future batching, run-key mutation, deletion, or
multi-device job feature must explicitly review ordering again.

## Regression coverage and safe reproduction

`tests/test_postgres_concurrency.py` uses independent PostgreSQL connections and
a unique temporary schema per test. It tests synchronized sibling claims,
completions, and retries; duplicate delivery during a blocked device call;
stale-owner writes; immutable terminal results; and two runs contending for
one device. A release-review test also observes a second run-status writer
waiting in PostgreSQL until the first commits, and reservation is checked after
the winning transaction commits. No production schema/table is modified by
these fixtures. Existing
domain/lease-recovery tests and Docker fault trials remain in place.

From the repository root (PowerShell):

```powershell
docker compose -p pulsehunter-deadlock-diagnosis -f benchmarks/compose.diagnosis.yaml up --detach --wait
$env:PULSEHUNTER_DATABASE_URL='postgresql+psycopg://pulsehunter:pulsehunter@127.0.0.1:15433/pulsehunter'
$env:PULSEHUNTER_RUN_SERVICE_TESTS='1'
python -m pytest tests/test_postgres_concurrency.py -W error
```

Optionally, while the diagnosis stack and environment above are still active,
inspect the held locks by installing `pgrowlocks` there:

```powershell
docker compose -p pulsehunter-deadlock-diagnosis -f benchmarks/compose.diagnosis.yaml exec -T postgres psql -U pulsehunter -d pulsehunter -c 'CREATE EXTENSION IF NOT EXISTS pgrowlocks'
$env:PULSEHUNTER_CAPTURE_LOCKS='1'
python -m pytest tests/test_postgres_concurrency.py -k claim -s -W error
Remove-Item Env:PULSEHUNTER_CAPTURE_LOCKS
```

Then clean up only the named disposable diagnosis project:

```powershell
Remove-Item Env:PULSEHUNTER_DATABASE_URL, Env:PULSEHUNTER_RUN_SERVICE_TESTS
docker compose -p pulsehunter-deadlock-diagnosis -f benchmarks/compose.diagnosis.yaml down --volumes
```

For the original failure on a disposable checkout, change only that helper's
lock back to `with_for_update()` and run the claim regression. Do not run this
deliberately broken variant against an application database. The new tests are
enabled by the existing GitHub Actions live-service test environment without
requiring a new workflow or the optional extension.

## Identical benchmark rerun — 2026-10-09 (Eastern)

The after experiment began at `2026-10-09T04:11:00.781312+00:00`. Both captures
use five measured batches of 16 jobs at each of 1/2/4/8 prefork slots, one
excluded/retained warm-up per level, a 0.4-second healthy simulator workload,
the same 30-second observation deadline, and five trials per fault. The runner
source hash and recorded methodology/settings are identical. Both experiments
ran on the same Windows 10 / Ryzen 5 3600 workstation and Docker WSL2 resources
described in [the original methodology](benchmarks.md). No physical-host agent
was contacted; these are **simulated jobs, not production traffic**.

Third-party runtime wheels and PostgreSQL/Redis versions match. The application
wheel changes as expected; the only changed execution source is
`services/runs.py`. Baseline raw provenance records earlier analysis/report
hashes because those tools were refined after its capture. The current tools
match the archived baseline report manifest, and recomputing the original raw
data produces its saved summary exactly. Neither original samples nor charts
were rewritten. The baseline raw SHA-256 remains
`2c950c788a8495ef84ef98020ee5b724dcc02780cecf300a8c149056f300a10a`.

Before → after; latency is in seconds and conditional on observed completion:

| Slots | Mean observed jobs/s | Completed jobs | Unfinished jobs | E2E p50 | E2E p95 | E2E p99 |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 2.199 → 2.270 | 80/80 → 80/80 | 0 → 0 | 4.027 → 3.820 | 7.196 → 6.993 | 7.318 → 7.089 |
| 2 | 4.097 → 4.623 | 80/80 → 80/80 | 0 → 0 | 2.336 → 1.960 | 4.121 → 3.455 | 4.548 → 3.476 |
| 4 | 2.662 → 9.032 | 80/80 → 80/80 | 0 → 0 | 5.005 → 1.102 | 10.514 → 1.768 | 10.931 → 1.783 |
| 8 | 0.201 → 16.566 | 27/80 → 80/80 | 53 → 0 | 12.845 → 0.685 | 24.633 → 0.968 | 24.961 → 0.980 |

All after batches (20/20 measured, plus four warm-ups) completed. All 320
measured jobs passed on attempt one; none was excluded as unfinished or rejected.
The baseline's eight-slot percentiles use **27 completions**, not all 80 jobs;
53 right-censored jobs remain explicitly reported. Do not compare that tail as
though the baseline also fully completed. Charts show observed-window mean and
min–max, not confidence bounds. The four-slot measured throughput improvement
is approximately 3.39×; small single-slot timing differences should not be
attributed confidently to the lock fix on an interactive workstation.

### Deadlock evidence without double-counting

Counts below include warm-up (96 submitted jobs per level), not just the 80
headline jobs. They are server deadlock **reports**, not distinct failed jobs or
failure probabilities: a rolled-back claim can be republished repeatedly.

| Slots | Before reports | After reports | Before non-overlapping pre-reset log boundaries |
|---|---:|---:|---|
| 1 | 0 | 0 | 6 |
| 2 | 2 | 0 | 12 |
| 4 | 38 | 0 | 18 |
| 8 | 209 | 0 | 19, 20, 21, 23, 24 |
| Total | 249 | 0 | Each snapshot ends a separate database lifetime |

`benchmarks.compare` derives these boundaries from recorded stack resets and
uses only `before-reset-N.log` diagnostic counts. It does not also add overlapping
`concurrency-N.log` snapshots. Missing diagnostic evidence is an error, never an
assumed zero. Fault-stage logs also contained zero deadlock reports in both
experiments. Zero observed reports is not a proof of universal deadlock freedom.

### Repeated faults

| Fault (five trials each, before and after) | Mean seconds before → after | Expected behavior before → after | After attempts |
|---|---:|---:|---:|
| Missed heartbeat/offline detection | 11.986 → 11.869 | 5/5 → 5/5 detected and reconnected | N/A |
| Transient HTTP 503 | 1.493 → 1.473 | 5/5 → 5/5 passed/released | 2 |
| Slow-agent timeout | 12.128 → 12.082 | 5/5 → 5/5 timed out/released | 3 |
| SIGKILL worker | 8.164 → 9.274 | 5/5 → 5/5 passed/released after lease expiry | 2 |

Transient retries retained the 1-second scheduled backoff; timeout trials
retained 1/2-second exponential backoffs and the three-attempt bound. Every
worker-kill trial captured `WorkerLeaseExpired` and completed after its original
lease expired. Worker-loss latency **regressed by about 1.11 seconds** in this
sample. Reconciliation tick phase and Docker restart scheduling affect this
measurement; neither was separately profiled, so the cause of this difference
is unconfirmed. Do not describe the correction as improving every fault metric.

Each expected-behavior rate remains 5/5 with Wilson 95% bounds of approximately
56.6–100%, not a production reliability guarantee. Timeout validation passes
remain 0/5 by design: containment is not recovery. Full per-trial observations,
min/max, percentiles, rates, and attempts are in the portable captures.

### Evidence and charts

- [Unmodified baseline capture](benchmarks/baseline-2026-10-08/raw.json)
- [After capture](benchmarks/after-deadlock-fix-2026-10-09/raw.json),
  [summary](benchmarks/after-deadlock-fix-2026-10-09/summary.json),
  [jobs](benchmarks/after-deadlock-fix-2026-10-09/jobs.csv), and
  [diagnostics](benchmarks/after-deadlock-fix-2026-10-09/diagnostics.json)
- [Structured comparison with input hashes](benchmarks/comparison-2026-10-09/comparison.json)

![Before/after observed simulator throughput](benchmarks/comparison-2026-10-09/comparison-throughput.png)

![Before/after completed-only latency with denominators](benchmarks/comparison-2026-10-09/comparison-latency.png)

![Unfinished jobs preserved in the comparison](benchmarks/comparison-2026-10-09/comparison-unfinished.png)

### Reproduction and verification

Run from the repository root after installing `.[dev,benchmark]`, using a new
output folder and no existing `pulsehunter-benchmark` stack:

```powershell
python -m benchmarks.run --output .benchmark-results/my-after --trials 5 --fault-trials 5 --fleet 16 --concurrency 1 2 4 8
python -m benchmarks.report .benchmark-results/my-after/raw.json --output .benchmark-results/my-after
python -m benchmarks.compare docs/benchmarks/baseline-2026-10-08 .benchmark-results/my-after --output .benchmark-results/my-comparison
ruff check .
ruff format --check .
mypy src benchmarks
pytest -W error
docker compose config --quiet
```

The report is generated into the capture folder here so `compare` finds both
`raw.json` and `diagnostics.json`. The runner normally deletes only its own
disposable stack/volumes after saving results. Production volumes were never
mounted, migrated, reset, or queried in this investigation. The diagnosis
database likewise used a separate project and disposable schema/volume.

Verified locally: Ruff, format check (75 files), Mypy (46 source files), and
61 pytest passes with six live-service cases skipped. Against the rebuilt
Python 3.14.8 Docker image and actual isolated PostgreSQL/Redis, **all 67 tests
passed**, including those six service cases. The five PostgreSQL concurrency
cases additionally passed ten repetitions (50/50). Docker build, Compose
validation/startup, fresh migrations, Alembic no-drift check, repeated seeding,
existing simulator E2E, and packaged CI-client healthy-success/slow-failure
gating passed. Health, dashboard, run detail, Swagger, and OpenAPI returned
successfully. Celery answered `inspect ping`; Beat continued scheduled liveness
and reconciliation. This benchmark-time verification was local; pull-request
workflow checks provide separate release verification. The complete repeated
benchmark returned **exit 0**.

Release review additionally passed all **73 tests** against Docker-backed
PostgreSQL/Redis (66 local passes, seven service-only skips), including the
explicit run-writer serialization test. All six PostgreSQL cases additionally
passed ten release-review repetitions (60/60). The benchmark preflight now also
rejects reserved resource names without Compose labels, preventing accidental
reuse or deletion of pre-existing volumes/networks. Two focused tests cover this
safety guard. It does not change the workloads or historical measurements;
the runner hashes cited above describe the two recorded experiments, before
this preflight-only hardening.
Three archive-evidence tests verify capture SHA-256 values, recompute summaries
and comparison results, and retain the 53 unfinished baseline jobs. Archive
attributes disable text normalization to preserve the original measured bytes
on Windows and Linux; generated evidence is collapsed in GitHub diffs rather
than omitted. Temporary outputs, configs, logs, and caches remain ignored.

### Remaining engineering limitations

This establishes removal of the observed FK lock-upgrade cycle, not a universal
database-concurrency proof or production capacity claim. Five short batches
cannot establish a stable p99/SLA; batch jobs share resources and are correlated.
Fixed ascending concurrency order, background workstation load, and unprofiled
CPU/database waits remain confounders. Keep per-run writer exclusion and
re-review ordering if introducing multi-run batches or parent-key mutations.
Next performance work should profile waits/resource utilization, reconciliation
publication traffic, and longer randomized trials before tuning. Agent-restart
idempotency, broker-loss recovery, and physical-host performance were not tested
by this simulated benchmark; existing physical acceptance evidence is separate.
