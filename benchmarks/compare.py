"""Compare two captured experiments without running or discarding any jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from benchmarks.analysis import summarize


def deadlock_counts(raw: dict[str, Any], diagnostics: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Use pre-reset snapshots from distinct database lifetimes, not overlapping stage logs."""
    counts: dict[int, dict[str, Any]] = {}
    covered = 0
    for reset in raw["environment"]["stack_resets"]:
        end = reset["after_records"]
        if end <= covered:
            raise ValueError("reset boundaries must increase")
        records = raw["records"][covered:end]
        levels = {r["concurrency"] for r in records if r["kind"] in {"warmup", "load"}}
        if len(levels) != 1 or any(r["kind"] not in {"warmup", "load"} for r in records):
            raise ValueError("each reset segment must contain one load concurrency")
        level = levels.pop()
        name = f"before-reset-{end}.log"
        # Missing evidence is an error, never an assumed zero.
        reports = diagnostics["logs"][name]["postgres_deadlock_reports"]
        row = counts.setdefault(level, {"reports": 0, "snapshots": [], "jobs_including_warmup": 0})
        row["reports"] += reports
        row["snapshots"].append(name)
        row["jobs_including_warmup"] += sum(len(r.get("jobs", [])) for r in records)
        covered = end
    if any(r["kind"] in {"warmup", "load"} for r in raw["records"][covered:]):
        raise ValueError("load logs are not fully covered by reset snapshots")
    return counts


def compare(
    before: dict[str, Any],
    after: dict[str, Any],
    before_logs: dict[str, Any],
    after_logs: dict[str, Any],
) -> dict[str, Any]:
    if before["environment"]["method"] != after["environment"]["method"]:
        raise ValueError("benchmark methodology/settings differ")
    summaries = [summarize(raw["records"]) for raw in (before, after)]
    locks = [
        deadlock_counts(raw, logs) for raw, logs in ((before, before_logs), (after, after_logs))
    ]
    result: dict[str, Any] = {"method_equal": True, "scaling": [], "faults": {}}
    for old, new in zip(summaries[0]["scaling"], summaries[1]["scaling"], strict=True):
        if old["concurrency"] != new["concurrency"]:
            raise ValueError("concurrency levels differ")
        level = old["concurrency"]
        row: dict[str, Any] = {"concurrency": level}
        for label, summary, counts in (("before", old, locks[0]), ("after", new, locks[1])):
            completed = summary["completion_rate"]["successes"]
            row[label] = {
                "mean_jobs_per_second": summary["observed_window_throughput_jobs_per_second"][
                    "mean"
                ],
                "throughput_distribution": summary["observed_window_throughput_jobs_per_second"],
                "completed_jobs": completed,
                "requested_jobs": summary["jobs_requested"],
                "unfinished_jobs": summary["jobs_submitted"] - completed,
                "not_submitted_jobs": summary["jobs_requested"] - summary["jobs_submitted"],
                "latency_seconds_completed_only": summary["e2e_seconds"],
                "deadlocks_including_warmup": counts[level],
            }
        result["scaling"].append(row)
    for kind in summaries[0]["faults"]:
        result["faults"][kind] = {
            "before": summaries[0]["faults"][kind],
            "after": summaries[1]["faults"][kind],
        }
    result["changed_source_files"] = sorted(
        path
        for path, digest in before["environment"]["source_sha256"].items()
        if after["environment"]["source_sha256"].get(path) != digest
    )
    package_sets = [
        set(raw["environment"]["runtime_packages"].splitlines()) for raw in (before, after)
    ]
    result["runtime_package_changes"] = {
        "removed": sorted(package_sets[0] - package_sets[1]),
        "added": sorted(package_sets[1] - package_sets[0]),
    }
    # Redis INFO includes uptime and instance IDs: compare the version, not the entire snapshot.
    result["server_versions_equal"] = before["environment"]["postgres_version"] == after[
        "environment"
    ]["postgres_version"] and [
        line
        for line in before["environment"]["redis_version"].splitlines()
        if line.startswith("redis_version:")
    ] == [
        line
        for line in after["environment"]["redis_version"].splitlines()
        if line.startswith("redis_version:")
    ]
    result["third_party_packages_equal"] = [
        line
        for line in before["environment"]["runtime_packages"].splitlines()
        if not line.startswith("pulsehunter ")
    ] == [
        line
        for line in after["environment"]["runtime_packages"].splitlines()
        if not line.startswith("pulsehunter ")
    ]
    result["runner_source_equal"] = (
        before["environment"]["source_sha256"]["benchmarks/run.py"]
        == after["environment"]["source_sha256"]["benchmarks/run.py"]
    )
    return result


