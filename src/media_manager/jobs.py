from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from media_manager.config import Settings
from media_manager.errors import ApiError, ProcessingError
from media_manager.models import (
    CompressionJobView,
    CompressionMetadata,
    CompressionOptions,
    ConversionOptions,
    EstimateView,
    HistoryEntry,
    JobError,
    JobProgress,
    JobProgressStage,
    JobState,
    JobView,
    MediaMetadata,
    OutputMetadata,
    UploadView,
)
from media_manager.processor import CompressionResult, ConversionCancelled, ConversionResult

logger = logging.getLogger(__name__)


class Processor(Protocol):
    async def verify(self) -> None: ...

    async def inspect(
        self, input_path: Path, cancel_event: asyncio.Event | None = None
    ) -> MediaMetadata: ...

    async def convert(
        self,
        input_path: Path,
        job_dir: Path,
        options: ConversionOptions,
        cancel_event: asyncio.Event,
        progress_callback: Callable[[JobProgressStage, int | None], Awaitable[None]] | None = None,
    ) -> ConversionResult: ...

    async def compress(
        self,
        input_path: Path,
        job_dir: Path,
        cancel_event: asyncio.Event,
        progress_callback: Callable[[JobProgressStage, int | None], Awaitable[None]] | None = None,
    ) -> CompressionResult: ...


