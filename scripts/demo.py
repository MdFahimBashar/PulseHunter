"""Narrate real simulator runs through the existing API; no mocked results."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from scripts.verify_e2e import request_json, wait_for_devices


def simulator_ids(devices: list[dict[str, Any]]) -> dict[str, str]:
    expected = {"sim-healthy", "sim-unreliable", "sim-slow"}
    selected = {device["name"]: device for device in devices if device["name"] in expected}
    if selected.keys() != expected:
        raise RuntimeError("The three default simulators must be registered")
    for name, device in selected.items():
        if (
            device["device_type"] != "simulator"
            or device["capabilities"].get("simulated") is not True
        ):
            raise RuntimeError(f"Refusing to send demo work to non-simulator {name}")
        if device["status"] != "online":
            raise RuntimeError(f"{name} is not available; wait for existing work to finish")
    return {name: device["id"] for name, device in selected.items()}


def run_case(
    base_url: str,
    suite_id: str,
    device_ids: dict[str, str],
    expected: dict[str, tuple[str, int]],
    timeout: float,
) -> None:
    run = request_json(
        base_url,
        "/runs",
        body={"test_suite_id": suite_id, "device_ids": [device_ids[name] for name in expected]},
    )
    run_id = run["id"]
    print(f"Run ID: {run_id}\nView: {base_url.rstrip('/')}/runs/{run_id}/view", flush=True)
    deadline = time.monotonic() + timeout
    previous: dict[str, tuple[str, int]] = {}
    while True:
        run = request_json(base_url, f"/runs/{run_id}")
        jobs = request_json(base_url, f"/runs/{run_id}/jobs")
        current = {job["device_name"]: (job["status"], job["attempts"]) for job in jobs}
        for name, state in current.items():
            if previous.get(name) != state:
                print(f"  {name}: {state[0]} (attempts: {state[1]})", flush=True)
        previous = current
        if run["status"] in {"passed", "failed", "cancelled"}:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Demo wait expired; inspect the existing run at /runs/{run_id}/view"
            )
        time.sleep(0.25)

    expected_run = (
        "passed" if all(status == "passed" for status, _ in expected.values()) else "failed"
    )
    if current != expected or run["status"] != expected_run:
        raise RuntimeError(f"Unexpected outcome: run={run['status']}, jobs={current}")
    for job in jobs:
        if job["status"] == "passed" and (not job["result"] or not job["logs"]):
            raise RuntimeError(f"Missing persisted result/logs for {job['device_name']}")
        duration = job["execution_duration"]
        duration_text = f"{duration:.2f}s" if duration is not None else "unavailable"
        reason = f"; {job['error_message']}" if job["error_message"] else ""
        print(
            f"  Result: {job['device_name']}: {job['status']}, last attempt {duration_text}{reason}"
        )
    devices = {device["name"]: device for device in request_json(base_url, "/devices")}
    if any(devices[name]["status"] != "online" for name in expected):
        raise RuntimeError("A terminal job did not release its simulator to online")
    print(f"Run {run['status'].upper()}; results persisted and devices released.\n", flush=True)


def demonstrate(base_url: str, timeout: float) -> None:
    print("PulseHunter live demo: Docker simulators, real API/queue/database.\n", flush=True)
    devices = simulator_ids(wait_for_devices(base_url, timeout))
    for name in sorted(devices):
        print(f"Registered and online: {name} [SIMULATOR]", flush=True)
    suites = request_json(base_url, "/test-suites")
    suite = next((item for item in suites if item["slug"] == "smoke"), None)
    if suite is None:
        raise RuntimeError("The seeded smoke suite is missing")
    print("\n1. Healthy execution: expect a pass on attempt 1.", flush=True)
    run_case(base_url, suite["id"], devices, {"sim-healthy": ("passed", 1)}, timeout)
    print("2. Injected HTTP 503: unreliable returns a transient error, then recovers.", flush=True)
    run_case(base_url, suite["id"], devices, {"sim-unreliable": ("passed", 2)}, timeout)
    print("3. Concurrent mixed run: healthy passes, unreliable retries, slow exhausts its budget.")
    run_case(
        base_url,
        suite["id"],
        devices,
        {
            "sim-healthy": ("passed", 1),
            "sim-unreliable": ("passed", 2),
            "sim-slow": ("timed_out", 3),
        },
        timeout,
    )
    print("Demo verified. The mixed run intentionally FAILED; timeout containment worked.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=90, help="Wait budget per run, in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        demonstrate(args.base_url, args.timeout)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
