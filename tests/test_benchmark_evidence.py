"""Release evidence must survive Git checkout without hash or statistical drift."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.analysis import summarize
from benchmarks.compare import compare

ARCHIVES = Path(__file__).resolve().parents[1] / "docs" / "benchmarks"


@pytest.mark.parametrize("name", ["baseline-2026-10-08", "after-deadlock-fix-2026-10-09"])
def test_archived_capture_hash_and_summary_remain_reproducible(name: str) -> None:
    archive = ARCHIVES / name
    raw = (archive / "raw.json").read_bytes()
    manifest = json.loads((archive / "report-manifest.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == manifest["input_sha256"]
    capture = json.loads(raw)
    assert summarize(capture["records"]) == json.loads(
        (archive / "summary.json").read_text(encoding="utf-8")
    )


def test_archived_comparison_keeps_failed_baseline_samples_and_matches_inputs() -> None:
    archives = [
        ARCHIVES / name for name in ("baseline-2026-10-08", "after-deadlock-fix-2026-10-09")
    ]
    raws = [json.loads((archive / "raw.json").read_bytes()) for archive in archives]
    logs = [
        json.loads((archive / "diagnostics.json").read_text(encoding="utf-8"))
        for archive in archives
    ]
    recorded = json.loads(
        (ARCHIVES / "comparison-2026-10-09" / "comparison.json").read_text(encoding="utf-8")
    )
    recomputed = compare(*raws, *logs)
    assert all(recorded[key] == value for key, value in recomputed.items())
    assert sum(row["before"]["unfinished_jobs"] for row in recorded["scaling"]) == 53
    for label, archive in zip(("before", "after"), archives, strict=True):
        assert (
            recorded["input_sha256"][label]
            == hashlib.sha256((archive / "raw.json").read_bytes()).hexdigest()
        )
