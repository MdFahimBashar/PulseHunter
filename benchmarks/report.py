"""Generate reproducible charts from existing raw measurements; never run jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from benchmarks.analysis import summarize


def diagnostics(directory: Path) -> dict[str, Any]:
    """Per-file counts only: reset/stage logs can overlap and must not be summed."""
    result = {}
    for path in sorted(directory.glob("*.log")):
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        result[path.name] = {
            "postgres_deadlock_reports": len(re.findall(r"ERROR:\s+deadlock detected", text)),
            "worker_deadlock_task_failures": sum(
                "raised unexpected:" in line and "DeadlockDetected" in line
                for line in text.splitlines()
            ),
        }
    return {
        "note": "Counts are per log snapshot and may overlap; includes warm-up.",
        "logs": result,
    }


def render(raw: dict[str, Any], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    summary = summarize(raw["records"])
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    plt.rcParams.update(
        {"font.size": 10, "svg.fonttype": "none", "svg.hashsalt": "pulsehunter-benchmarks"}
    )
    scaling = summary["scaling"]
    positions = list(range(len(scaling)))
    labels = [str(row["concurrency"]) for row in scaling]
    fig, ax = plt.subplots(figsize=(7, 4), layout="constrained")
    means = [
        row["observed_window_throughput_jobs_per_second"]["mean"]
        if row["observed_window_throughput_jobs_per_second"]["mean"] is not None
        else float("nan")
        for row in scaling
    ]
    ax.bar(positions, means, color="#6855a3", width=0.6)
    for index, row in enumerate(scaling):
        trials = row["observed_window_throughput_jobs_per_second"]
        if trials["n"]:
            ax.plot(
                [index, index],
                [trials["min"], trials["max"]],
                color="#24202e",
                marker="_",
                linewidth=1,
            )
        ax.annotate(
            f"{means[index]:.2f}\nn={trials['n']}/{row['trials']} batches"
            if trials["n"]
            else f"Unavailable\nn=0/{row['trials']} batches",
            (index, trials["max"] if trials["n"] else 0),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=9,
        )
    ax.set(
        xticks=positions,
        xticklabels=labels,
        xlabel="Celery prefork concurrency (one worker container)",
        ylabel="Completed jobs / second",
        title="Observed-window throughput (bars: mean; lines: min–max)",
    )
    ax.set_ylim(0, max([m for m in means if m == m], default=1) * 1.35)
    fig.savefig(output / "throughput.svg", metadata={"Date": None})
    fig.savefig(output / "throughput.png", dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(7, 4), layout="constrained")
    for q, color in [(50, "#6855a3"), (95, "#bf863c"), (99, "#ad4f61")]:
        ax.plot(
            positions,
            [row["e2e_seconds"][f"p{q}"] for row in scaling],
            marker="o",
            label=f"p{q}",
            color=color,
        )
    ax.set(
        xticks=positions,
        xticklabels=[
            f"{label}\n{row['e2e_seconds']['n']}/{row['jobs_requested']} completed"
            for label, row in zip(labels, scaling, strict=True)
        ],
        xlabel="Celery prefork concurrency",
        ylabel="Creation → persisted terminal result (seconds)",
        title="End-to-end latency (completed jobs only; simulated work)",
    )
    ax.legend()
    fig.savefig(output / "latency.svg", metadata={"Date": None})
    fig.savefig(output / "latency.png", dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(8, 4), layout="constrained")
    kinds = ["offline", "worker_loss", "transient", "timeout"]
    titles = [
        "Last heartbeat\n→ offline observed",
        "Kill confirmed\n→ terminal result",
        "Transient error\ncreation → pass",
        "Slow job\ncreation → timeout",
    ]
    for index, kind in enumerate(kinds):
        samples = [
            r["latency_seconds"]
            for r in raw["records"]
            if r["kind"] == kind and r.get("latency_seconds") is not None
        ]
        ax.scatter(
            [index + 0.03 * (i - (len(samples) - 1) / 2) for i in range(len(samples))],
            samples,
            color="#6855a3",
            s=25,
            alpha=0.8,
        )
    ax.set(
        xticks=list(range(4)),
        xticklabels=[
            f"{title}\nn={summary['faults'].get(kind, {}).get('latency_seconds', {}).get('n', 0)} trials"
            for title, kind in zip(titles, kinds, strict=True)
        ],
        ylabel="Seconds",
        title="Fault-injection timing: each point is one measured trial",
    )
    ax.set_ylim(bottom=0)
    fig.savefig(output / "fault-latency.svg", metadata={"Date": None})
    fig.savefig(output / "fault-latency.png", dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(8, 4), layout="constrained")
    for index, kind in enumerate(kinds):
        observed = summary["faults"].get(kind, {}).get("expected_behavior_rate")
        if observed and observed["trials"]:
            low, high = observed["wilson_95"]
            measured = observed["rate"]
            ax.errorbar(
                index,
                measured * 100,
                yerr=[[max(0, 100 * (measured - low))], [max(0, 100 * (high - measured))]],
                fmt="o",
                capsize=5,
                color="#6855a3",
            )
            ax.annotate(
                f"{observed['successes']}/{observed['trials']}",
                (index, measured * 100),
                xytext=(0, 8),
                textcoords="offset points",
                ha="center",
            )
    ax.set(
        xticks=list(range(4)),
        xticklabels=[
            "Offline detection",
            "Worker-loss recovery",
            "Transient recovery",
            "Timeout containment",
        ],
        ylabel="Observed expected-behavior rate (%)",
        title="Repeated fault trials (Wilson 95% intervals; not an SLA)",
        ylim=(0, 115),
    )
    fig.savefig(output / "fault-rates.svg", metadata={"Date": None})
    fig.savefig(output / "fault-rates.png", dpi=150)
    plt.close(fig)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="raw.json from benchmarks.run")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(json.loads(args.input.read_text(encoding="utf-8")), args.output)
    (args.output / "diagnostics.json").write_text(
        json.dumps(diagnostics(args.input.parent), indent=2), encoding="utf-8"
    )
    manifest = {
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "matplotlib_version": matplotlib.__version__,
        "calculation_source_sha256": {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("analysis.py", "report.py")
        },
    }
    (args.output / "report-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
