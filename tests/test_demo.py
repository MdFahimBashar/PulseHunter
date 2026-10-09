from __future__ import annotations

import pytest

from scripts import demo


def test_demo_refuses_a_physical_device_using_a_simulator_name() -> None:
    devices = [
        {
            "name": name,
            "id": name,
            "device_type": "simulator",
            "capabilities": {"simulated": True},
            "status": "online",
        }
        for name in ("sim-healthy", "sim-slow", "sim-unreliable")
    ]
    devices[0]["device_type"] = "windows-host"
    devices[0]["capabilities"] = {"simulated": False}
    with pytest.raises(RuntimeError, match="non-simulator"):
        demo.simulator_ids(devices)


@pytest.mark.parametrize("attempts", [1, 2])
def test_demo_checks_retry_evidence_and_prints_real_run_link(monkeypatch, capsys, attempts) -> None:
    posted = []

    def request(base_url, path, *, body=None):
        if path == "/runs":
            posted.append(body)
            return {"id": "captured-run"}
        if path.endswith("/jobs"):
            return [
                {
                    "device_name": "sim-unreliable",
                    "status": "passed",
                    "attempts": attempts,
                    "result": {"checks_passed": 1},
                    "logs": ["ok"],
                    "execution_duration": 0.4,
                    "error_message": None,
                }
            ]
        if path == "/devices":
            return [{"name": "sim-unreliable", "status": "online"}]
        return {"status": "passed"}

    monkeypatch.setattr(demo, "request_json", request)
    args = (
        "http://local",
        "smoke-id",
        {"sim-unreliable": "device-id"},
        {"sim-unreliable": ("passed", 2)},
        90,
    )
    if attempts == 1:
        with pytest.raises(RuntimeError, match="Unexpected outcome"):
            demo.run_case(*args)
    else:
        demo.run_case(*args)
        output = capsys.readouterr().out
        assert "http://local/runs/captured-run/view" in output
        assert "attempts: 2" in output and "devices released" in output
    assert posted == [{"test_suite_id": "smoke-id", "device_ids": ["device-id"]}]
