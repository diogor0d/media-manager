from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from media_manager.config import Settings
from media_manager.errors import ApiError, ProcessingError
from media_manager.models import (
    ConversionOptions,
    JobError,
    JobState,
    JobView,
    MediaMetadata,
    OutputMetadata,
)
from media_manager.processor import ConversionCancelled, ConversionResult

logger = logging.getLogger(__name__)


class Processor(Protocol):
    async def verify(self) -> None: ...

    async def convert(
        self,
        input_path: Path,
        job_dir: Path,
        options: ConversionOptions,
        cancel_event: asyncio.Event,
    ) -> ConversionResult: ...


@dataclass(slots=True)
class JobRecord:
    id: str
    principal_id: str
    options: ConversionOptions
    directory: Path
    input_path: Path
    created_at: datetime
    state: JobState = JobState.QUEUED
    input_bytes: int = 0
    input_metadata: MediaMetadata | None = None
    output_metadata: OutputMetadata | None = None
    output_path: Path | None = None
    error: JobError | None = None
    expires_at: datetime | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


class JobManager:
    def __init__(self, settings: Settings, processor: Processor) -> None:
        self._settings = settings
        self._processor = processor
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._janitor_task: asyncio.Task[None] | None = None
        self._ready = False

    @property
    def ready(self) -> bool:
        if not self._ready or self._worker_task is None or self._worker_task.done():
            return False
        try:
            required = self._settings.max_upload_bytes + self._settings.max_output_bytes
            return shutil.disk_usage(self._settings.work_dir).free >= required
        except OSError:
            return False

    async def start(self) -> None:
        self._settings.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        await self._remove_orphans()
        await self._processor.verify()
        self._worker_task = asyncio.create_task(self._worker(), name="media-worker")
        self._janitor_task = asyncio.create_task(self._janitor(), name="job-janitor")
        self._ready = True

    async def stop(self) -> None:
        self._ready = False
        async with self._lock:
            for job in self._jobs.values():
                job.cancel_event.set()

        if self._worker_task:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._worker_task, timeout=5)
            except TimeoutError:
                self._worker_task.cancel()
                await asyncio.gather(self._worker_task, return_exceptions=True)

        if self._janitor_task:
            self._janitor_task.cancel()
            await asyncio.gather(self._janitor_task, return_exceptions=True)

        async with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        await asyncio.gather(*(self._remove_directory(job.directory) for job in jobs))

    async def reserve(self, principal_id: str, options: ConversionOptions) -> JobRecord:
        async with self._lock:
            if len(self._jobs) >= self._settings.max_live_jobs:
                raise ApiError(
                    429,
                    "QUEUE_FULL",
                    "The conversion queue is full",
                    headers={"Retry-After": "10"},
                )

            required = self._settings.max_upload_bytes + self._settings.max_output_bytes
            try:
                free_bytes = shutil.disk_usage(self._settings.work_dir).free
            except OSError as exc:
                raise ApiError(
                    507,
                    "INSUFFICIENT_STORAGE",
                    "Conversion workspace is unavailable",
                ) from exc
            if free_bytes < required:
                raise ApiError(
                    507,
                    "INSUFFICIENT_STORAGE",
                    "Conversion workspace has insufficient free space",
                )

            job_id = str(uuid.uuid4())
            directory = self._settings.work_dir / job_id
            try:
                directory.mkdir(mode=0o700)
            except OSError as exc:
                raise ApiError(
                    507,
                    "INSUFFICIENT_STORAGE",
                    "Conversion workspace is unavailable",
                ) from exc
            job = JobRecord(
                id=job_id,
                principal_id=principal_id,
                options=options,
                directory=directory,
                input_path=directory / "input",
                created_at=datetime.now(UTC),
            )
            self._jobs[job_id] = job
            return job

    async def enqueue(self, job_id: str, input_bytes: int) -> JobView:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
            job.input_bytes = input_bytes
            view = self._snapshot(job)
        await self._queue.put(job_id)
        return view

    async def discard(self, job_id: str) -> None:
        async with self._lock:
            job = self._jobs.pop(job_id, None)
            if job:
                job.cancel_event.set()
        if job:
            await self._remove_directory(job.directory)

    async def get(self, job_id: str, principal_id: str) -> JobView:
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            return self._snapshot(job)

    async def download(self, job_id: str, principal_id: str) -> tuple[Path, str, str]:
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            if job.state is not JobState.READY or not job.output_path or not job.output_metadata:
                raise ApiError(409, "JOB_NOT_READY", "Converted media is not ready")
            if not job.output_path.is_file():
                raise ApiError(410, "RESULT_EXPIRED", "Converted media has expired")

            # Keep the result alive while Starlette opens and streams the file.
            minimum_expiry = datetime.now(UTC) + timedelta(seconds=60)
            if job.expires_at is None or job.expires_at < minimum_expiry:
                job.expires_at = minimum_expiry
            return job.output_path, job.output_metadata.filename, job.output_metadata.media_type

    async def delete(self, job_id: str, principal_id: str) -> None:
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            self._jobs.pop(job_id)
            job.cancel_event.set()

        if job.state is not JobState.PROCESSING:
            await self._remove_directory(job.directory)

    def _authorized_job(self, job_id: str, principal_id: str) -> JobRecord:
        job = self._jobs.get(job_id)
        if job is None or job.principal_id != principal_id:
            raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
        return job

    def _snapshot(self, job: JobRecord) -> JobView:
        return JobView(
            id=job.id,
            state=job.state,
            target=job.options.target,
            quality=job.options.quality,
            resolution=job.options.resolution,
            audio=job.options.audio,
            created_at=job.created_at,
            expires_at=job.expires_at,
            status_url=f"{self._settings.public_base_url}/v1/jobs/{job.id}",
            input=job.input_metadata,
            output=job.output_metadata,
            error=job.error,
        )

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id is None:
                    return

                async with self._lock:
                    job = self._jobs.get(job_id)
                    if job is None:
                        continue
                    job.state = JobState.PROCESSING

                try:
                    result = await self._processor.convert(
                        job.input_path,
                        job.directory,
                        job.options,
                        job.cancel_event,
                    )
                except ConversionCancelled:
                    await self._finish_cancelled(job)
                except ProcessingError as exc:
                    await self._finish_failed(job, exc.code, exc.message)
                except Exception as exc:
                    logger.error(
                        "Unhandled conversion failure for job %s (%s)",
                        job.id,
                        type(exc).__name__,
                    )
                    await self._finish_failed(
                        job,
                        "CONVERSION_FAILED",
                        "The media could not be converted",
                    )
                else:
                    await self._finish_ready(job, result)
            finally:
                self._queue.task_done()

    async def _finish_ready(self, job: JobRecord, result: ConversionResult) -> None:
        await self._remove_file(job.input_path)
        async with self._lock:
            current = self._jobs.get(job.id)
            if current is not job:
                remove = True
            else:
                job.input_metadata = result.input
                job.output_path = result.output_path
                job.output_metadata = OutputMetadata(
                    bytes=result.output_bytes,
                    filename=result.filename,
                    media_type=result.media_type,
                    download_url=(
                        f"{self._settings.public_base_url}/v1/jobs/{job.id}/content"
                    ),
                    width=result.width,
                    height=result.height,
                    duration_ms=result.duration_ms,
                )
                job.state = JobState.READY
                job.expires_at = datetime.now(UTC) + timedelta(
                    seconds=self._settings.result_ttl_seconds
                )
                remove = False
        if remove:
            await self._remove_directory(job.directory)

    async def _finish_failed(self, job: JobRecord, code: str, message: str) -> None:
        await self._remove_job_files(job.directory)
        async with self._lock:
            current = self._jobs.get(job.id)
            if current is job:
                job.state = JobState.FAILED
                job.error = JobError(code=code, message=message)
                job.expires_at = datetime.now(UTC) + timedelta(
                    seconds=self._settings.result_ttl_seconds
                )
                remove = False
            else:
                remove = True
        if remove:
            await self._remove_directory(job.directory)

    async def _finish_cancelled(self, job: JobRecord) -> None:
        async with self._lock:
            self._jobs.pop(job.id, None)
        await self._remove_directory(job.directory)

    async def _janitor(self) -> None:
        while True:
            await asyncio.sleep(self._settings.cleanup_interval_seconds)
            now = datetime.now(UTC)
            async with self._lock:
                expired = [
                    job
                    for job in self._jobs.values()
                    if job.expires_at is not None and job.expires_at <= now
                ]
                for job in expired:
                    self._jobs.pop(job.id, None)
                    job.cancel_event.set()
            await asyncio.gather(*(self._remove_directory(job.directory) for job in expired))

    async def _remove_orphans(self) -> None:
        children = list(self._settings.work_dir.iterdir())
        await asyncio.gather(*(self._remove_path(path) for path in children))

    @staticmethod
    async def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            await asyncio.to_thread(path.unlink, missing_ok=True)
        elif path.exists():
            await asyncio.to_thread(shutil.rmtree, path, True)

    @classmethod
    async def _remove_directory(cls, path: Path) -> None:
        await cls._remove_path(path)

    @classmethod
    async def _remove_job_files(cls, directory: Path) -> None:
        if not directory.exists():
            return
        children = list(directory.iterdir())
        await asyncio.gather(*(cls._remove_path(path) for path in children))

    @staticmethod
    async def _remove_file(path: Path) -> None:
        await asyncio.to_thread(path.unlink, missing_ok=True)
