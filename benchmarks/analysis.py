"""Statistics over recorded samples, without silently discarding failed trials."""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from statistics import mean
from typing import Any


def percentile(values: list[float], quantile: float) -> float | None:
    """Linear interpolation (Hyndman-Fan type 7); empty samples stay unavailable."""
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    if not values:
        return None
    if any(not math.isfinite(value) for value in values):
        raise ValueError("samples must be finite")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "mean": mean(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        **{f"p{q}": percentile(values, q / 100) for q in (50, 95, 99)},
    }


def rate(successes: int, trials: int) -> dict[str, Any]:
    """Wilson 95% interval makes a small all-success sample's uncertainty explicit."""
    if not 0 <= successes <= trials:
        raise ValueError("invalid success count")
    if not trials:
        return {"successes": 0, "trials": 0, "rate": None, "wilson_95": None}
    z = 1.959963984540054
    proportion = successes / trials
    denominator = 1 + z * z / trials
    center = (proportion + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials**2))
    return {
        "successes": successes,
        "trials": trials,
        "rate": proportion,
        "wilson_95": [max(0, center - margin / denominator), min(1, center + margin / denominator)],
    }


def elapsed(start: str, finish: str) -> float:
    value = (datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds()
    if value < 0:
        raise ValueError("negative duration: inspect recorded clocks")
    return value


def window_throughput(trial: dict[str, Any]) -> float | None:
    jobs = trial.get("jobs", [])
    if not jobs:
        return None
    if trial.get("terminal"):
        return trial.get("throughput")
    if not all(job.get("observed_at") for job in jobs):
        return None
    span = elapsed(min(job["created_at"] for job in jobs), max(job["observed_at"] for job in jobs))
    return sum(job.get("completed_at") is not None for job in jobs) / span if span > 0 else None


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    scaling = []
    for concurrency in sorted({r["concurrency"] for r in records if r["kind"] == "load"}):
        trials = [r for r in records if r["kind"] == "load" and r["concurrency"] == concurrency]
        jobs = [job for trial in trials for job in trial.get("jobs", [])]
        terminal = [j for j in jobs if j["completed_at"] is not None]
        scaling.append(
            {
                "concurrency": concurrency,
                "trials": len(trials),
                "jobs_requested": sum(t["expected_jobs"] for t in trials),
                "jobs_submitted": len(jobs),
                "jobs_recorded": len(jobs),
                "completion_rate": rate(len(terminal), sum(t["expected_jobs"] for t in trials)),
                "pass_rate": rate(
                    sum(j["status"] == "passed" for j in jobs),
                    sum(t["expected_jobs"] for t in trials),
                ),
                "throughput_jobs_per_second": distribution(
                    [t["throughput"] for t in trials if t.get("throughput") is not None]
                ),
                "observed_window_throughput_jobs_per_second": distribution(
                    [value for trial in trials if (value := window_throughput(trial)) is not None]
                ),
                "e2e_seconds": distribution(
                    [elapsed(j["created_at"], j["completed_at"]) for j in terminal]
                ),
                "queue_seconds": distribution(
                    [elapsed(j["created_at"], j["started_at"]) for j in jobs if j["started_at"]]
                ),
                "attempt_counts": dict(Counter(str(j["attempts"]) for j in jobs)),
                "admission_conflicts": sum(len(t.get("admission_conflicts", [])) for t in trials),
                "admission_attempts": sum(t.get("admission_attempts", 0) for t in trials),
                "admission_seconds": distribution(
                    [t["admission_seconds"] for t in trials if "admission_seconds" in t]
                ),
                "client_wall_seconds": distribution(
                    [t["client_wall_seconds"] for t in trials if "client_wall_seconds" in t]
                ),
            }
        )
    faults = {}
    for kind in sorted({r["kind"] for r in records} - {"load", "warmup"}):
        trials = [r for r in records if r["kind"] == kind]
        faults[kind] = {
            "expected_behavior_rate": rate(
                sum(bool(t["expected_behavior"]) for t in trials), len(trials)
            ),
            "pass_rate": rate(sum(t.get("final_status") == "passed" for t in trials), len(trials))
            if kind != "offline"
            else None,
            "terminal_rate": rate(sum(bool(t.get("terminal")) for t in trials), len(trials))
            if kind != "offline"
            else None,
            "latency_seconds": distribution(
                [t["latency_seconds"] for t in trials if t.get("latency_seconds") is not None]
            ),
            "attempt_counts": dict(
                Counter(str(t.get("attempts")) for t in trials if "attempts" in t)
            ),
            "observed_retry_backoff_seconds": sorted(
                {
                    observation["error_details"]["retry_in_seconds"]
                    for trial in trials
                    for observation in trial.get("observations", [])
                    if observation.get("error_details")
                    and "retry_in_seconds" in observation["error_details"]
                }
            ),
        }
    return {"scaling": scaling, "faults": faults}