def render(result: dict[str, Any], output: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {"font.size": 10, "svg.fonttype": "none", "svg.hashsalt": "pulsehunter-comparison"}
    )
    rows = result["scaling"]
    for metric, ylabel, name in (
        ("mean_jobs_per_second", "Completed jobs / second", "throughput"),
        ("unfinished_jobs", "Unfinished measured jobs (of 80)", "unfinished"),
    ):
        fig, ax = plt.subplots(figsize=(8, 4), layout="constrained")
        for label, offset, color in (("before", -0.18, "#8a8497"), ("after", 0.18, "#6855a3")):
            positions = [i + offset for i in range(len(rows))]
            values = [r[label][metric] for r in rows]
            ax.bar(positions, values, width=0.34, label=label.title(), color=color)
            for x, value, row in zip(positions, values, rows, strict=True):
                ax.annotate(
                    f"{value:.2f}" if name == "throughput" else str(value),
                    (x, value),
                    xytext=(0, 4),
                    textcoords="offset points",
                    ha="center",
                    fontsize=9,
                )
                if name == "throughput":
                    spread = row[label]["throughput_distribution"]
                    ax.plot(
                        [x, x],
                        [spread["min"], spread["max"]],
                        color="#24202e",
                        marker="_",
                        linewidth=1,
                    )
        ax.set(
            xticks=range(len(rows)),
            xticklabels=[str(r["concurrency"]) for r in rows],
            xlabel="Celery prefork slots; 5 × 16 measured jobs per level",
            ylabel=ylabel,
            title="Simulator comparison: mean observed-window throughput"
            if name == "throughput"
            else "Unfinished jobs retained, not excluded",
        )
        ax.legend()
        ax.margins(y=0.2)
        fig.savefig(output / f"comparison-{name}.svg", metadata={"Date": None})
        fig.savefig(output / f"comparison-{name}.png", dpi=150)
        plt.close(fig)
    fig, axes = plt.subplots(1, 3, figsize=(12, 4), layout="constrained")
    for ax, quantile in zip(axes, ("p50", "p95", "p99"), strict=True):
        for label, color in (("before", "#8a8497"), ("after", "#6855a3")):
            ax.plot(
                [r["concurrency"] for r in rows],
                [r[label]["latency_seconds_completed_only"][quantile] for r in rows],
                marker="o",
                label=label.title(),
                color=color,
            )
        ax.set(
            title=quantile,
            xlabel="Celery slots",
            ylabel="Completed-job E2E seconds",
            xticks=[1, 2, 4, 8],
        )
    axes[0].legend()
    denominators = "; ".join(
        f"{row['concurrency']} slots {row['before']['completed_jobs']}/{row['before']['requested_jobs']} → {row['after']['completed_jobs']}/{row['after']['requested_jobs']}"
        for row in rows
    )
    fig.suptitle("Completed-only latency, before → after: " + denominators, fontsize=10)
    fig.savefig(output / "comparison-latency.svg", metadata={"Date": None})
    fig.savefig(output / "comparison-latency.png", dpi=150)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw_paths = [directory / "raw.json" for directory in (args.before, args.after)]
    raws = [json.loads(path.read_text(encoding="utf-8")) for path in raw_paths]
    logs = [
        json.loads((directory / "diagnostics.json").read_text(encoding="utf-8"))
        for directory in (args.before, args.after)
    ]
    result = compare(*raws, *logs)
    args.output.mkdir(parents=True, exist_ok=True)
    result["input_sha256"] = {
        label: hashlib.sha256(path.read_bytes()).hexdigest()
        for label, path in zip(("before", "after"), raw_paths, strict=True)
    }
    result["calculation_source_sha256"] = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ("analysis.py", "compare.py")
    }
    (args.output / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    render(result, args.output)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
