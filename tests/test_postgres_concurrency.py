from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from pulsehunter.core.config import Settings, get_settings
from pulsehunter.db.base import Base
from pulsehunter.db.seed import seed_default_suite
from pulsehunter.models.domain import DeviceStatus, JobStatus, RunStatus
from pulsehunter.models.domain import TestJob as JobModel
from pulsehunter.models.domain import TestRun as RunModel
from pulsehunter.schemas.agent import AgentExecutionRequest, AgentExecutionResponse, AgentOutcome
from pulsehunter.schemas.api import DeviceRegister, RunCreate
from pulsehunter.services.device_client import HttpDeviceClient, TransientDeviceError
from pulsehunter.services.devices import register_device
from pulsehunter.services.execution import JobExecutor
from pulsehunter.services.runs import NoAvailableDevicesError, create_run, recompute_run_status

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("PULSEHUNTER_RUN_SERVICE_TESTS") != "1",
        reason="requires isolated PostgreSQL service tests",
    ),
]


@pytest.fixture
def postgres_factory() -> Iterator[sessionmaker[Session]]:
    url = get_settings().database_url
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL lock semantics cannot be tested with SQLite")
    root = create_engine(url, pool_pre_ping=True)
    schema = f"pulsehunter_concurrency_{uuid.uuid4().hex}"
    with root.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = root.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        with root.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        root.dispose()


def queued_batch(
    factory: sessionmaker[Session], size: int = 2
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    with factory() as session:
        suite = seed_default_suite(session)
        devices = [
            register_device(
                session,
                DeviceRegister(
                    name=f"concurrent-{index}",
                    device_type="simulator",
                    endpoint_url=f"http://concurrent-{index}:9000",
                    capabilities={},
                ),
            )
            for index in range(size)
        ]
        run, jobs = create_run(
            session,
            RunCreate(test_suite_id=suite.id, device_ids=[device.id for device in devices]),
            heartbeat_timeout_seconds=30,
        )
        session.commit()
        return run.id, jobs


@pytest.mark.parametrize("operation", ["claim", "finish", "retry"])
def test_parallel_updates_do_not_deadlock_on_run_lock_upgrade(
    postgres_factory: sessionmaker[Session], operation: str
) -> None:
    factory = postgres_factory
    run_id, job_ids = queued_batch(factory)
    executor = JobExecutor(
        factory, HttpDeviceClient(), Settings(environment="test"), lambda job_id, delay: None
    )
    claims = []
    if operation != "claim":
        claims = [
            executor._claim(job_id, worker_task_id=f"owner-{index}")
            for index, job_id in enumerate(job_ids)
        ]
    engine = factory.kw["bind"]
    ready = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(2, action=ready.set)
    statements: list[str] = []

    def synchronize_run_lock(connection, cursor, statement, parameters, context, executemany):
        if threading.current_thread().name.startswith("claim"):
            statements.append(statement)
            if "test_runs" in statement and "FOR " in statement:
                barrier.wait(timeout=10)
                assert release.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", synchronize_run_lock)
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="claim") as pool:
            if operation == "claim":
                futures = [
                    pool.submit(executor._claim, job_id, worker_task_id=f"owner-{index}")
                    for index, job_id in enumerate(job_ids)
                ]
            elif operation == "finish":
                futures = [
                    pool.submit(
                        executor._finish,
                        job_id,
                        worker_task_id=f"owner-{index}",
                        target=JobStatus.PASSED,
                        elapsed=0.4,
                        result={"verified": True},
                        logs=["ok"],
                    )
                    for index, job_id in enumerate(job_ids)
                ]
            else:
                futures = [
                    pool.submit(
                        executor._handle_retryable_failure,
                        claim,
                        worker_task_id=f"owner-{index}",
                        error=TransientDeviceError("temporary"),
                        exhausted_status=JobStatus.FAILED,
                        elapsed=0.4,
                    )
                    for index, claim in enumerate(claims)
                ]
            try:
                assert ready.wait(timeout=10), "both claims must reach the run-lock boundary"
                if os.getenv("PULSEHUNTER_CAPTURE_LOCKS") == "1":
                    schema = engine.get_execution_options()["schema_translate_map"][None]
                    with engine.connect() as observer:
                        print(
                            "RUN LOCKS BEFORE AGGREGATION:",
                            observer.execute(
                                text("SELECT modes, pids FROM pgrowlocks(:table)"),
                                {"table": f"{schema}.test_runs"},
                            ).all(),
                        )
                    print("CLAIM SQL:", "\n".join(statements))
            finally:
                release.set()
            results = [future.result(timeout=15) for future in futures]
        if operation == "claim":
            assert all(result is not None for result in results)
        elif operation == "retry":
            assert results == ["retrying", "retrying"]
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", synchronize_run_lock)
    with factory() as session:
        jobs = list(session.scalars(select(JobModel).where(JobModel.test_run_id == run_id)))
        expected = {
            "claim": JobStatus.RUNNING,
            "finish": JobStatus.PASSED,
            "retry": JobStatus.RETRYING,
        }[operation]
        assert all(job.status == expected and job.attempts == 1 for job in jobs)
        assert all(
            job.device.status
            == (DeviceStatus.ONLINE if operation == "finish" else DeviceStatus.BUSY)
            for job in jobs
        )
        assert session.get(RunModel, run_id).status == (
            RunStatus.PASSED if operation == "finish" else RunStatus.RUNNING
        )
        if operation == "finish":
            assert all(job.result == {"verified": True} and job.logs == ["ok"] for job in jobs)
        if operation == "retry":
            assert all(
                job.next_attempt_at is not None and job.lease_expires_at is None for job in jobs
            )


