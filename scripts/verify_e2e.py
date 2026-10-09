from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any


def request_json(
    base_url: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def wait_for_devices(base_url: str, timeout: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            devices: list[dict[str, Any]] = request_json(base_url, "/devices")
            names = {device["name"] for device in devices if device["status"] == "online"}
            if {"sim-healthy", "sim-slow", "sim-unreliable"} <= names:
                return devices
        except (OSError, urllib.error.HTTPError):  # fmt: skip
            pass
        time.sleep(1)
    raise RuntimeError("Three online simulated devices did not register before the deadline")


def verify(base_url: str, timeout: float) -> dict[str, Any]:
    devices = wait_for_devices(base_url, timeout)
    suites = request_json(base_url, "/test-suites")
    suite = next((item for item in suites if item["slug"] == "smoke"), None)
    if suite is None:
        raise RuntimeError("The seeded smoke test suite is not available")

    simulator_names = {"sim-healthy", "sim-slow", "sim-unreliable"}
    simulator_ids = [device["id"] for device in devices if device["name"] in simulator_names]
    run = request_json(
        base_url,
        "/runs",
        body={"test_suite_id": suite["id"], "device_ids": simulator_ids},
    )
    run_id = run["id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = request_json(base_url, f"/runs/{run_id}")
        if run["status"] in {"passed", "failed", "cancelled"}:
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"Run {run_id} did not become terminal before the deadline")

    jobs = request_json(base_url, f"/runs/{run_id}/jobs")
    by_device = {job["device_name"]: job for job in jobs}
    expected_names = simulator_names
    if set(by_device) != expected_names:
        raise AssertionError(f"Expected jobs for {expected_names}, received {set(by_device)}")
    assert run["status"] == "failed", run
    assert by_device["sim-healthy"]["status"] == "passed", by_device
    assert by_device["sim-healthy"]["attempts"] == 1, by_device
    assert by_device["sim-unreliable"]["status"] == "passed", by_device
    assert by_device["sim-unreliable"]["attempts"] == 2, by_device
    assert by_device["sim-slow"]["status"] == "timed_out", by_device
    assert by_device["sim-slow"]["attempts"] == 3, by_device
    assert by_device["sim-healthy"]["result"], by_device
    assert by_device["sim-healthy"]["logs"], by_device
    starts = [datetime.fromisoformat(job["started_at"].replace("Z", "+00:00")) for job in jobs]
    assert (max(starts) - min(starts)).total_seconds() < 2, starts

    final_devices = request_json(base_url, "/devices")
    final_statuses = {device["name"]: device["status"] for device in final_devices}
    assert all(final_statuses[name] == "online" for name in expected_names), final_statuses
    return {
        "run": run,
        "jobs": jobs,
        "devices_before": devices,
        "devices_after": final_devices,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the complete local PulseHunter workflow")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    try:
        result = verify(args.base_url, args.timeout)
    except Exception as exc:
        print(f"E2E verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
