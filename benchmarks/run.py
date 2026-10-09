"""Reproduce load and failure experiments against isolated, unmodified app services."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil
import psycopg
from psycopg.rows import dict_row

from benchmarks.analysis import distribution, elapsed, summarize

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "pulsehunter-benchmark"
TERMINAL = {"passed", "failed", "timed_out"}


def command(args: list[str], *, timeout: float = 180, capture: bool = True) -> str:
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PULSEHUNTER_", "POSTGRES_", "COMPOSE_"))
    }
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        timeout=timeout,
        check=True,
    )
    return result.stdout if capture else ""


def make_config(base: dict[str, Any], *, fleet: int, api_port: int, db_port: int) -> dict[str, Any]:
    """Copy the resolved example configuration; never attach production resources."""
    config = copy.deepcopy(base)
    config["name"] = PROJECT
    config["networks"] = {"default": {"name": f"{PROJECT}_default"}}
    config["volumes"] = {
        name: {"name": f"{PROJECT}_{name}"} for name in ("postgres-data", "redis-data")
    }
    services = config["services"]
    for service in services.values():
        service.pop("container_name", None)
        service.pop("profiles", None)
        service["restart"] = "no"
        service["networks"] = {"default": None}
        service.pop("ports", None)
        if "build" in service:
            service["image"] = "pulsehunter:benchmark"
        for volume in service.get("volumes", []):
            if volume["type"] != "volume" or volume["source"] not in config["volumes"]:
                raise ValueError("benchmark must not mount host files or external data volumes")
    services["api"]["ports"] = [
        {"target": 8000, "published": str(api_port), "host_ip": "127.0.0.1", "protocol": "tcp"}
    ]
    services["postgres"]["ports"] = [
        {"target": 5432, "published": str(db_port), "host_ip": "127.0.0.1", "protocol": "tcp"}
    ]
    healthy = copy.deepcopy(services["agent-healthy"])
    for index in range(fleet):
        name = f"bench-healthy-{index + 1:02d}"
        agent = copy.deepcopy(healthy)
        agent["environment"]["PULSEHUNTER_AGENT_NAME"] = name
        agent["environment"]["PULSEHUNTER_AGENT_PUBLIC_URL"] = f"http://{name}:9000"
        services[name] = agent
    recovery = copy.deepcopy(healthy)
    recovery["environment"].update(
        {
            "PULSEHUNTER_AGENT_NAME": "bench-recovery",
            "PULSEHUNTER_AGENT_PUBLIC_URL": "http://bench-recovery:9000",
            "PULSEHUNTER_AGENT_EXECUTION_SECONDS": "2",
        }
    )
    services["bench-recovery"] = recovery
    config["services"] = services
    return config


class Harness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.output).resolve()
        self.config_path = self.out / "compose.generated.json"
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{args.api_port}", timeout=10)
        self.db: psycopg.Connection[dict[str, Any]] | None = None
        self.records: list[dict[str, Any]] = []
        self.environment: dict[str, Any] = {}
        self.device_ids: dict[str, str] = {}
        self.suite_id = ""
        self.owns_stack = False
        self.owns_output = False
        self.heartbeat_timeout_seconds = 10.0

    def compose(self, *args: str, capture: bool = True, timeout: float = 180) -> str:
        return command(
            ["docker", "compose", "-p", PROJECT, "-f", str(self.config_path), *args],
            capture=capture,
            timeout=timeout,
        )

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        assert self.db is not None
        return self.db.execute(query, params).fetchall()

    def prepare(self) -> None:
        if self.out.exists() and any(self.out.iterdir()):
            raise ValueError("output directory must be empty; use a new path for each experiment")
        if self.args.api_port == 8000 or self.args.db_port == 5432:
            raise ValueError("use dedicated benchmark ports, not the production defaults")
        # Detect stale benchmark containers before changing any state.
        existing = command(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]
        )
        if existing.strip():
            raise ValueError("benchmark project already exists; use the documented cleanup command")
        for resource in ("volume", "network"):
            reserved = (
                {f"{PROJECT}_postgres-data", f"{PROJECT}_redis-data"}
                if resource == "volume"
                else {f"{PROJECT}_default"}
            )
            names = command(["docker", resource, "ls", "--format", "{{.Name}}"])
            if reserved.intersection(names.splitlines()):
                raise ValueError(
                    "reserved benchmark resource names already exist; refusing to reuse or delete them"
                )
            existing = command(
                [
                    "docker",
                    resource,
                    "ls",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={PROJECT}",
                ]
            )
            if existing.strip():
                raise ValueError(
                    "old benchmark resources exist; clean up this isolated project first"
                )
        self.out.mkdir(parents=True, exist_ok=True)
        self.owns_output = True
        base = json.loads(
            command(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    ".env.example",
                    "-f",
                    "compose.yaml",
                    "config",
                    "--format",
                    "json",
                ]
            )
        )
        config = make_config(
            base, fleet=self.args.fleet, api_port=self.args.api_port, db_port=self.args.db_port
        )
        self.heartbeat_timeout_seconds = float(
            config["services"]["worker"]["environment"]["PULSEHUNTER_HEARTBEAT_TIMEOUT_SECONDS"]
        )
        self.config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self.compose("config", "--quiet")
        self.owns_stack = True
        self.compose("build", "api", timeout=600, capture=False)
        self.compose(
            "up", "--detach", "--wait", "--wait-timeout", "180", timeout=240, capture=False
        )
        self.connect_db()
        self.environment = {
            "measured_at_utc": datetime.now(UTC).isoformat(),
            "git_sha": command(
                ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"]
            ).strip(),
            "git_status": command(
                ["git", "-c", f"safe.directory={ROOT.as_posix()}", "status", "--short"]
            ),
            "host_os": platform.platform(),
            "python": sys.version,
            "cpu_logical": psutil.cpu_count(),
            "cpu_physical": psutil.cpu_count(logical=False),
            "host_memory_bytes": psutil.virtual_memory().total,
            "host_memory_available_bytes": psutil.virtual_memory().available,
            "docker": json.loads(command(["docker", "info", "--format", "{{json .}}"])),
            "compose_version": command(["docker", "compose", "version"]).strip(),
            "runtime_versions": json.loads(
                self.compose(
                    "exec",
                    "-T",
                    "api",
                    "python",
                    "-c",
                    "import importlib.metadata as m,json,platform; print(json.dumps({'python':platform.python_version(),**{n:m.version(n) for n in ['celery','fastapi','psycopg','SQLAlchemy','redis','uvicorn']}}))",
                )
            ),
            "runtime_packages": self.compose("exec", "-T", "api", "python", "-m", "pip", "freeze"),
            "harness_packages": "\n".join(
                "pulsehunter (editable repository checkout)"
                if line.startswith("-e ") or line.startswith("pulsehunter @ file:")
                else line
                for line in command([sys.executable, "-m", "pip", "freeze"]).splitlines()
            ),
            "source_sha256": {
                path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for directory in (ROOT / "src", ROOT / "benchmarks", ROOT / "alembic")
                for path in sorted(directory.rglob("*"))
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix in {".py", ".html", ".css", ".js"}
            },
            "postgres_version": self.sql("SELECT version() AS version")[0]["version"],
            "redis_version": self.compose("exec", "-T", "redis", "redis-cli", "INFO", "server"),
            "method": {
                "fleet": self.args.fleet,
                "trials": self.args.trials,
                "fault_trials": self.args.fault_trials,
                "poll_seconds": self.args.poll_interval,
                "warmup_runs_per_concurrency": 1,
                "concurrency": self.args.concurrency,
                "suite": "smoke",
                "simulated": True,
                "physical_hardware_trials": 0,
                "settings": config["services"]["worker"]["environment"],
                "healthy_execution_seconds": 0.4,
                "recovery_execution_seconds": 2,
                "restart_policy": "no (harness explicitly restarts after injected faults)",
                "load_observation_deadline_seconds": self.args.load_timeout,
                "isolation": "fresh stack per concurrency; fresh stack after an incomplete batch; fresh stack before faults",
            },
            "images": self.compose("images", "--format", "json"),
        }
        # Docker host names and IDs aren't needed to reproduce a public baseline.
        self.environment["docker"] = {
            k: self.environment["docker"].get(k)
            for k in (
                "ServerVersion",
                "OperatingSystem",
                "Architecture",
                "NCPU",
                "MemTotal",
                "Driver",
                "KernelVersion",
            )
        }
        self.client.get("/health").raise_for_status()
        suites = self.client.get("/test-suites").json()
        self.suite_id = next(s["id"] for s in suites if s["slug"] == "smoke")
        expected = [f"bench-healthy-{i + 1:02d}" for i in range(self.args.fleet)] + [
            "sim-healthy",
            "sim-slow",
            "sim-unreliable",
            "bench-recovery",
        ]
        self.wait_devices(expected)
        self.compose(
            "exec",
            "-T",
            "worker",
            "celery",
            "-A",
            "pulsehunter.tasks.celery_app:celery_app",
            "inspect",
            "ping",
            "--timeout=5",
        )
        self.save()

    def connect_db(self) -> None:
        self.db = psycopg.connect(
            host="127.0.0.1",
            port=self.args.db_port,
            dbname="pulsehunter",
            user="pulsehunter",
            password="pulsehunter",
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )

    def reset_stack(self, reason: str) -> None:
        """Preserve failed samples, then prevent their backlog contaminating another trial."""
        if not self.owns_stack:
            raise RuntimeError("cannot reset a stack this invocation does not own")
        self.capture_logs(f"before-reset-{len(self.records)}.log")
        self.environment.setdefault("stack_resets", []).append(
            {"reason": reason, "after_records": len(self.records)}
        )
        self.save()
        if self.db is not None:
            self.db.close()
        self.compose("down", "--volumes", "--remove-orphans", capture=False)
        self.compose(
            "up", "--detach", "--wait", "--wait-timeout", "180", timeout=240, capture=False
        )
        self.connect_db()
        self.device_ids.clear()
        self.wait_devices(
            [f"bench-healthy-{i + 1:02d}" for i in range(self.args.fleet)]
            + ["sim-healthy", "sim-slow", "sim-unreliable", "bench-recovery"]
        )

    def wait_devices(self, names: list[str], *, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.sql(
                "SELECT id::text, name, status, capabilities FROM devices "
                "WHERE last_heartbeat_at > clock_timestamp() - %s * interval '1 second'",
                (self.heartbeat_timeout_seconds,),
            )
            by_name = {row["name"]: row for row in rows}
            if all(name in by_name and by_name[name]["status"] == "online" for name in names):
                for name in names:
                    row = by_name[name]
                    if row["capabilities"].get("simulated") is not True:
                        raise RuntimeError("benchmark refuses to target a physical device")
                    self.device_ids[name] = row["id"]
                return
            time.sleep(self.args.poll_interval)
        raise TimeoutError(f"devices did not become available: {names}")

    def start_run(self, names: list[str], record: dict[str, Any]) -> str:
        started = time.monotonic()
        record["admission_attempts"] = 0
        record["admission_conflicts"] = []
        deadline = started + 15
        while True:
            self.wait_devices(names)
            response = self.client.post(
                "/runs",
                json={
                    "test_suite_id": self.suite_id,
                    "device_ids": [self.device_ids[n] for n in names],
                },
            )
            record["admission_attempts"] += 1
            record["admission_seconds"] = time.monotonic() - started
            if response.status_code == 409:
                record["admission_conflicts"].append(response.json())
                if time.monotonic() < deadline:
                    time.sleep(0.2)
                    continue
            response.raise_for_status()
            return str(response.json()["id"])

    def jobs(self, run_id: str) -> list[dict[str, Any]]:
        return self.sql(
            "SELECT j.*, d.name AS device_name, d.status AS device_status, "
            "clock_timestamp() AS observed_at FROM test_jobs j JOIN devices d ON d.id=j.device_id "
            "WHERE test_run_id=%s ORDER BY d.name",
            (run_id,),
        )

    def wait_run(
        self, run_id: str, record: dict[str, Any], *, timeout: float = 90
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        signatures: dict[str, tuple[Any, ...]] = {}
        record.setdefault("observations", [])
        while time.monotonic() < deadline:
            jobs = self.jobs(run_id)
            for job in jobs:
                signature = (
                    job["status"],
                    job["attempts"],
                    job["worker_task_id"],
                    str(job["error_details"]),
                )
                key = str(job["id"])
                if signatures.get(key) != signature:
                    record["observations"].append(job)
                    signatures[key] = signature
            if jobs and all(job["status"] in TERMINAL for job in jobs):
                record["jobs"] = jobs
                record["final_status"] = self.sql(
                    "SELECT status FROM test_runs WHERE id=%s", (run_id,)
                )[0]["status"]
                record["terminal"] = record["final_status"] in {"passed", "failed"}
                return jobs
            time.sleep(self.args.poll_interval)
        incomplete = self.jobs(run_id)
        record["jobs"] = incomplete
        record["terminal"] = False
        record["final_status"] = "deadline_exceeded"
        record["error"] = "harness observation deadline exceeded; incomplete samples retained"
        return incomplete

    def save(self) -> None:
        payload = {"schema_version": 1, "environment": self.environment, "records": self.records}
        (self.out / "raw.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        # Round-trip to normalize datetimes before calculating exact timestamp differences.
        normalized = json.loads(json.dumps(self.records, default=str))
        (self.out / "summary.json").write_text(
            json.dumps(summarize(normalized), indent=2), encoding="utf-8"
        )
        with (self.out / "jobs.csv").open("w", newline="", encoding="utf-8") as stream:
            fields = [
                "kind",
                "trial",
                "concurrency",
                "id",
                "device_name",
                "status",
                "attempts",
                "created_at",
                "started_at",
                "completed_at",
                "e2e_seconds",
                "queue_seconds",
                "execution_duration",
            ]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for record in normalized:
                for job in record.get("jobs", []):
                    row = {key: job.get(key) for key in fields}
                    row.update({key: record.get(key) for key in ("kind", "trial", "concurrency")})
                    row["e2e_seconds"] = (
                        elapsed(job["created_at"], job["completed_at"])
                        if job["completed_at"]
                        else None
                    )
                    row["queue_seconds"] = (
                        elapsed(job["created_at"], job["started_at"]) if job["started_at"] else None
                    )
                    writer.writerow(row)

    def set_concurrency(self, concurrency: int, *, start: bool = True) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        worker = config["services"]["worker"]
        worker["command"] = [
            "celery",
            "-A",
            "pulsehunter.tasks.celery_app:celery_app",
            "worker",
            "--loglevel=INFO",
            f"--concurrency={concurrency}",
        ]
        self.config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        if not start:
            return
        self.compose("up", "--detach", "--no-deps", "--force-recreate", "worker", capture=False)
        # Give prefork processes time to become ready, then verify with actual tasks in warm-up.
        time.sleep(2)

    def load_trials(self) -> None:
        names = [f"bench-healthy-{i + 1:02d}" for i in range(self.args.fleet)]
        for index, concurrency in enumerate(self.args.concurrency):
            if index:
                self.set_concurrency(concurrency, start=False)
                self.reset_stack(f"independent concurrency={concurrency}")
            else:
                self.set_concurrency(concurrency)
            for trial in range(self.args.trials + 1):
                if trial and not self.records[-1].get("terminal", False):
                    self.reset_stack("previous batch observation deadline exceeded")
                record: dict[str, Any] = {
                    "kind": "warmup" if trial == 0 else "load",
                    "trial": trial,
                    "concurrency": concurrency,
                    "expected_jobs": len(names),
                    "expected_behavior": False,
                    "terminal": False,
                }
                self.records.append(record)
                started = time.monotonic()
                record["run_id"] = self.start_run(names, record)
                jobs = self.wait_run(record["run_id"], record, timeout=self.args.load_timeout)
                record["client_wall_seconds"] = time.monotonic() - started
                record["throughput"] = None
                record["expected_behavior"] = bool(
                    record["terminal"]
                    and len(jobs) == len(names)
                    and all(j["status"] == "passed" and j["attempts"] == 1 for j in jobs)
                )
                if record["terminal"]:
                    span = (
                        max(j["completed_at"] for j in jobs) - min(j["created_at"] for j in jobs)
                    ).total_seconds()
                    record["throughput"] = len(jobs) / span
                self.save()
                print(
                    f"load concurrency={concurrency} trial={trial} status={record['final_status']} throughput={record['throughput']}",
                    flush=True,
                )
            self.capture_logs(f"concurrency-{concurrency}.log")

    def capture_logs(self, name: str) -> None:
        logs = self.compose("logs", "--no-color", "worker", "postgres", timeout=60)
        (self.out / name).write_text(logs, encoding="utf-8")

    def offline_trial(self, trial: int) -> None:
        name = "sim-healthy"
        self.wait_devices([name])
        record: dict[str, Any] = {
            "kind": "offline",
            "trial": trial,
            "expected_behavior": False,
            "latency_seconds": None,
            "terminal": False,
        }
        self.records.append(record)
        before = self.sql("SELECT clock_timestamp() AS now")[0]["now"]
        self.compose("pause", "agent-healthy")
        after = self.sql("SELECT clock_timestamp() AS now")[0]["now"]
        record["fault_requested_at"] = before
        record["fault_confirmed_at"] = after
        observations = []
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                row = self.sql(
                    "SELECT status, last_heartbeat_at, clock_timestamp() AS observed_at FROM devices WHERE name=%s",
                    (name,),
                )[0]
                observations.append(row)
                if row["status"] == "offline":
                    heartbeat = row["last_heartbeat_at"]
                    record["last_heartbeat_at"] = heartbeat
                    record["latency_seconds"] = (row["observed_at"] - heartbeat).total_seconds()
                    record["fault_to_detection_seconds"] = (
                        row["observed_at"] - after
                    ).total_seconds()
                    record["detection_lower_bound_seconds"] = (
                        (observations[-2]["observed_at"] - heartbeat).total_seconds()
                        if len(observations) > 1
                        else 0
                    )
                    record["detected"] = True
                    break
                time.sleep(self.args.poll_interval)
        finally:
            self.compose("unpause", "agent-healthy")
            self.wait_devices([name])
            record["reconnected"] = True
            record["observations"] = observations
            record["expected_behavior"] = bool(record.get("detected"))
            record["terminal"] = bool(record.get("detected"))
            self.save()
        print(f"offline trial={trial} latency={record['latency_seconds']}", flush=True)

    def fault_job_trial(self, kind: str, trial: int) -> None:
        name = {
            "transient": "sim-unreliable",
            "timeout": "sim-slow",
            "worker_loss": "bench-recovery",
        }[kind]
        record: dict[str, Any] = {
            "kind": kind,
            "trial": trial,
            "expected_behavior": False,
            "latency_seconds": None,
        }
        self.records.append(record)
        record["run_id"] = self.start_run([name], record)
        if kind == "worker_loss":
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                job = self.jobs(record["run_id"])[0]
                if job["status"] == "running":
                    record["attempt_before_fault"] = job
                    break
                time.sleep(self.args.poll_interval)
            else:
                raise TimeoutError("no running job to interrupt")
            record["fault_requested_at"] = self.sql("SELECT clock_timestamp() AS now")[0]["now"]
            try:
                self.compose("kill", "--signal", "SIGKILL", "worker")
                record["fault_confirmed_at"] = self.sql("SELECT clock_timestamp() AS now")[0]["now"]
            finally:
                self.compose("start", "worker")
        jobs = self.wait_run(record["run_id"], record)
        job = jobs[0]
        record["attempts"] = job["attempts"]
        if job["completed_at"]:
            origin = record.get("fault_confirmed_at", job["created_at"])
            record["latency_seconds"] = (job["completed_at"] - origin).total_seconds()
        expected_status, expected_attempts = (
            ("timed_out", 3) if kind == "timeout" else ("passed", 2)
        )
        record["expected_behavior"] = bool(
            record["terminal"]
            and job["status"] == expected_status
            and job["attempts"] == expected_attempts
            and job["device_status"] == "online"
        )
        if kind == "worker_loss":
            record["lease_recovery_observed"] = any(
                (o["error_details"] or {}).get("kind") == "WorkerLeaseExpired"
                for o in record["observations"]
            )
            record["completed_after_expired_lease"] = bool(
                job["completed_at"]
                and job["completed_at"] >= record["attempt_before_fault"]["lease_expires_at"]
            )
            record["expected_behavior"] &= record["completed_after_expired_lease"]
        self.save()
        print(
            f"{kind} trial={trial} status={job['status']} attempts={job['attempts']} latency={record['latency_seconds']} expected={record['expected_behavior']}",
            flush=True,
        )

    def run(self) -> None:
        self.prepare()
        self.load_trials()
        self.set_concurrency(4, start=False)
        self.reset_stack("independent fault trials")
        for kind in ("offline", "transient", "timeout", "worker_loss"):
            for trial in range(1, self.args.fault_trials + 1):
                if kind == "offline":
                    self.offline_trial(trial)
                else:
                    self.fault_job_trial(kind, trial)
        durations = [
            (o["next_attempt_at"] - o["observed_at"]).total_seconds()
            for r in self.records
            for o in r.get("observations", [])
            if o.get("status") == "retrying" and o.get("next_attempt_at") is not None
        ]
        self.environment["observed_retry_remaining_seconds"] = distribution(durations)
        self.save()

    def close(self) -> None:
        if self.owns_stack and self.config_path.exists():
            try:
                logs = self.compose("logs", "--no-color", timeout=60)
                (self.out / "services.log").write_text(logs, encoding="utf-8")
            finally:
                if not self.args.keep_stack:
                    # Exact isolated project resources only; production volumes never referenced.
                    self.compose(
                        "down", "--volumes", "--remove-orphans", timeout=180, capture=False
                    )
        if self.db is not None:
            self.db.close()
        self.client.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", default=".benchmark-results/latest")
    result.add_argument("--trials", type=int, default=5)
    result.add_argument("--fault-trials", type=int, default=5)
    result.add_argument("--fleet", type=int, default=16)
    result.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4, 8])
    result.add_argument("--api-port", type=int, default=18080)
    result.add_argument("--db-port", type=int, default=15432)
    result.add_argument("--poll-interval", type=float, default=0.1)
    result.add_argument("--load-timeout", type=float, default=30)
    result.add_argument("--keep-stack", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if min(args.trials, args.fault_trials, args.fleet) < 1 or args.fleet > 100:
        raise SystemExit("trial counts must be positive; fleet must be 1..100")
    if not args.concurrency or any(c not in (1, 2, 4, 8) for c in args.concurrency):
        raise SystemExit("supported concurrency values are 1, 2, 4, 8")
    if not 0.01 <= args.poll_interval <= 2:
        raise SystemExit("poll interval must be 0.01..2 seconds")
    if args.load_timeout <= 0:
        raise SystemExit("load timeout must be positive")
    harness = Harness(args)
    try:
        harness.run()
    except Exception as exc:
        if harness.owns_output:
            if harness.records:
                harness.records[-1]["error"] = f"{type(exc).__name__}: {exc}"
            harness.save()
            (harness.out / "error.txt").write_text(
                f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
            )
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stdout, exc.stderr, file=sys.stderr)
        return 1
    finally:
        harness.close()
    return (
        0
        if all(
            r.get("expected_behavior", True) and r.get("terminal", False) for r in harness.records
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