@dataclass(slots=True)
class JobRecord:
    id: str
    principal_id: str
    options: ConversionOptions | None
    directory: Path
    input_path: Path
    created_at: datetime
    state: JobState = JobState.UPLOADING
    input_bytes: int = 0
    expected_input_bytes: int | None = None
    input_metadata: MediaMetadata | None = None
    output_metadata: OutputMetadata | None = None
    output_path: Path | None = None
    error: JobError | None = None
    progress: JobProgress | None = None
    source_filename: str | None = None
    is_compression: bool = False
    compression: CompressionMetadata | None = None
    expires_at: datetime | None = None
    completed_at: datetime | None = None
    history_persisted: bool = False
    accepted: bool = False
    recovery_count: int = 0
    compression_options: CompressionOptions | None = None
    origin: str = "api"
    parent_id: str | None = None
    processing_started: float | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    upload_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class JobManager:
    _HISTORY_DIR_NAME = ".history"
    _HISTORY_FILE_NAME = "history.json"
    _PREFERENCES_FILE_NAME = "preferences.json"
    _PENDING_FILE_NAME = "pending.json"

    def __init__(self, settings: Settings, processor: Processor) -> None:
        self._settings = settings
        self._processor = processor
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._janitor_task: asyncio.Task[None] | None = None
        self._ready = False
        self._retention_preferences: dict[str, int] = {}
        self._stopping = False
        self._estimates: dict[str, EstimateView] = {}
        self._estimate_tasks: dict[str, asyncio.Task] = {}
        self._estimate_cancellations: dict[str, asyncio.Event] = {}
        self._sample_lock = asyncio.Lock()
        self._acceptance_tasks: set[asyncio.Task] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self.event_sink = None

    @property
    def _preferences_dir(self) -> Path:
        return self._settings.work_dir / self._HISTORY_DIR_NAME

    @property
    def _preferences_path(self) -> Path:
        return self._preferences_dir / self._PREFERENCES_FILE_NAME

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
        self._stopping = False
        self._settings.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        await self._load_preferences()
        restored = await self._remove_orphans()
        async with self._lock:
            self._jobs.update({job.id: job for job in restored})
        await self._processor.verify()
        for job in restored:
            if job.state is JobState.QUEUED:
                await self._queue.put(job.id)
        self._worker_task = asyncio.create_task(self._worker(), name="media-worker")
        self._janitor_task = asyncio.create_task(self._janitor(), name="job-janitor")
        self._ready = True

    async def stop(self) -> None:
        self._ready = False
        self._stopping = True
        await asyncio.gather(*self._acceptance_tasks, return_exceptions=True)
        for cancellation in self._estimate_cancellations.values():
            cancellation.set()
        for task in self._estimate_tasks.values():
            task.cancel()
        await asyncio.gather(*self._estimate_tasks.values(), return_exceptions=True)
        async with self._lock:
            for job in self._jobs.values():
                if job.state not in (JobState.READY, JobState.FAILED):
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
        await asyncio.gather(
            *(
                self._remove_record_directory(job)
                for job in jobs
                if not job.accepted and not job.history_persisted
            )
        )

    async def reserve(
        self,
        principal_id: str,
        options: ConversionOptions | None,
        expected_input_bytes: int | None = None,
        source_filename: str | None = None,
    ) -> JobRecord:
        principal_id = self._settings.owner_id(principal_id)
        async with self._lock:
            active_jobs = sum(
                job.state in (
                    JobState.UPLOADING, JobState.AWAITING_REVIEW,
                    JobState.QUEUED, JobState.PROCESSING,
                )
                for job in self._jobs.values()
            )
            if active_jobs >= self._settings.max_live_jobs:
                raise ApiError(
                    429,
                    "QUEUE_FULL",
                    "The conversion queue is full",
                    headers={"Retry-After": "10"},
                )

            output_slots = 2 if options is None else 1
            required = self._settings.max_upload_bytes + (
                output_slots * self._settings.max_output_bytes
            )
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
                expected_input_bytes=expected_input_bytes,
                source_filename=_safe_source_name(source_filename),
                expires_at=datetime.now(UTC)
                + timedelta(seconds=self._settings.upload_session_ttl_seconds),
            )
            self._jobs[job_id] = job
        return job

    async def reserve_compression(
        self,
        principal_id: str,
        expected_input_bytes: int | None = None,
        source_filename: str | None = None,
    ) -> JobRecord:
        job = await self.reserve(
            principal_id,
            None,
            expected_input_bytes,
            source_filename,
        )
        job.is_compression = True
        return job

    async def enqueue(self, job_id: str, input_bytes: int) -> JobView | CompressionJobView:
        return await self._accept(self._enqueue(job_id, input_bytes))

    async def _accept(self, operation):
        task = asyncio.create_task(operation)
        self._acceptance_tasks.add(task)

        def completed(finished: asyncio.Task) -> None:
            self._acceptance_tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def _enqueue(self, job_id: str, input_bytes: int) -> JobView | CompressionJobView:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
            if job.state is not JobState.UPLOADING:
                raise ApiError(409, "UPLOAD_ALREADY_COMPLETED", "Upload is already complete")
            if job.expected_input_bytes is not None and input_bytes != job.expected_input_bytes:
                raise ApiError(409, "UPLOAD_INCOMPLETE", "Upload is incomplete")
            job.input_bytes = input_bytes
            job.state = JobState.QUEUED
            job.progress = JobProgress(stage=JobProgressStage.QUEUED)
            job.expires_at = None
            job.accepted = True
            try:
                await self._persist_pending(job)
            except (OSError, ValueError) as exc:
                job.accepted = False
                job.state = JobState.UPLOADING
                raise ApiError(507, "JOB_PERSISTENCE_FAILED", "Job could not be saved") from exc
            view = self._snapshot(job)
        self._idle.clear()
        self._queue.put_nowait(job_id)
        return view

    async def upload_view(self, job_id: str, principal_id: str) -> UploadView:
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            return self._upload_snapshot(job)

    async def prepare(self, job_id: str, input_bytes: int) -> JobView | CompressionJobView:
        return await self._accept(self._prepare(job_id, input_bytes))

    async def _prepare(self, job_id: str, input_bytes: int) -> JobView | CompressionJobView:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state is not JobState.UPLOADING:
                raise ApiError(409, "UPLOAD_ALREADY_COMPLETED", "Upload is already complete")
            if not input_bytes or (
                job.expected_input_bytes is not None and input_bytes != job.expected_input_bytes
            ):
                raise ApiError(409, "UPLOAD_INCOMPLETE", "Upload is incomplete")
            job.input_bytes = input_bytes
            job.state = JobState.AWAITING_REVIEW
            job.accepted = True
            upload_expiry = job.expires_at
            job.expires_at = datetime.now(UTC) + timedelta(
                seconds=self.retention_seconds(job.principal_id)
            )
            try:
                await self._persist_pending(job)
            except (OSError, ValueError) as exc:
                job.state = JobState.UPLOADING
                job.accepted = False
                job.expires_at = upload_expiry
                raise ApiError(507, "JOB_PERSISTENCE_FAILED", "Upload could not be saved") from exc
            return self._snapshot(job)

    async def start_prepared(
        self,
        job_id: str,
        principal_id: str,
        conversion: ConversionOptions | None = None,
        compression: CompressionOptions | None = None,
    ) -> JobView | CompressionJobView:
        return await self._accept(
            self._start_prepared(job_id, principal_id, conversion, compression)
        )

    async def _start_prepared(
        self,
        job_id: str,
        principal_id: str,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
    ) -> JobView | CompressionJobView:
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            if self._is_expired(job):
                raise ApiError(410, "RESULT_EXPIRED", "Prepared upload has expired")
            if job.state in (JobState.QUEUED, JobState.PROCESSING, JobState.READY):
                return self._snapshot(job)
            if job.state is not JobState.AWAITING_REVIEW:
                raise ApiError(409, "JOB_NOT_PREPARED", "Job is not awaiting review")
            if compression is not None:
                if compression.target_bytes > max(self._settings.max_output_bytes, 20_000_000):
                    raise ApiError(422, "INVALID_OPTIONS", "Size target exceeds the output limit")
                if conversion is not None:
                    compression = compression.model_copy(update={
                        "target": conversion.target,
                    })
                job.is_compression = True
                job.compression_options = compression
                job.options = None
            else:
                job.is_compression = False
                job.options = conversion or job.options
                if job.options is None:
                    raise ApiError(422, "INVALID_OPTIONS", "Select an output format")
            review_expiry = job.expires_at
            job.expires_at = None
            job.state = JobState.QUEUED
            job.progress = JobProgress(stage=JobProgressStage.QUEUED)
            try:
                await self._persist_pending(job)
            except (OSError, ValueError) as exc:
                job.state = JobState.AWAITING_REVIEW
                job.expires_at = review_expiry
                raise ApiError(507, "JOB_PERSISTENCE_FAILED", "Job could not be saved") from exc
            view = self._snapshot(job)
        self._idle.clear()
        self._queue.put_nowait(job_id)
        return view

    async def prepare_from_path(
        self,
        path: Path,
        principal_id: str,
        filename: str,
        conversion_options: ConversionOptions | None = None,
        compression_options: CompressionOptions | None = None,
        parent_id: str | None = None,
        origin: str = "api",
        auto_start: bool = False,
    ) -> JobView | CompressionJobView:
        existing = (
            await self.find_child(parent_id, principal_id, active_only=True) if parent_id else None
        )
        if existing is not None:
            if auto_start and existing.state is JobState.AWAITING_REVIEW:
                return await self.start_prepared(
                    existing.id, principal_id, conversion_options, compression_options
                )
            return existing
        if path.is_symlink() or not path.is_file():
            raise ApiError(410, "RESULT_EXPIRED", "Source is no longer available")
        size = path.stat().st_size
        if size <= 0 or size > self._settings.max_upload_bytes:
            raise ApiError(413, "INPUT_TOO_LARGE", "Source exceeds the input limit")
        # Reserve as a conversion until the explicit start selects the final operation.
        default = conversion_options or ConversionOptions(target="video-mp4")
        job = await self.reserve(
            principal_id, None if compression_options is not None else default, size, filename
        )
        job.parent_id = parent_id
        job.origin = origin
        if compression_options is not None:
            if conversion_options is not None:
                compression_options = compression_options.model_copy(update={
                    "target": conversion_options.target,
                })
            job.is_compression = True
            job.compression_options = compression_options
            job.options = None
        try:
            await asyncio.to_thread(shutil.copyfile, path, job.input_path)
            await asyncio.to_thread(os.chmod, job.input_path, 0o600)
            prepared = await self.prepare(job.id, size)
        except BaseException:
            await self.discard(job.id)
            raise
        if auto_start:
            return await self.start_prepared(
                job.id, principal_id, conversion_options, compression_options
            )
        return prepared

    async def find_child(
        self, parent_id: str | None, principal_id: str, *, active_only: bool = False
    ) -> JobView | CompressionJobView | None:
        async with self._lock:
            for job in self._jobs.values():
                if active_only and job.state not in {
                    JobState.AWAITING_REVIEW, JobState.QUEUED, JobState.PROCESSING
                }:
                    continue
                if parent_id and job.parent_id == parent_id and (
                    self._settings.owner_id(job.principal_id)
                    == self._settings.owner_id(principal_id)
                ) and not self._is_expired(job):
                    return self._snapshot(job)
        return None

    async def inspect(self, job_id: str, principal_id: str) -> MediaMetadata:
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            if self._is_expired(job):
                raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
            if job.input_metadata is not None:
                return job.input_metadata
            if job.state is not JobState.AWAITING_REVIEW:
                raise ApiError(409, "JOB_NOT_PREPARED", "Upload is not awaiting review")
            path = job.input_path
        return await self._processor.inspect(path)

    async def estimate(
        self,
        job_id: str,
        principal_id: str,
        conversion: ConversionOptions | None = None,
        compression: CompressionOptions | None = None,
    ) -> EstimateView:
        from media_manager.estimates import EstimateService

        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            if job.state is not JobState.AWAITING_REVIEW or self._is_expired(job):
                raise ApiError(409, "JOB_NOT_PREPARED", "Upload is not awaiting review")
            path = job.input_path
        service = EstimateService(self._settings, self._processor)
        if compression and compression.target_bytes > max(
            self._settings.max_output_bytes, 20_000_000
        ):
            raise ApiError(422, "INVALID_OPTIONS", "Size target exceeds the output limit")
        fast = await service.fast(path, conversion, compression)
        cached = self._estimates.get(job_id)
        if cached and cached.options_key == fast.options_key:
            return cached
        old = self._estimate_cancellations.get(job_id)
        if old:
            old.set()
        old_task = self._estimate_tasks.get(job_id)
        if old_task:
            old_task.cancel()
        cancellation = asyncio.Event()
        self._estimate_cancellations[job_id] = cancellation
        self._estimates[job_id] = fast

        async def refine() -> None:
            try:
                async with self._sample_lock:
                    await self._idle.wait()
                    if cancellation.is_set():
                        return
                    refined = await service.refine(path, conversion, compression, cancellation)
                if not cancellation.is_set() and self._jobs.get(job_id) is job:
                    self._estimates[job_id] = refined
            except Exception:
                if not cancellation.is_set():
                    self._estimates[job_id] = fast.model_copy(update={"status": "fallback"})

        self._estimate_tasks[job_id] = asyncio.create_task(refine(), name="media-estimate")
        return fast

    async def latest_estimate(self, job_id: str, principal_id: str) -> EstimateView:
        async with self._lock:
            self._authorized_job(job_id, principal_id)
        value = self._estimates.get(job_id)
        if value is None:
            raise ApiError(404, "ESTIMATE_NOT_FOUND", "Request an estimate first")
        return value

    async def _cancel_samples(self) -> None:
        for cancellation in self._estimate_cancellations.values():
            cancellation.set()
        for task in self._estimate_tasks.values():
            task.cancel()
        for job_id, estimate in list(self._estimates.items()):
            if estimate.status == "pending":
                self._estimates[job_id] = estimate.model_copy(update={"status": "fallback"})
        # Wait until sample subprocesses have exited before taking the encoding slot.
        await asyncio.gather(*self._estimate_tasks.values(), return_exceptions=True)

    async def _emit(self, event: str, job: JobRecord) -> None:
        if self.event_sink is not None and job.output_metadata is not None:
            try:
                await self.event_sink(
                    event, job.id, self._settings.owner_id(job.principal_id),
                    job.output_metadata.filename, job.output_metadata.bytes, job.expires_at,
                )
            except Exception:
                logger.warning("Notification could not be queued for job %s", job.id)

    async def notify_download(self, job_id: str, principal_id: str) -> None:
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
        await self._emit("download", job)

    @asynccontextmanager
    async def upload_chunk(
        self,
        job_id: str,
        principal_id: str,
        offset: int,
    ) -> AsyncIterator[JobRecord]:
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            upload_lock = job.upload_lock

        await upload_lock.acquire()
        try:
            async with self._lock:
                current = self._authorized_job(job_id, principal_id)
                if current is not job or job.state is not JobState.UPLOADING:
                    raise ApiError(409, "UPLOAD_ALREADY_COMPLETED", "Upload is already complete")
                if offset != job.input_bytes:
                    raise ApiError(
                        409,
                        "UPLOAD_OFFSET_MISMATCH",
                        "Upload offset does not match the server",
                    )
            yield job
        finally:
            upload_lock.release()

    async def advance_upload(self, job: JobRecord, received: int) -> UploadView:
        async with self._lock:
            current = self._jobs.get(job.id)
            if current is not job or job.state is not JobState.UPLOADING:
                raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
            expected = job.expected_input_bytes
            if expected is None or job.input_bytes + received > expected:
                raise ApiError(413, "INPUT_TOO_LARGE", "Upload exceeds its declared size")
            job.input_bytes += received
            job.expires_at = datetime.now(UTC) + timedelta(
                seconds=self._settings.upload_session_ttl_seconds
            )
            return self._upload_snapshot(job)

    async def discard(self, job_id: str) -> None:
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is not None and current.accepted:
                return
            job = self._jobs.pop(job_id, None)
            if job:
                job.cancel_event.set()
        if job:
            await self._remove_record_directory(job)

    def retention_days(self, principal_id: str) -> int:
        principal_id = self._settings.owner_id(principal_id)
        configured = self._retention_preferences.get(principal_id)
        if configured is not None:
            return configured
        return max(
            1,
            min(
                90,
                (self._settings.result_ttl_seconds + 86_399) // 86_400,
            ),
        )

    def retention_seconds(self, principal_id: str) -> int:
        principal_id = self._settings.owner_id(principal_id)
        configured = self._retention_preferences.get(principal_id)
        if configured is not None:
            return configured * 86_400
        return self._settings.result_ttl_seconds

    async def get_retention_days(self, principal_id: str) -> int:
        async with self._lock:
            return self.retention_days(principal_id)

    async def set_retention_days(self, principal_id: str, retention_days: int) -> int:
        principal_id = self._settings.owner_id(principal_id)
        if not isinstance(retention_days, int) or isinstance(retention_days, bool):
            raise ApiError(422, "INVALID_REQUEST", "Request parameters are invalid")
        if not 1 <= retention_days <= 90:
            raise ApiError(422, "INVALID_REQUEST", "Request parameters are invalid")
        async with self._lock:
            updated = dict(self._retention_preferences)
            updated[principal_id] = retention_days
            try:
                await self._write_preferences(updated)
            except OSError as exc:
                raise ApiError(
                    507,
                    "INSUFFICIENT_STORAGE",
                    "History settings are unavailable",
                ) from exc
            self._retention_preferences = updated
            return retention_days

    async def history(
        self,
        principal_id: str,
        limit: int,
        offset: int,
    ) -> tuple[list[HistoryEntry], int]:
        expired: list[JobRecord] = []
        async with self._lock:
            now = datetime.now(UTC)
            for job in list(self._jobs.values()):
                if job.state in (JobState.READY, JobState.FAILED) and self._is_expired(job, now):
                    self._jobs.pop(job.id, None)
                    job.cancel_event.set()
                    expired.append(job)
            records = sorted(
                (
                    job
                    for job in self._jobs.values()
                    if self._settings.owner_id(job.principal_id)
                    == self._settings.owner_id(principal_id)
                    and job.state is not JobState.UPLOADING
                ),
                key=lambda job: job.created_at,
                reverse=True,
            )
            total = len(records)
            selected = records[offset : offset + limit]
            result = [self._history_entry(job) for job in selected]
        await asyncio.gather(*(self._remove_record_directory(job) for job in expired))
        return result, total

    async def get(self, job_id: str, principal_id: str) -> JobView | CompressionJobView:
        expired: JobRecord | None = None
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            if self._is_expired(job):
                self._jobs.pop(job.id, None)
                job.cancel_event.set()
                expired = job
            else:
                result = self._snapshot(job)
        if expired is not None:
            await self._remove_record_directory(expired)
            raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
        return result

    async def download(
        self,
        job_id: str,
        principal_id: str,
        compression: bool | None = None,
    ) -> tuple[Path, str, str]:
        expired: JobRecord | None = None
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            if compression is not None and job.is_compression is not compression:
                raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
            if self._is_expired(job):
                self._jobs.pop(job.id, None)
                job.cancel_event.set()
                expired = job
            elif job.state is not JobState.READY or not job.output_path or not job.output_metadata:
                raise ApiError(409, "JOB_NOT_READY", "Converted media is not ready")
            elif not job.output_path.is_file() or job.output_path.is_symlink():
                raise ApiError(410, "RESULT_EXPIRED", "Converted media has expired")
            else:
                return job.output_path, job.output_metadata.filename, job.output_metadata.media_type
        if expired is not None:
            await self._remove_record_directory(expired)
            raise ApiError(410, "RESULT_EXPIRED", "Converted media has expired")
        raise AssertionError("unreachable")

    async def delete(
        self,
        job_id: str,
        principal_id: str,
        compression: bool | None = None,
    ) -> None:
        async with self._lock:
            job = self._authorized_job(job_id, principal_id)
            if compression is not None and job.is_compression is not compression:
                raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
            self._jobs.pop(job_id)
            job.cancel_event.set()
            cancellation = self._estimate_cancellations.get(job_id)
            if cancellation:
                cancellation.set()
            task = self._estimate_tasks.get(job_id)
            if task:
                task.cancel()
            self._estimates.pop(job_id, None)

        if job.state is not JobState.PROCESSING:
            await self._remove_record_directory(job)

    def _authorized_job(self, job_id: str, principal_id: str) -> JobRecord:
        job = self._jobs.get(job_id)
        if job is None or (
            self._settings.owner_id(job.principal_id) != self._settings.owner_id(principal_id)
        ):
            raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
        return job

    def _snapshot(self, job: JobRecord) -> JobView | CompressionJobView:
        output = job.output_metadata
        if output is not None:
            output = output.model_copy(
                update={
                    "download_url": (
                        f"{self._settings.public_base_url}/v1/"
                        f"{'compressions' if job.is_compression else 'jobs'}/{job.id}/content"
                    )
                }
            )
        if job.is_compression:
            return CompressionJobView(
                id=job.id,
                state=job.state,
                created_at=job.created_at,
                expires_at=job.expires_at,
                status_url=f"{self._settings.public_base_url}/v1/compressions/{job.id}",
                input=job.input_metadata,
                output=output,
                error=job.error,
                progress=job.progress,
                compression=job.compression,
                options=job.compression_options,
                origin=job.origin,
                parent_id=job.parent_id,
            )
        assert job.options is not None
        return JobView(
            id=job.id,
            state=job.state,
            target=job.options.target,
            quality=job.options.quality,
            quality_percent=_quality_percent(job.options),
            resolution=job.options.resolution,
            audio=job.options.audio,
            created_at=job.created_at,
            expires_at=job.expires_at,
            status_url=f"{self._settings.public_base_url}/v1/jobs/{job.id}",
            input=job.input_metadata,
            output=output,
            error=job.error,
            progress=job.progress,
            origin=job.origin,
            parent_id=job.parent_id,
        )

    def _history_entry(self, job: JobRecord) -> HistoryEntry:
        return HistoryEntry(
            id=job.id,
            kind="compression" if job.is_compression else "conversion",
            source_filename=job.source_filename,
            completed_at=job.completed_at,
            job=self._snapshot(job),
        )

    @staticmethod
    def _is_expired(job: JobRecord, now: datetime | None = None) -> bool:
        return job.expires_at is not None and job.expires_at <= (now or datetime.now(UTC))

    def _upload_snapshot(self, job: JobRecord) -> UploadView:
        if (
            job.state is not JobState.UPLOADING
            or job.expected_input_bytes is None
            or job.expires_at is None
        ):
            raise ApiError(409, "UPLOAD_ALREADY_COMPLETED", "Upload is already complete")
        return UploadView(
            id=job.id,
            offset=job.input_bytes,
            length=job.expected_input_bytes,
            chunk_size=min(self._settings.max_chunk_bytes, self._settings.max_upload_bytes),
            upload_url=f"{self._settings.public_base_url}/v1/uploads/{job.id}",
            expires_at=job.expires_at,
        )

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id is None:
                    return

                await self._cancel_samples()

                async with self._lock:
                    job = self._jobs.get(job_id)
                    if job is None:
                        continue
                    job.state = JobState.PROCESSING
                    job.processing_started = time.monotonic()
                    job.progress = JobProgress(stage=JobProgressStage.INSPECTING)
                    self._idle.clear()

                try:
                    async with self._lock:
                        if self._jobs.get(job.id) is not job:
                            continue
                        await self._persist_pending(job)
                    async def callback(
                        stage: JobProgressStage,
                        percent: int | None,
                        record: JobRecord = job,
                    ) -> None:
                        await self._update_progress(record, stage, percent)

                    if job.is_compression:
                        compression_kwargs = (
                            {"options": job.compression_options}
                            if job.compression_options is not None else {}
                        )
                        result = await self._processor.compress(
                            job.input_path,
                            job.directory,
                            job.cancel_event,
                            callback,
                            **compression_kwargs,
                        )
                    else:
                        assert job.options is not None
                        result = await self._processor.convert(
                            job.input_path,
                            job.directory,
                            job.options,
                            job.cancel_event,
                            callback,
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
                if self._queue.empty():
                    self._idle.set()

    async def _finish_ready(self, job: JobRecord, result: ConversionResult) -> None:
        async with self._lock:
            current = self._jobs.get(job.id)
            if current is not job:
                remove = True
            else:
                job.input_metadata = result.input
                job.output_path = result.output_path
                job.output_metadata = OutputMetadata(
                    bytes=result.output_bytes,
                    filename=_output_filename(
                        job.source_filename,
                        result.filename,
                        "compressed" if job.is_compression else "converted",
                    ),
                    media_type=result.media_type,
                    download_url=(
                        f"{self._settings.public_base_url}/v1/"
                        f"{'compressions' if job.is_compression else 'jobs'}/{job.id}/content"
                    ),
                    width=result.width,
                    height=result.height,
                    duration_ms=result.duration_ms,
                )
                if isinstance(result, CompressionResult):
                    job.compression = result.compression
                job.progress = None
                job.completed_at = datetime.now(UTC)
                job.expires_at = datetime.now(UTC) + timedelta(
                    seconds=self.retention_seconds(job.principal_id)
                )
                job.state = JobState.READY
                try:
                    await self._persist_terminal_file(job)
                except Exception:
                    logger.error("Could not persist completed job %s", job.id)
                    await self._finish_persistence_failure_locked(job)
                else:
                    job.history_persisted = True
                remove = False
        if remove:
            await self._remove_directory(job.directory)
            return
        if job.history_persisted:
            with suppress(OSError):
                await self._remove_file(job.input_path)
                await self._remove_file(job.directory / self._PENDING_FILE_NAME)
        if job.state is JobState.READY:
            await self._emit("ready", job)

    async def _finish_failed(self, job: JobRecord, code: str, message: str) -> None:
        async with self._lock:
            current = self._jobs.get(job.id)
            if current is job:
                job.state = JobState.FAILED
                job.progress = None
                job.error = JobError(code=code, message=message)
                job.completed_at = datetime.now(UTC)
                job.expires_at = datetime.now(UTC) + timedelta(
                    seconds=self.retention_seconds(job.principal_id)
                )
                job.history_persisted = False
                try:
                    await self._persist_terminal_file(job)
                except Exception:
                    logger.error("Could not persist failed job %s", job.id)
                else:
                    job.history_persisted = True
                remove = False
            else:
                remove = True
        if remove:
            await self._remove_directory(job.directory)
            return
        if job.history_persisted:
            try:
                await self._remove_job_files(job.directory, keep_history=True)
            except OSError:
                logger.error("Could not clean failed job files %s", job.id)

    async def _finish_persistence_failure_locked(self, job: JobRecord) -> None:
        job.state = JobState.FAILED
        job.input_metadata = None
        job.output_metadata = None
        job.output_path = None
        job.compression = None
        job.progress = None
        job.error = JobError(
            code="RESULT_PERSISTENCE_FAILED",
            message="The converted media could not be saved",
        )
        job.completed_at = datetime.now(UTC)
        job.expires_at = datetime.now(UTC) + timedelta(
            seconds=self.retention_seconds(job.principal_id)
        )
        job.history_persisted = False
        try:
            await self._persist_terminal_file(job)
        except Exception:
            logger.error("Could not persist fallback failure for job %s", job.id)
        else:
            job.history_persisted = True
            try:
                await self._remove_job_files(job.directory, keep_history=True)
            except OSError:
                logger.error("Could not clean unpersisted job files %s", job.id)

    async def _finish_cancelled(self, job: JobRecord) -> None:
        if self._stopping and self._jobs.get(job.id) is job and job.accepted:
            return
        async with self._lock:
            self._jobs.pop(job.id, None)
        await self._remove_directory(job.directory)

    async def _update_progress(
        self,
        job: JobRecord,
        stage: JobProgressStage,
        percent: int | None,
    ) -> None:
        async with self._lock:
            if self._jobs.get(job.id) is not job or job.state is not JobState.PROCESSING:
                return
            if (
                stage is JobProgressStage.CONVERTING
                and job.progress is not None
                and job.progress.stage is JobProgressStage.CONVERTING
                and job.progress.percent is not None
                and percent is not None
            ):
                percent = max(job.progress.percent, percent)
            eta = None
            if percent and job.processing_started and percent < 100:
                elapsed = time.monotonic() - job.processing_started
                eta = max(0, round(elapsed * (100 - percent) / percent))
            job.progress = JobProgress(stage=stage, percent=percent, eta_seconds=eta)

    async def _persist_terminal_file(self, job: JobRecord) -> None:
        if job.state not in (JobState.READY, JobState.FAILED) or job.completed_at is None:
            raise ValueError("terminal job is incomplete")
        output_basename: str | None = None
        if job.state is JobState.READY:
            if job.output_path is None or job.output_metadata is None:
                raise ValueError("ready job has no output")
            try:
                relative = job.output_path.relative_to(job.directory)
            except ValueError as exc:
                raise ValueError("output is outside job directory") from exc
            if (
                len(relative.parts) != 1
                or relative.name in {"", ".", "..", self._HISTORY_FILE_NAME}
            ):
                raise ValueError("output path is not a basename")
            await asyncio.to_thread(
                _validate_output_file,
                job.output_path,
                job.output_metadata.bytes,
            )
            def sync_output() -> None:
                with job.output_path.open("rb") as output:
                    os.fsync(output.fileno())

            await asyncio.to_thread(sync_output)
            output_basename = relative.name
        record = {
            "version": 1,
            "id": job.id,
            "principal_id": job.principal_id,
            "kind": "compression" if job.is_compression else "conversion",
            "state": job.state.value,
            "created_at": job.created_at.isoformat(),
            "completed_at": job.completed_at.isoformat(),
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "source_filename": job.source_filename,
            "options": job.options.model_dump(mode="json") if job.options else None,
            "input": job.input_metadata.model_dump(mode="json") if job.input_metadata else None,
            "output": job.output_metadata.model_dump(mode="json") if job.output_metadata else None,
            "output_basename": output_basename,
            "error": job.error.model_dump(mode="json") if job.error else None,
            "compression": job.compression.model_dump(mode="json") if job.compression else None,
            "compression_options": (
                job.compression_options.model_dump(mode="json") if job.compression_options else None
            ),
            "origin": job.origin,
            "parent_id": job.parent_id,
        }
        await asyncio.to_thread(
            _atomic_write_json,
            job.directory / self._HISTORY_FILE_NAME,
            record,
        )

    async def _persist_pending(self, job: JobRecord) -> None:
        if job.input_path.is_symlink() or not job.input_path.is_file():
            raise ValueError("accepted source is unsafe")
        if job.input_path.stat().st_size != job.input_bytes or job.input_bytes <= 0:
            raise ValueError("accepted source size is inconsistent")

        def sync_source() -> None:
            with job.input_path.open("rb") as source:
                os.fsync(source.fileno())

        await asyncio.to_thread(sync_source)
        await asyncio.to_thread(_atomic_write_json, job.directory / self._PENDING_FILE_NAME, {
            "version": 1,
            "id": job.id,
            "principal_id": job.principal_id,
            "kind": "compression" if job.is_compression else "conversion",
            "state": job.state.value,
            "created_at": job.created_at.isoformat(),
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "source_filename": job.source_filename,
            "input_bytes": job.input_bytes,
            "options": job.options.model_dump(mode="json") if job.options else None,
            "compression_options": (
                job.compression_options.model_dump(mode="json") if job.compression_options else None
            ),
            "origin": job.origin,
            "parent_id": job.parent_id,
            "recovery_count": job.recovery_count,
        })

    async def _restore_pending(self, directory: Path) -> JobRecord | None:
        try:
            path = directory / self._PENDING_FILE_NAME
            if path.is_symlink() or not path.is_file():
                return None
            payload = await asyncio.to_thread(_read_json, path)
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return None
            if payload.get("id") != directory.name:
                return None
            principal = payload.get("principal_id")
            if not isinstance(principal, str) or not principal:
                return None
            if payload.get("kind") not in {"conversion", "compression"}:
                return None
            state = JobState(payload["state"])
            if state not in (JobState.AWAITING_REVIEW, JobState.QUEUED, JobState.PROCESSING):
                return None
            created = _aware_datetime(payload.get("created_at"))
            expires = _aware_datetime(payload.get("expires_at"))
            now = datetime.now(UTC)
            if created is None or created > now or (expires is not None and expires <= now):
                return None
            if state is JobState.AWAITING_REVIEW and expires is None:
                return None
            size = payload.get("input_bytes")
            if type(size) is not int or not 0 < size <= self._settings.max_upload_bytes:
                return None
            await asyncio.to_thread(_validate_output_file, directory / "input", size)
            recoveries = payload.get("recovery_count", 0)
            if type(recoveries) is not int or not 0 <= recoveries <= 3:
                return None
            compression = payload["kind"] == "compression"
            options = _model_or_none(ConversionOptions, payload.get("options"))
            if not compression and options is None:
                return None
            source_name = payload.get("source_filename")
            parent_id = payload.get("parent_id")
            if parent_id is not None and (
                not isinstance(parent_id, str) or str(uuid.UUID(parent_id)) != parent_id
            ):
                return None
            job = JobRecord(
                id=directory.name, principal_id=principal, options=options,
                directory=directory, input_path=directory / "input", created_at=created,
                state=state, input_bytes=size, expected_input_bytes=size,
                is_compression=compression, source_filename=_safe_source_name(source_name),
                expires_at=expires, accepted=True, recovery_count=recoveries,
                compression_options=_model_or_none(
                    CompressionOptions, payload.get("compression_options")
                ), origin=str(payload.get("origin", "api"))[:32], parent_id=parent_id,
            )
            if state is JobState.PROCESSING:
                job.recovery_count += 1
                if job.recovery_count > 2:
                    await self._remove_job_files(directory)
                    job.state = JobState.FAILED
                    job.error = JobError(
                        code="RECOVERY_LIMIT_EXCEEDED",
                        message="Processing was interrupted repeatedly; start a new job",
                    )
                    job.completed_at = now
                    job.expires_at = now + timedelta(seconds=self.retention_seconds(principal))
                    await self._persist_terminal_file(job)
                    job.history_persisted = True
                    return job
                job.state = JobState.QUEUED
                job.progress = JobProgress(stage=JobProgressStage.QUEUED)
                await self._persist_pending(job)
            children = await asyncio.to_thread(lambda: list(directory.iterdir()))
            await asyncio.gather(*(
                self._remove_path(child) for child in children
                if child.name not in {"input", self._PENDING_FILE_NAME}
            ))
            return job
        except (OSError, ValueError, TypeError, KeyError):
            return None

    async def _load_preferences(self) -> None:
        if (
            self._preferences_dir.is_symlink()
            or not self._preferences_dir.is_dir()
            or self._preferences_path.is_symlink()
        ):
            self._retention_preferences = {}
            return
        try:
            payload = await asyncio.to_thread(_read_json, self._preferences_path)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            self._retention_preferences = {}
            return
        if not isinstance(payload, dict):
            self._retention_preferences = {}
            return
        if isinstance(payload.get("retention_days"), dict):
            payload = payload["retention_days"]
        preferences: dict[str, int] = {}
        for principal, value in payload.items():
            if (
                isinstance(principal, str)
                and principal
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= 90
            ):
                preferences[principal] = value
        self._retention_preferences = preferences

    async def _write_preferences(self, preferences: dict[str, int]) -> None:
        if self._preferences_dir.is_symlink() or self._preferences_path.is_symlink():
            raise OSError("history preference path is unsafe")
        await asyncio.to_thread(
            _atomic_write_json,
            self._preferences_path,
            {"version": 1, "retention_days": preferences},
            0o600,
        )

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
            await asyncio.gather(*(self._remove_record_directory(job) for job in expired))

    async def _remove_orphans(self) -> list[JobRecord]:
        children = await asyncio.to_thread(lambda: list(self._settings.work_dir.iterdir()))
        restored: list[JobRecord] = []
        for path in children:
            if path.name in {self._HISTORY_DIR_NAME, ".integrations"} and (
                path.is_dir() and not path.is_symlink()
            ):
                continue
            try:
                directory_uuid = uuid.UUID(path.name)
            except (ValueError, AttributeError):
                await self._remove_path(path)
                continue
            if not path.is_dir() or path.is_symlink() or str(directory_uuid) != path.name:
                await self._remove_path(path)
                continue
            job = await self._restore_record(path)
            if job is None:
                job = await self._restore_pending(path)
            if job is None:
                await self._remove_path(path)
            else:
                restored.append(job)
        return restored

    async def _restore_record(self, directory: Path) -> JobRecord | None:
        try:
            record_path = directory / self._HISTORY_FILE_NAME
            if record_path.is_symlink() or not record_path.is_file():
                return None
            payload = await asyncio.to_thread(_read_json, record_path)
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return None
            job_id = payload.get("id")
            if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != directory.name:
                return None
            principal_id = payload.get("principal_id")
            kind = payload.get("kind")
            state_value = payload.get("state")
            if not isinstance(principal_id, str) or not principal_id:
                return None
            if kind not in {"conversion", "compression"}:
                return None
            if state_value not in {JobState.READY.value, JobState.FAILED.value}:
                return None
            created_at = _aware_datetime(payload.get("created_at"))
            completed_at = _aware_datetime(payload.get("completed_at"))
            expires_at = _aware_datetime(payload.get("expires_at"))
            if created_at is None or completed_at is None or expires_at is None:
                return None
            now = datetime.now(UTC)
            if created_at > completed_at or completed_at > expires_at or expires_at <= now:
                return None
            source_filename = payload.get("source_filename")
            if source_filename is not None and not isinstance(source_filename, str):
                return None
            options = (
                ConversionOptions.model_validate(payload["options"])
                if kind == "conversion" and isinstance(payload.get("options"), dict)
                else None
            )
            if kind == "conversion" and options is None:
                return None
            if kind == "compression" and payload.get("options") is not None:
                return None
            input_metadata = _model_or_none(MediaMetadata, payload.get("input"))
            output_metadata = _model_or_none(OutputMetadata, payload.get("output"))
            error = _model_or_none(JobError, payload.get("error"))
            compression = _model_or_none(CompressionMetadata, payload.get("compression"))
            state = JobState(state_value)
            if state is JobState.READY:
                if input_metadata is None or error is not None:
                    return None
                if kind == "compression" and compression is None:
                    return None
                if kind == "conversion" and compression is not None:
                    return None
            elif input_metadata is not None or error is None:
                return None
            output_path: Path | None = None
            output_basename = payload.get("output_basename")
            if state is JobState.READY:
                if (
                    not isinstance(output_basename, str)
                    or not output_basename
                    or Path(output_basename).name != output_basename
                    or output_basename in {".", ".."}
                    or "/" in output_basename
                    or "\\" in output_basename
                    or output_basename == self._HISTORY_FILE_NAME
                    or output_metadata is None
                ):
                    return None
                output_path = directory / output_basename
                await asyncio.to_thread(
                    _validate_output_file,
                    output_path,
                    output_metadata.bytes,
                )
            elif (
                output_basename is not None
                or output_metadata is not None
                or compression is not None
            ):
                return None
            children = await asyncio.to_thread(lambda: list(directory.iterdir()))
            keep = {self._HISTORY_FILE_NAME}
            if output_path is not None:
                keep.add(output_path.name)
            await asyncio.gather(
                *(self._remove_path(child) for child in children if child.name not in keep)
            )
            return JobRecord(
                id=job_id,
                principal_id=principal_id,
                options=options,
                directory=directory,
                input_path=directory / "input",
                created_at=created_at,
                state=state,
                input_metadata=input_metadata,
                output_metadata=output_metadata,
                output_path=output_path,
                error=error,
                source_filename=source_filename,
                is_compression=kind == "compression",
                compression=compression,
                expires_at=expires_at,
                completed_at=completed_at,
                history_persisted=True,
                accepted=True,
                compression_options=_model_or_none(
                    CompressionOptions, payload.get("compression_options")
                ),
                origin=payload.get("origin", "api"),
                parent_id=payload.get("parent_id"),
            )
        except (OSError, ValueError, TypeError, KeyError):
            return None

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
    async def _remove_record_directory(cls, job: JobRecord) -> None:
        if job.state is JobState.UPLOADING:
            async with job.upload_lock:
                await cls._remove_directory(job.directory)
        else:
            await cls._remove_directory(job.directory)

    @classmethod
    async def _remove_job_files(cls, directory: Path, *, keep_history: bool = False) -> None:
        if not directory.exists():
            return
        children = list(directory.iterdir())
        await asyncio.gather(*(
            cls._remove_path(path) for path in children
            if not keep_history or path.name != cls._HISTORY_FILE_NAME
        ))

    @staticmethod
    async def _remove_file(path: Path) -> None:
        await asyncio.to_thread(path.unlink, missing_ok=True)


def _quality_percent(options: ConversionOptions) -> int:
    if options.quality_percent is not None:
        return options.quality_percent
    return {"economy": 0, "balanced": 50, "high": 100}[options.quality.value]


def _safe_source_name(value: str | None) -> str | None:
    if not value:
        return None
    name = value.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    name = "".join(character for character in name if ord(character) >= 32).strip(" .")
    return name[:180] or None


def _output_filename(source: str | None, fallback: str, suffix: str) -> str:
    extension = fallback.rsplit(".", maxsplit=1)[-1]
    if not source:
        return fallback
    stem = source.rsplit(".", maxsplit=1)[0] if "." in source else source
    return f"{stem.rstrip(' .') or 'media'}-{suffix}.{extension}"


def _read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def _atomic_write_json(path: Path, payload: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            descriptor = -1
            json.dump(payload, destination, separators=(",", ":"), ensure_ascii=False)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _model_or_none(model: type, value: object):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid history model")
    return model.model_validate(value)


def _validate_output_file(path: Path, expected_bytes: int) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("output is not a regular file")
    if path.stat().st_size != expected_bytes:
        raise ValueError("output size is inconsistent")