def test_duplicate_delivery_does_not_execute_device_twice(
    postgres_factory: sessionmaker[Session],
) -> None:
    factory = postgres_factory
    run_id, job_ids = queued_batch(factory, size=1)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class BlockingClient:
        def execute(
            self, endpoint_url: str, request: AgentExecutionRequest, *, timeout_seconds: float
        ) -> AgentExecutionResponse:
            calls.append(request.job_id)
            entered.set()
            assert release.wait(timeout=10)
            return AgentExecutionResponse(
                job_id=request.job_id,
                outcome=AgentOutcome.PASSED,
                duration_ms=400,
                result={"verified": True},
            )

    executor = JobExecutor(
        factory, BlockingClient(), Settings(environment="test"), lambda job_id, delay: None
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(executor.execute, job_ids[0], worker_task_id="owner")
            assert entered.wait(timeout=10)
            # The HTTP call is blocked, but no database locks are held by its worker.
            duplicate = pool.submit(executor.execute, job_ids[0], worker_task_id="duplicate")
            assert duplicate.result(timeout=5) == "ignored"
            # A stale worker cannot overwrite the active owner's result or release its device.
            executor._finish(job_ids[0], worker_task_id="stale", target=JobStatus.FAILED, elapsed=1)
            with factory() as session:
                job = session.get(JobModel, job_ids[0])
                assert job.status == JobStatus.RUNNING and job.device.status == DeviceStatus.BUSY
                assert job.worker_task_id == "owner" and job.result is None
            release.set()
            assert first.result(timeout=10) == "passed"
    finally:
        release.set()
    assert calls == job_ids
    executor._finish(job_ids[0], worker_task_id="owner", target=JobStatus.FAILED, elapsed=1)
    with factory() as session:
        job = session.get(JobModel, job_ids[0])
        assert job.attempts == 1 and job.result == {"verified": True}
        assert job.device.status == DeviceStatus.ONLINE
        assert session.get(RunModel, run_id).status == RunStatus.PASSED


def test_concurrent_runs_cannot_reserve_the_same_device(
    postgres_factory: sessionmaker[Session],
) -> None:
    factory = postgres_factory
    with factory() as session:
        suite = seed_default_suite(session)
        device = register_device(
            session,
            DeviceRegister(
                name="exclusive",
                device_type="simulator",
                endpoint_url="http://exclusive:9000",
                capabilities={},
            ),
        )
        session.commit()
        request = RunCreate(test_suite_id=suite.id, device_ids=[device.id])
    reserved = threading.Event()
    release = threading.Event()

    def reserve_first():
        with factory() as session:
            run, jobs = create_run(session, request, heartbeat_timeout_seconds=30)
            reserved.set()
            assert release.wait(timeout=10)
            session.commit()
            return run.id, jobs

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(reserve_first)
            assert reserved.wait(timeout=10)
            with factory() as session, pytest.raises(NoAvailableDevicesError):
                create_run(session, request, heartbeat_timeout_seconds=30)
            release.set()
            run_id, jobs = first.result(timeout=10)
    finally:
        release.set()
    with factory() as session:
        assert len(list(session.scalars(select(JobModel)))) == len(jobs) == 1
        assert session.get(RunModel, run_id).jobs[0].device.status == DeviceStatus.BUSY
    with factory() as session, pytest.raises(NoAvailableDevicesError):
        create_run(session, request, heartbeat_timeout_seconds=30)


def test_run_status_writers_remain_serialized(postgres_factory: sessionmaker[Session]) -> None:
    factory = postgres_factory
    run_id, _ = queued_batch(factory)
    started = threading.Event()
    backend = []

    def second_writer():
        with factory() as session:
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            recompute_run_status(session, run_id)
            session.commit()

    with factory() as first:
        recompute_run_status(first, run_id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(second_writer)
            try:
                assert started.wait(timeout=5)
                deadline = time.monotonic() + 5
                with factory() as observer:
                    while time.monotonic() < deadline:
                        wait = observer.scalar(
                            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                            {"pid": backend[0]},
                        )
                        # pg_stat_activity snapshots must refresh between observations.
                        observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                        if wait == "Lock":
                            break
                        assert not future.done(), "the second status writer must wait for the first"
                        time.sleep(0.01)
                    else:
                        pytest.fail("second writer did not enter a PostgreSQL lock wait")
                assert not future.done()
            finally:
                first.commit()
            future.result(timeout=5)
