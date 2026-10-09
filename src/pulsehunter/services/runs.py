from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from pulsehunter.core.time import utc_now
from pulsehunter.models.domain import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    TERMINAL_RUN_STATUSES,
    Device,
    DeviceStatus,
    JobStatus,
    RunStatus,
    TestJob,
    TestRun,
    TestSuite,
)
from pulsehunter.schemas.api import RunCounts, RunCreate, TestJobRead, TestRunRead
from pulsehunter.services.devices import available_devices_query


class TestSuiteNotFoundError(LookupError):
    pass


class TestRunNotFoundError(LookupError):
    pass


class NoAvailableDevicesError(RuntimeError):
    pass


def create_run(
    session: Session,
    request: RunCreate,
    *,
    heartbeat_timeout_seconds: float,
    now: datetime | None = None,
) -> tuple[TestRun, list[uuid.UUID]]:
    current_time = now or utc_now()
    suite = session.get(TestSuite, request.test_suite_id)
    if suite is None:
        raise TestSuiteNotFoundError(str(request.test_suite_id))

    query = available_devices_query(
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        now=current_time,
    )
    if request.device_ids is not None:
        query = query.where(Device.id.in_(request.device_ids))
    devices = list(
        session.scalars(query.order_by(Device.name).with_for_update(skip_locked=True)).all()
    )

    if request.device_ids is not None and len(devices) != len(request.device_ids):
        raise NoAvailableDevicesError(
            "One or more requested devices are unknown, stale, offline, or already busy"
        )
    if not devices:
        raise NoAvailableDevicesError("No online, available devices were found")

    test_run = TestRun(
        test_suite_id=suite.id,
        status=RunStatus.QUEUED,
        source=request.source.model_dump(exclude_none=True) if request.source else None,
    )
    session.add(test_run)
    session.flush()

    jobs: list[TestJob] = []
    for device in devices:
        device.status = DeviceStatus.BUSY
        job = TestJob(
            test_run_id=test_run.id,
            device_id=device.id,
            status=JobStatus.QUEUED,
            attempts=0,
        )
        session.add(job)
        jobs.append(job)
    session.flush()
    return test_run, [job.id for job in jobs]


def recompute_run_status(
    session: Session,
    run_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> TestRun:
    current_time = now or utc_now()
    # Status aggregation never changes the run's referenced key. PostgreSQL
    # FOR NO KEY UPDATE excludes other writers but permits FK KEY SHARE locks
    # acquired by sibling job updates; FOR UPDATE caused a lock-upgrade cycle.
    test_run = session.scalar(
        select(TestRun).where(TestRun.id == run_id).with_for_update(key_share=True)
    )
    if test_run is None:
        raise TestRunNotFoundError(str(run_id))
    if test_run.status in TERMINAL_RUN_STATUSES:
        return test_run

    statuses = list(
        session.scalars(select(TestJob.status).where(TestJob.test_run_id == run_id)).all()
    )
    if not statuses:
        test_run.status = RunStatus.QUEUED
        return test_run

    if any(status in ACTIVE_JOB_STATUSES for status in statuses):
        has_started = any(status != JobStatus.QUEUED for status in statuses)
        test_run.status = RunStatus.RUNNING if has_started else RunStatus.QUEUED
        if has_started and test_run.started_at is None:
            test_run.started_at = current_time
        test_run.completed_at = None
        return test_run

    if all(status in TERMINAL_JOB_STATUSES for status in statuses):
        test_run.status = (
            RunStatus.PASSED
            if all(status == JobStatus.PASSED for status in statuses)
            else RunStatus.FAILED
        )
        if test_run.started_at is None:
            test_run.started_at = current_time
        if test_run.completed_at is None:
            test_run.completed_at = current_time
    return test_run


def _run_counts(jobs: list[TestJob]) -> RunCounts:
    counts = Counter(job.status for job in jobs)
    return RunCounts(
        total=len(jobs),
        queued=counts[JobStatus.QUEUED],
        running=counts[JobStatus.RUNNING],
        retrying=counts[JobStatus.RETRYING],
        passed=counts[JobStatus.PASSED],
        failed=counts[JobStatus.FAILED],
        timed_out=counts[JobStatus.TIMED_OUT],
    )


def load_run(session: Session, run_id: uuid.UUID) -> TestRun:
    test_run = session.scalar(
        select(TestRun)
        .where(TestRun.id == run_id)
        .options(
            selectinload(TestRun.test_suite),
            selectinload(TestRun.jobs).selectinload(TestJob.device),
        )
    )
    if test_run is None:
        raise TestRunNotFoundError(str(run_id))
    return test_run


def list_runs(session: Session, *, limit: int = 50) -> list[TestRun]:
    return list(
        session.scalars(
            select(TestRun)
            .order_by(TestRun.created_at.desc())
            .limit(limit)
            .options(
                selectinload(TestRun.test_suite),
                selectinload(TestRun.jobs).selectinload(TestJob.device),
            )
        ).all()
    )


def run_read_model(test_run: TestRun) -> TestRunRead:
    return TestRunRead(
        id=test_run.id,
        test_suite_id=test_run.test_suite_id,
        test_suite_name=test_run.test_suite.name,
        status=test_run.status,
        created_at=test_run.created_at,
        started_at=test_run.started_at,
        completed_at=test_run.completed_at,
        counts=_run_counts(test_run.jobs),
        source=test_run.source,
    )


def job_read_model(job: TestJob) -> TestJobRead:
    return TestJobRead(
        id=job.id,
        test_run_id=job.test_run_id,
        device_id=job.device_id,
        device_name=job.device.name,
        status=job.status,
        attempts=job.attempts,
        result=job.result,
        logs=job.logs,
        error_message=job.error_message,
        error_details=job.error_details,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        execution_duration=job.execution_duration,
    )
