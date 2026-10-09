from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from benchmarks.analysis import elapsed, percentile, rate, summarize, window_throughput
from benchmarks.compare import compare, deadlock_counts
from benchmarks.run import PROJECT, Harness, make_config, parser


def test_percentiles_and_missing_samples_are_explicit() -> None:
    assert percentile([], 0.99) is None
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([1, 2, 3, 4], 0.95) == pytest.approx(3.85)
    assert percentile([7], 0.99) == 7
    with pytest.raises(ValueError):
        percentile([float("nan")], 0.5)
    with pytest.raises(ValueError):
        elapsed("2026-10-08T00:00:02+00:00", "2026-10-08T00:00:01+00:00")


def test_small_success_samples_do_not_claim_certain_reliability() -> None:
    measured = rate(5, 5)
    assert measured["rate"] == 1
    assert measured["wilson_95"][0] < 0.6
    assert rate(0, 0)["rate"] is None
    with pytest.raises(ValueError):
        rate(6, 5)


def test_summary_keeps_incomplete_jobs_and_failed_fault_trials() -> None:
    job = {
        "status": "passed",
        "attempts": 1,
        "created_at": "2026-10-08T00:00:00+00:00",
        "started_at": "2026-10-08T00:00:01+00:00",
        "completed_at": "2026-10-08T00:00:02+00:00",
    }
    pending = {**job, "status": "running", "completed_at": None}
    records = [
        {"kind": "warmup", "concurrency": 1, "expected_jobs": 99, "jobs": [job]},
        {
            "kind": "load",
            "concurrency": 1,
            "expected_jobs": 2,
            "jobs": [job, pending],
            "throughput": None,
        },
        {
            "kind": "worker_loss",
            "expected_behavior": True,
            "terminal": True,
            "final_status": "passed",
            "latency_seconds": 9,
            "attempts": 2,
        },
        {
            "kind": "worker_loss",
            "expected_behavior": False,
            "terminal": False,
            "final_status": "deadline_exceeded",
            "latency_seconds": None,
        },
    ]
    summary = summarize(records)
    scaling = summary["scaling"][0]
    assert scaling["jobs_submitted"] == 2
    assert scaling["completion_rate"]["rate"] == 0.5
    assert scaling["e2e_seconds"]["n"] == 1
    assert scaling["throughput_jobs_per_second"]["n"] == 0
    assert summary["faults"]["worker_loss"]["expected_behavior_rate"]["rate"] == 0.5
    assert summary["faults"]["worker_loss"]["latency_seconds"]["n"] == 1


def base_config() -> dict:
    app = {
        "image": "pulsehunter:local",
        "build": {"context": "."},
        "environment": {},
        "networks": {"default": None},
    }
    agent = {**app, "environment": {"PULSEHUNTER_AGENT_NAME": "sim-healthy"}}
    return {
        "services": {
            "api": copy.deepcopy(app),
            "worker": copy.deepcopy(app),
            "agent-healthy": agent,
            "postgres": {
                "image": "postgres",
                "volumes": [{"type": "volume", "source": "postgres-data", "target": "/data"}],
            },
        },
        "volumes": {"postgres-data": {"name": "pulsehunter_postgres-data"}},
    }


def test_compose_generation_isolates_resources_and_preserves_input() -> None:
    base = base_config()
    before = copy.deepcopy(base)
    config = make_config(base, fleet=16, api_port=18080, db_port=15432)
    assert base == before
    assert config["name"] == PROJECT
    assert config["volumes"]["postgres-data"]["name"].startswith(PROJECT)
    assert config["services"]["api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert (
        config["services"]["bench-healthy-16"]["environment"]["PULSEHUNTER_AGENT_PUBLIC_URL"]
        == "http://bench-healthy-16:9000"
    )
    assert config["services"]["worker"]["image"] == "pulsehunter:benchmark"
    assert "ports" not in config["services"]["worker"]


def test_generation_rejects_host_and_external_volume_mounts() -> None:
    base = base_config()
    base["services"]["api"]["volumes"] = [{"type": "bind", "source": "private", "target": "/data"}]
    with pytest.raises(ValueError, match="must not mount"):
        make_config(base, fleet=1, api_port=18080, db_port=15432)


def test_preflight_failure_cannot_clean_up_someone_elses_stack(tmp_path: Path) -> None:
    config = tmp_path / "compose.generated.json"
    config.write_text(json.dumps({"name": PROJECT}), encoding="utf-8")
    args = parser().parse_args(["--output", str(tmp_path)])
    harness = Harness(args)
    with pytest.raises(ValueError, match="empty"):
        harness.prepare()
    harness.close()  # No Docker command: this invocation never owned a stack.


def test_run_admission_records_conflicts_without_retrying_server_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(parser().parse_args(["--output", str(tmp_path)]))
    harness.client.close()
    harness.device_ids = {"sim": "id"}
    monkeypatch.setattr(harness, "wait_devices", lambda names: None)
    monkeypatch.setattr("benchmarks.run.time.sleep", lambda seconds: None)
    responses = [
        httpx.Response(409, json={"detail": "unavailable"}),
        httpx.Response(202, json={"id": "run"}),
    ]
    harness.client = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(lambda request: responses.pop(0))
    )
    record: dict = {}
    assert harness.start_run(["sim"], record) == "run"
    assert record["admission_attempts"] == 2
    assert record["admission_conflicts"] == [{"detail": "unavailable"}]
    harness.client.close()
    harness.client = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )
    with pytest.raises(httpx.HTTPStatusError):
        harness.start_run(["sim"], record)
    assert record["admission_attempts"] == 1
    harness.close()


def test_rejected_load_trial_remains_in_denominator() -> None:
    summary = summarize([{"kind": "load", "concurrency": 1, "expected_jobs": 16}])
    assert summary["scaling"][0]["completion_rate"]["rate"] == 0
    assert summary["scaling"][0]["jobs_submitted"] == 0
    assert summary["scaling"][0]["jobs_requested"] == 16
    assert summary["scaling"][0]["throughput_jobs_per_second"]["n"] == 0


def test_stack_reset_requires_ownership(tmp_path: Path) -> None:
    harness = Harness(parser().parse_args(["--output", str(tmp_path)]))
    with pytest.raises(RuntimeError, match="does not own"):
        harness.reset_stack("not our stack")
    harness.close()


def test_censored_window_throughput_counts_only_observed_completions() -> None:
    pending = {
        "created_at": "2026-10-08T00:00:00+00:00",
        "observed_at": "2026-10-08T00:00:30+00:00",
        "completed_at": None,
    }
    assert window_throughput({"jobs": [pending]}) == 0
    completed = {**pending, "completed_at": "2026-10-08T00:00:01+00:00"}
    assert window_throughput({"jobs": [pending, completed]}) == pytest.approx(1 / 30)
    assert window_throughput({"jobs": []}) is None


def test_deadlock_counts_use_disjoint_reset_snapshots_not_overlapping_logs() -> None:
    raw = {
        "environment": {"stack_resets": [{"after_records": 1}, {"after_records": 2}]},
        "records": [
            {"kind": "warmup", "concurrency": 8, "jobs": [1]},
            {"kind": "load", "concurrency": 8, "jobs": [2]},
        ],
    }
    diagnostics = {
        "logs": {
            "before-reset-1.log": {"postgres_deadlock_reports": 3},
            "before-reset-2.log": {"postgres_deadlock_reports": 4},
            "concurrency-8.log": {"postgres_deadlock_reports": 4},
            "services.log": {"postgres_deadlock_reports": 100},
        }
    }
    counts = deadlock_counts(raw, diagnostics)
    assert counts[8]["reports"] == 7
    assert counts[8]["jobs_including_warmup"] == 2
    del diagnostics["logs"]["before-reset-2.log"]
    with pytest.raises(KeyError):
        deadlock_counts(raw, diagnostics)


@pytest.mark.parametrize(
    "resource,name", [("volume", f"{PROJECT}_postgres-data"), ("network", f"{PROJECT}_default")]
)
def test_preflight_rejects_reserved_names_without_compose_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: str, name: str
) -> None:
    calls = []

    def fake_command(args, **kwargs):
        calls.append(args)
        return name if args[:3] == ["docker", resource, "ls"] and "--format" in args else ""

    monkeypatch.setattr("benchmarks.run.command", fake_command)
    harness = Harness(parser().parse_args(["--output", str(tmp_path)]))
    with pytest.raises(ValueError, match="reserved benchmark resource"):
        harness.prepare()
    assert not harness.owns_stack and not harness.owns_output
    harness.close()
    assert not any("compose" in call for call in calls)


def test_comparison_rejects_changed_methodology() -> None:
    with pytest.raises(ValueError, match="methodology"):
        compare(
            {"environment": {"method": {"fleet": 16}}},
            {"environment": {"method": {"fleet": 8}}},
            {},
            {},
        )
