from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any

import httpx

from media_manager.errors import ApiError
from media_manager.integration_models import (
    DownloadRequest,
    DownloadState,
    DownloadView,
    DownloadWorkflow,
)
from media_manager.models import (
    CompressionOptions,
    ConversionOptions,
    JobError,
    OutputMetadata,
)

logger = logging.getLogger(__name__)
_MAX_NAME = 180
_POLL_INTERVAL_SECONDS = 2
_POLL_TIMEOUT_SECONDS = 15
_HEALTH_TIMEOUT_SECONDS = 2
_HEALTH_CACHE_SECONDS = 10
_AUTO_PROCESS_RETRY_SECONDS = 2


@dataclass(slots=True)
class _Download:
    id: str
    owner: str
    state: DownloadState
    created_at: datetime
    workflow: DownloadWorkflow
    directory: Path
    origin: str = "api"
    source_filename: str | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    output: OutputMetadata | None = None
    error: JobError | None = None
    child_id: str | None = None
    submission_started: bool = False
    auto_process: bool = False
    quality: str = "best"
    format: str = "ios"
    download_type: str = "video"
    codec: str = "h264"
    conversion_options: ConversionOptions | None = None
    compression_options: CompressionOptions | None = None
    idempotency_key: str | None = None
    submission_unknown: bool = False


class DownloadManager:
    """Durable MeTube bridge. URL input is deliberately never written to disk."""

    def __init__(
        self,
        settings: Any,
        jobs: Any,
        processor: Any,
        event_sink: Any | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.jobs = jobs
        self.processor = processor
        self.event_sink = event_sink
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._records: dict[str, _Download] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._auto_process_tasks: dict[str, asyncio.Task[None]] = {}
        self._submission_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._registry_lock = asyncio.Lock()
        self._submission_locks: dict[str, asyncio.Lock] = {}
        self._stopping = False
        self._janitor_task: asyncio.Task[None] | None = None
        self._health_lock = asyncio.Lock()
        self._health_checked_at: float | None = None
        self._health_available = False

    @property
    def _base_dir(self) -> Path:
        return Path(self.settings.work_dir) / ".integrations"

    @property
    def _downloads_dir(self) -> Path:
        return self._base_dir / "downloads"

    @property
    def _registry(self) -> Path:
        return self._base_dir / "downloads.json"

    @property
    def _sidecar_jobs_path(self) -> str:
        return "/internal/jobs" if self.settings.metube_sidecar_api_key else "/jobs"

    @property
    def _sidecar_file_suffix(self) -> str:
        return "download" if self.settings.metube_sidecar_api_key else "file"

    async def start(self) -> None:
        self._stopping = False
        self._health_checked_at = None
        self._health_available = False
        sidecar_url = self.settings.metube_sidecar_url
        if sidecar_url:
            self._base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._downloads_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._base_dir.is_symlink() or self._downloads_dir.is_symlink():
            raise RuntimeError("Integration storage path is unsafe")
        if sidecar_url:
            headers = {}
            if self.settings.metube_sidecar_api_key:
                headers["X-Sidecar-Bridge-Key"] = self.settings.metube_sidecar_api_key
            self._client = httpx.AsyncClient(
                base_url=sidecar_url.rstrip("/"),
                headers=headers,
                timeout=httpx.Timeout(_POLL_TIMEOUT_SECONDS, connect=5),
                follow_redirects=False,
                transport=self._transport,
            )
        async with self._lock:
            self._records = await asyncio.to_thread(self._load_registry)
        for record in self._records.values():
            partial = record.directory / ".output.tmp"
            if (
                not self._downloads_dir.is_symlink()
                and not record.directory.is_symlink()
                and record.directory == self._downloads_dir / record.id
                and not partial.is_symlink()
            ):
                partial.unlink(missing_ok=True)
        await self._cleanup_expired()
        for record in list(self._records.values()):
            if (
                self._client
                and record.output is None
                and record.state in {DownloadState.QUEUED, DownloadState.DOWNLOADING}
            ):
                if record.state is DownloadState.QUEUED and not record.submission_started:
                    # The source URL is intentionally not persisted, so a record
                    # accepted just before a crash cannot safely be resubmitted.
                    self._fail(record, "Download was interrupted before submission; resubmit it")
                    await self._save()
                else:
                    self._spawn(record)
            elif record.workflow is not DownloadWorkflow.DOWNLOAD and record.output is not None:
                if not record.child_id:
                    await self._recover_child(record)
                if record.auto_process:
                    self._spawn_auto_process(record)
        self._janitor_task = asyncio.create_task(
            self._janitor(), name="integration-download-janitor"
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._janitor_task:
            self._janitor_task.cancel()
            await asyncio.gather(self._janitor_task, return_exceptions=True)
            self._janitor_task = None
        tasks = list(self._tasks.values())
        submissions = list(self._submission_tasks.values())
        auto_process_tasks = list(self._auto_process_tasks.values())
        for task in tasks:
            task.cancel()
        for task in submissions:
            task.cancel()
        for task in auto_process_tasks:
            task.cancel()
        if tasks or submissions or auto_process_tasks:
            await asyncio.gather(*tasks, *submissions, *auto_process_tasks, return_exceptions=True)
        self._tasks.clear()
        self._submission_tasks.clear()
        self._auto_process_tasks.clear()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def available(self) -> bool:
        """Return cached sidecar health, probing its fixed endpoint at most every ten seconds."""
        client = self._client
        if client is None:
            return False
        loop = asyncio.get_running_loop()
        now = loop.time()
        if (
            self._health_checked_at is not None
            and now - self._health_checked_at < _HEALTH_CACHE_SECONDS
        ):
            return self._health_available
        async with self._health_lock:
            now = loop.time()
            if (
                self._health_checked_at is not None
                and now - self._health_checked_at < _HEALTH_CACHE_SECONDS
            ):
                return self._health_available
            available = False
            try:
                async with asyncio.timeout(_HEALTH_TIMEOUT_SECONDS):
                    response = await client.get(
                        "/healthz",
                        timeout=httpx.Timeout(
                            _HEALTH_TIMEOUT_SECONDS, connect=_HEALTH_TIMEOUT_SECONDS
                        ),
                        follow_redirects=False,
                    )
                    available = response.is_success
                    await response.aclose()
            except Exception:
                available = False
            self._health_available = available
            self._health_checked_at = loop.time()
            return available

    def _update_health(self, available: bool) -> None:
        self._health_available = available
        self._health_checked_at = asyncio.get_running_loop().time()

    async def create(
        self,
        request: DownloadRequest | dict[str, Any],
        owner: str,
        *,
        idempotency_key: str | None = None,
        origin: str = "api",
    ) -> DownloadView:
        request = (
            request
            if isinstance(request, DownloadRequest)
            else DownloadRequest.model_validate(request)
        )
        if not owner or len(owner) > 256:
            raise ApiError(422, "INVALID_REQUEST", "Request parameters are invalid")
        if not isinstance(origin, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", origin):
            raise ApiError(422, "INVALID_REQUEST", "Request parameters are invalid")
        owner = self._canonical_owner(owner)
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 160
        ):
            raise ApiError(422, "INVALID_REQUEST", "Request parameters are invalid")
        async with self._lock:
            if self._client is None:
                raise ApiError(503, "INTEGRATION_UNAVAILABLE", "Downloads are unavailable")
            record = self._find_idempotent_record(owner, idempotency_key)
        if record is None and not await self.available():
            async with self._lock:
                record = self._find_idempotent_record(owner, idempotency_key)
            if record is None:
                raise ApiError(503, "INTEGRATION_UNAVAILABLE", "Downloads are unavailable")
        async with self._lock:
            if self._client is None:
                raise ApiError(503, "INTEGRATION_UNAVAILABLE", "Downloads are unavailable")
            record = self._find_idempotent_record(owner, idempotency_key)
            if record is None:
                active = sum(
                    item.state in {DownloadState.QUEUED, DownloadState.DOWNLOADING}
                    for item in self._records.values()
                )
                if active >= getattr(self.settings, "max_live_jobs", 1):
                    raise ApiError(
                        429,
                        "QUEUE_FULL",
                        "The download queue is full",
                        headers={"Retry-After": "10"},
                    )
                required = self.settings.max_output_bytes * (active + 1)
                try:
                    free_bytes = shutil.disk_usage(self.settings.work_dir).free
                except OSError as exc:
                    raise ApiError(
                        507,
                        "INSUFFICIENT_STORAGE",
                        "Download workspace is unavailable",
                    ) from exc
                if free_bytes < required:
                    raise ApiError(
                        507,
                        "INSUFFICIENT_STORAGE",
                        "There is not enough workspace for another download",
                    )
                record_id = str(uuid.uuid4())
                now = datetime.now(UTC)
                record = _Download(
                    id=record_id,
                    owner=owner,
                    state=DownloadState.QUEUED,
                    created_at=now,
                    workflow=request.workflow,
                    directory=self._downloads_dir / record_id,
                    origin=origin,
                    auto_process=request.auto_process,
                    quality=request.quality,
                    format=request.format,
                    download_type=request.download_type.value,
                    codec=request.codec,
                    conversion_options=request.conversion_options,
                    compression_options=request.compression_options,
                    idempotency_key=idempotency_key,
                )
                record.directory.mkdir(mode=0o700)
                self._records[record.id] = record
                await self._save()
        task = self._submission_tasks.get(record.id)
        if task is None:
            task = asyncio.create_task(
                self._submit_and_monitor(record, request),
                name=f"metube-submit-{record.id}",
            )
            self._submission_tasks[record.id] = task
            task.add_done_callback(
                lambda finished, key=record.id: self._clear_submission_task(key, finished)
            )
        await asyncio.shield(task)
        return self._view(record)

    def _find_idempotent_record(
        self, owner: str, idempotency_key: str | None
    ) -> _Download | None:
        if idempotency_key is None:
            return None
        return next(
            (
                item
                for item in self._records.values()
                if item.idempotency_key == idempotency_key
                and self._canonical_owner(item.owner) == owner
            ),
            None,
        )

    def _clear_submission_task(self, record_id: str, task: asyncio.Task[None]) -> None:
        if self._submission_tasks.get(record_id) is task:
            self._submission_tasks.pop(record_id, None)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.warning("MeTube submission task failed (%s)", type(error).__name__)

    async def _submit_and_monitor(self, record: _Download, request: DownloadRequest) -> None:
        try:
            if not record.submission_started and record.state is DownloadState.QUEUED:
                await self._submit(record, request)
        finally:
            self._spawn(record)

    async def get(self, download_id: str, owner: str) -> DownloadView:
        owner = self._canonical_owner(owner)
        record = self._owned(download_id, owner)
        if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
            await self.delete(download_id, owner)
            raise ApiError(404, "DOWNLOAD_NOT_FOUND", "Download was not found")
        await self._sync_child_state(record)
        return self._view(record)

    async def history(
        self, owner: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        owner = self._canonical_owner(owner)
        if not 1 <= limit <= 100 or offset < 0:
            raise ApiError(422, "INVALID_REQUEST", "Request parameters are invalid")
        now = datetime.now(UTC)
        async with self._lock:
            expired = [
                r.id
                for r in self._records.values()
                if self._canonical_owner(r.owner) == owner and r.expires_at and r.expires_at <= now
            ]
        for item_id in expired:
            await self.delete(item_id, owner)
        records = sorted(
            (
                r
                for r in self._records.values()
                if self._canonical_owner(r.owner) == owner
                and r.state
                in {
                    DownloadState.QUEUED,
                    DownloadState.DOWNLOADING,
                    DownloadState.PROCESSING,
                    DownloadState.AWAITING_REVIEW,
                    DownloadState.READY,
                    DownloadState.FAILED,
                }
            ),
            key=lambda item: item.created_at,
            reverse=True,
        )
        total = len(records)
        page = records[offset : offset + limit]
        for record in page:
            if record.child_id is not None:
                await self._sync_child_state(record)
        return [self._history_view(record) for record in page], total

    async def download(self, download_id: str, owner: str) -> tuple[Path, str, str]:
        owner = self._canonical_owner(owner)
        record = self._owned(download_id, owner)
        if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
            await self.delete(download_id, owner)
            raise ApiError(404, "DOWNLOAD_NOT_FOUND", "Download was not found")
        if record.state not in {
            DownloadState.READY,
            DownloadState.PROCESSING,
            DownloadState.AWAITING_REVIEW,
        } or record.output is None:
            raise ApiError(409, "DOWNLOAD_NOT_READY", "Download is not ready")
        file_path = record.directory / "output"
        if (
            self._downloads_dir.is_symlink()
            or record.directory.is_symlink()
            or record.directory != self._downloads_dir / record.id
            or file_path.is_symlink()
            or not file_path.is_file()
            or file_path.stat().st_size != record.output.bytes
        ):
            raise ApiError(410, "RESULT_EXPIRED", "Downloaded media has expired")
        return file_path, record.output.filename, record.output.media_type

    async def delete(self, download_id: str, owner: str) -> None:
        owner = self._canonical_owner(owner)
        async with self._lock:
            record = self._records.get(download_id)
            if record is None or self._canonical_owner(record.owner) != owner:
                raise ApiError(404, "DOWNLOAD_NOT_FOUND", "Download was not found")
            self._records.pop(download_id, None)
            task = self._tasks.pop(download_id, None)
            auto_process_task = self._auto_process_tasks.pop(download_id, None)
        await self._save()
        tasks = [running for running in (task, auto_process_task) if running is not None]
        for running in tasks:
            if running is not asyncio.current_task():
                running.cancel()
        if tasks:
            await asyncio.gather(
                *(running for running in tasks if running is not asyncio.current_task()),
                return_exceptions=True,
            )
        if (
            not self._downloads_dir.is_symlink()
            and record.directory == self._downloads_dir / record.id
        ):
            await asyncio.to_thread(_remove_tree, record.directory)

    async def process(
        self,
        download_id: str,
        owner: str,
        conversion_options: ConversionOptions | None = None,
        compression_options: CompressionOptions | None = None,
        auto_start: bool = False,
    ) -> Any:
        owner = self._canonical_owner(owner)
        record = self._owned(download_id, owner)
        if record.state not in {DownloadState.READY, DownloadState.AWAITING_REVIEW}:
            raise ApiError(409, "DOWNLOAD_NOT_READY", "Downloaded media is not ready")
        return await self._process_record(
            record,
            conversion_options,
            compression_options,
            auto_start=auto_start,
            retry_terminal=True,
        )

    def _owned(self, record_id: str, owner: str) -> _Download:
        record = self._records.get(record_id)
        if record is None or self._canonical_owner(record.owner) != self._canonical_owner(owner):
            raise ApiError(404, "DOWNLOAD_NOT_FOUND", "Download was not found")
        return record

    def _canonical_owner(self, owner: str) -> str:
        resolver = getattr(self.settings, "owner_id", None)
        return resolver(owner) if callable(resolver) else owner

    def _spawn(self, record: _Download) -> None:
        if (
            record.id in self._tasks
            or self._stopping
            or record.state not in {DownloadState.QUEUED, DownloadState.DOWNLOADING}
        ):
            return
        task = asyncio.create_task(self._run(record), name=f"metube-{record.id}")
        self._tasks[record.id] = task
        task.add_done_callback(lambda _task, key=record.id: self._tasks.pop(key, None))

    def _spawn_auto_process(self, record: _Download) -> None:
        if record.id in self._auto_process_tasks or self._stopping:
            return
        task = asyncio.create_task(
            self._auto_process_until_expiry(record), name=f"auto-process-{record.id}"
        )
        self._auto_process_tasks[record.id] = task
        task.add_done_callback(
            lambda _task, key=record.id: self._auto_process_tasks.pop(key, None)
        )

    async def _auto_process_until_expiry(self, record: _Download) -> None:
        while not self._stopping:
            expires_at = record.expires_at
            if expires_at is None or expires_at <= datetime.now(UTC):
                self._preserve_source_after_processing_error(
                    record, TimeoutError("source retention expired before automatic processing")
                )
                await self._save()
                await self._emit("download_ready", record)
                return
            try:
                await self._process_record(
                    record,
                    record.conversion_options,
                    record.compression_options,
                    auto_start=True,
                )
            except ApiError as exc:
                if exc.code != "QUEUE_FULL":
                    self._preserve_source_after_processing_error(record, exc)
                    await self._save()
                    await self._emit("download_ready", record)
                    return
                delay = _retry_after_seconds(exc)
            except Exception as exc:
                self._preserve_source_after_processing_error(record, exc)
                await self._save()
                await self._emit("download_ready", record)
                return
            else:
                if record.state in {DownloadState.READY, DownloadState.AWAITING_REVIEW}:
                    await self._save()
                    await self._emit("download_ready", record)
                    return
                delay = _AUTO_PROCESS_RETRY_SECONDS
            remaining = max(0.0, (expires_at - datetime.now(UTC)).total_seconds())
            if remaining <= 0:
                continue
            await asyncio.sleep(min(delay, remaining))

    async def _submit(self, record: _Download, request: DownloadRequest) -> None:
        submission_lock = self._submission_locks.setdefault(record.id, asyncio.Lock())
        async with submission_lock:
            if record.submission_started:
                return
            record.submission_started = True
            await self._save()
            try:
                response = await self._call(
                    "POST",
                    self._sidecar_jobs_path,
                    json={
                        "request_id": record.id,
                        "url": str(request.url),
                        "quality": request.quality,
                        "format": request.format,
                        "download_type": request.download_type.value,
                        "codec": request.codec,
                    },
                )
                with suppress(Exception):
                    body = response.json()
                    if isinstance(body, dict):
                        record.submission_unknown = body.get("submission_unknown") is True
                self._update_health(True)
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    self._update_health(True)
                    self._fail(record, "The source service rejected this download request")
                    record.error = JobError(
                        code="DOWNLOAD_REJECTED",
                        message="The source service rejected this download request",
                    )
                else:
                    self._update_health(False)
                    record.submission_unknown = True
            except Exception as exc:
                self._update_health(False)
                logger.info("MeTube submission outcome uncertain (%s)", type(exc).__name__)
                record.submission_unknown = True
            await self._save()

    async def _run(self, record: _Download) -> None:
        try:
            deadline = asyncio.get_running_loop().time() + max(
                60, getattr(self.settings, "upload_timeout_seconds", 300)
            )
            while not self._stopping:
                try:
                    status = await self._upstream_status(record.id)
                except Exception as exc:
                    self._update_health(False)
                    if asyncio.get_running_loop().time() >= deadline:
                        raise
                    logger.info("MeTube status lookup will retry (%s)", type(exc).__name__)
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                    continue
                self._update_health(True)
                state = str(status.get("status", "")).lower()
                if isinstance(status.get("submission_unknown"), bool):
                    record.submission_unknown = status["submission_unknown"]
                elif state in {
                    "queued",
                    "downloading",
                    "complete",
                    "completed",
                    "finished",
                    "done",
                }:
                    record.submission_unknown = False
                if state in {"failed", "error", "cancelled"}:
                    self._fail(record, "The source could not be downloaded")
                    await self._save()
                    return
                if state in {"complete", "completed", "finished", "done"}:
                    break
                record.state = (
                    DownloadState.DOWNLOADING if state == "downloading" else DownloadState.QUEUED
                )
                await self._save()
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            if self._stopping:
                return
            status = await self._upstream_status(record.id)
            await self._copy_result(record, status)
            request_workflow = record.workflow
            if request_workflow is DownloadWorkflow.DOWNLOAD:
                record.state = DownloadState.READY
            elif record.auto_process:
                record.state = DownloadState.PROCESSING
                await self._save()
                self._spawn_auto_process(record)
                return
            else:
                record.state = DownloadState.AWAITING_REVIEW
            await self._save()
            await self._emit("download_ready", record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("MeTube download %s failed (%s)", record.id, type(exc).__name__)
            if record.submission_unknown:
                record.state = DownloadState.FAILED
                record.completed_at = datetime.now(UTC)
                record.expires_at = record.completed_at + timedelta(
                    seconds=self.jobs.retention_seconds(record.owner)
                )
                record.error = JobError(
                    code="SIDECAR_OUTCOME_UNKNOWN",
                    message=(
                        "The source service submission could not be confirmed. "
                        "Check its status before retrying."
                    ),
                )
                await self._save()
                await self._emit("download_failed", record)
                return
            if record.output is not None:
                self._preserve_source_after_processing_error(record, exc)
                await self._save()
                await self._emit("download_ready", record)
                return
            self._fail(record, "The source could not be downloaded")
            await self._save()
            await self._emit("download_failed", record)

    async def _upstream_status(self, record_id: str) -> dict[str, Any]:
        response = await self._call("GET", f"{self._sidecar_jobs_path}/{record_id}")
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Invalid sidecar response")
        return body

    async def _copy_result(self, record: _Download, status: dict[str, Any]) -> None:
        response = await self._call(
            "GET",
            f"{self._sidecar_jobs_path}/{record.id}/{self._sidecar_file_suffix}",
            stream=True,
        )
        disposition = Message()
        disposition["content-disposition"] = response.headers.get("content-disposition", "")
        raw_name = disposition.get_filename()
        name = _safe_filename(raw_name or status.get("filename") or f"{record.id}.media")
        media_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[
            0
        ]
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type):
            media_type = "application/octet-stream"
        limit = min(self.settings.max_upload_bytes, self.settings.max_output_bytes)
        declared_size = response.headers.get("content-length")
        destination = record.directory / "output"
        temp = record.directory / ".output.tmp"
        total = 0
        try:
            if (
                self._downloads_dir.is_symlink()
                or record.directory.is_symlink()
                or record.directory != self._downloads_dir / record.id
            ):
                raise ValueError("Integration result path is unsafe")
            if declared_size is not None:
                expected_size = int(declared_size)
                if expected_size < 0 or expected_size > limit:
                    raise ValueError("Sidecar result exceeds configured size limit")
                if shutil.disk_usage(self.settings.work_dir).free < expected_size:
                    raise OSError("Insufficient free space for the downloaded file")
            with temp.open("xb") as stream:
                os.chmod(temp, 0o600)
                async for chunk in response.aiter_bytes(128 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("Sidecar result exceeds configured size limit")
                    if shutil.disk_usage(self.settings.work_dir).free < len(chunk):
                        raise OSError("Insufficient free space for the downloaded file")
                    stream.write(chunk)
                if declared_size is not None and int(declared_size) != total:
                    raise ValueError("Sidecar result length did not match its declaration")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, destination)
            directory_fd = os.open(
                record.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            record.source_filename = name
            record.output = OutputMetadata(
                bytes=total,
                filename=name,
                media_type=media_type,
                download_url=f"{self.settings.public_base_url}/v1/downloads/{record.id}/content",
            )
            record.completed_at = datetime.now(UTC)
            record.expires_at = record.completed_at + timedelta(
                seconds=self.jobs.retention_seconds(record.owner)
            )
        finally:
            await response.aclose()
            if not self._downloads_dir.is_symlink() and not record.directory.is_symlink():
                temp.unlink(missing_ok=True)

    async def _process_record(
        self,
        record: _Download,
        conversion_options: ConversionOptions | None,
        compression_options: CompressionOptions | None,
        *,
        auto_start: bool,
        retry_terminal: bool = False,
    ) -> Any:
        if record.child_id:
            try:
                child = await self.jobs.get(record.child_id, record.owner)
            except ApiError as exc:
                if not retry_terminal or exc.code != "JOB_NOT_FOUND":
                    raise
            else:
                child_state = getattr(child.state, "value", child.state)
                if child_state in {"awaiting_review", "queued", "processing"}:
                    if auto_start:
                        return await self._start_child(
                            child.id, record, conversion_options, compression_options
                        )
                    return child
                if not retry_terminal:
                    if child_state == "ready":
                        record.state = DownloadState.READY
                        record.error = None
                        await self._save()
                    elif child_state == "failed":
                        self._preserve_source_after_processing_error(
                            record, RuntimeError("previous child processing failed")
                        )
                        await self._save()
                    return child
        path = record.directory / "output"
        if conversion_options is None and compression_options is None:
            conversion_options = record.conversion_options
            compression_options = record.compression_options
        if conversion_options is not None and compression_options is not None:
            workflow = DownloadWorkflow.CONVERT_COMPRESS
        elif compression_options is not None:
            workflow = DownloadWorkflow.COMPRESS
        elif conversion_options is not None:
            workflow = DownloadWorkflow.CONVERT
        else:
            workflow = record.workflow
        if workflow is DownloadWorkflow.COMPRESS:
            conversion_options = None
            compression_options = compression_options or CompressionOptions()
        elif workflow is DownloadWorkflow.CONVERT:
            compression_options = None
            if conversion_options is None:
                raise ApiError(422, "INVALID_REQUEST", "Conversion options are required")
        elif workflow is DownloadWorkflow.DOWNLOAD:
            raise ApiError(422, "INVALID_REQUEST", "Select conversion or compression options")
        elif workflow is DownloadWorkflow.CONVERT_COMPRESS:
            if conversion_options is None or compression_options is None:
                raise ApiError(
                    422, "INVALID_REQUEST", "Conversion and compression options are required"
                )
        record.workflow = workflow
        record.conversion_options = conversion_options
        record.compression_options = compression_options
        record.error = None
        await self._save()
        child = await self.jobs.find_child(record.id, record.owner, active_only=True)
        if child is None:
            child = await self.jobs.prepare_from_path(
                path,
                record.owner,
                filename=record.source_filename or "download.media",
                conversion_options=conversion_options,
                compression_options=compression_options,
                parent_id=record.id,
                origin=record.origin,
                auto_start=False,
            )
        record.child_id = child.id
        record.state = DownloadState.PROCESSING if auto_start else DownloadState.AWAITING_REVIEW
        await self._save()
        if auto_start:
            child = await self._start_child(
                child.id, record, conversion_options, compression_options
            )
        return child

    async def _start_child(
        self,
        child_id: str,
        record: _Download,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(
            60, getattr(self.settings, "upload_timeout_seconds", 300)
        )
        if record.expires_at is not None:
            retention = (record.expires_at - datetime.now(UTC)).total_seconds()
            deadline = min(deadline, loop.time() + max(0, retention))
        while True:
            try:
                child = await self.jobs.start_prepared(
                    child_id, record.owner, conversion, compression
                )
                record.state = DownloadState.READY
                await self._save()
                return child
            except ApiError as exc:
                if exc.code != "QUEUE_FULL":
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    record.state = DownloadState.AWAITING_REVIEW
                    await self._save()
                    return await self.jobs.get(child_id, record.owner)
                retry_after = int((exc.headers or {}).get("Retry-After", "10"))
                await asyncio.sleep(max(1, min(30, retry_after)))

    async def _recover_child(self, record: _Download) -> None:
        try:
            child = await self.jobs.find_child(record.id, record.owner)
        except Exception:
            return
        if child is not None:
            record.child_id = child.id
            await self._save()

    async def _sync_child_state(self, record: _Download) -> None:
        if record.child_id is None:
            return
        try:
            child = await self.jobs.get(record.child_id, record.owner)
        except Exception:
            return
        child_state = getattr(child.state, "value", child.state)
        if child_state == "failed":
            self._preserve_source_after_processing_error(record, RuntimeError("child failed"))
            await self._save()
        elif (
            not record.auto_process
            and record.state is DownloadState.AWAITING_REVIEW
            and child_state in {"queued", "processing", "ready"}
        ):
            # Manual child starts happen through the jobs API, outside this manager.
            # Match _start_child: once accepted, the original source is ready/recoverable.
            record.state = DownloadState.READY
            record.error = None
            await self._save()

    def _preserve_source_after_processing_error(self, record: _Download, exc: Exception) -> None:
        record.state = DownloadState.AWAITING_REVIEW
        record.error = JobError(
            code="PROCESSING_FAILED",
            message="The source download is ready, but automatic processing could not start.",
        )
        if record.completed_at is None:
            record.completed_at = datetime.now(UTC)
        if record.expires_at is None:
            record.expires_at = record.completed_at + timedelta(
                seconds=self.jobs.retention_seconds(record.owner)
            )
        logger.warning(
            "Automatic processing failed for download %s (%s)", record.id, type(exc).__name__
        )

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("MeTube client is not started")
        stream = kwargs.pop("stream", False)
        if stream:
            request = self._client.build_request(method, path, **kwargs)
            response = await self._client.send(request, stream=True)
        else:
            response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    def _fail(self, record: _Download, message: str) -> None:
        record.state = DownloadState.FAILED
        record.completed_at = datetime.now(UTC)
        record.expires_at = record.completed_at + timedelta(
            seconds=self.jobs.retention_seconds(record.owner)
        )
        record.error = JobError(code="DOWNLOAD_FAILED", message=message[:500])

    async def _save(self) -> None:
        async with self._registry_lock:
            payload = {
                "version": 1,
                "items": [self._dump_record(item) for item in self._records.values()],
            }
            await asyncio.to_thread(self._persist_registry, payload)

    async def _cleanup_expired(self) -> None:
        now = datetime.now(UTC)
        expired = [
            record
            for record in self._records.values()
            if record.expires_at is not None and record.expires_at <= now
        ]
        for record in expired:
            async with self._lock:
                self._records.pop(record.id, None)
                task = self._tasks.pop(record.id, None)
                auto_process_task = self._auto_process_tasks.pop(record.id, None)
            tasks = [running for running in (task, auto_process_task) if running is not None]
            for running in tasks:
                if running is not asyncio.current_task():
                    running.cancel()
            pending = [running for running in tasks if running is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if (
                not self._downloads_dir.is_symlink()
                and record.directory == self._downloads_dir / record.id
            ):
                await asyncio.to_thread(_remove_tree, record.directory)
        if expired:
            await self._save()

    async def _janitor(self) -> None:
        interval = max(1, int(getattr(self.settings, "cleanup_interval_seconds", 30)))
        while True:
            await asyncio.sleep(interval)
            await self._cleanup_expired()

    async def _emit(self, event: str, record: _Download) -> None:
        if self.event_sink is None:
            return
        try:
            result = self.event_sink(
                event,
                record.id,
                record.owner,
                record.source_filename,
                record.output.bytes if record.output else None,
                record.expires_at,
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.warning("Integration event delivery failed (%s)", event)

    def _view(self, record: _Download) -> DownloadView:
        return DownloadView(
            id=record.id,
            state=record.state,
            created_at=record.created_at,
            completed_at=record.completed_at,
            expires_at=record.expires_at,
            status_url=f"{self.settings.public_base_url}/v1/downloads/{record.id}",
            source_filename=record.source_filename,
            output=record.output,
            error=record.error,
            submission_unknown=record.submission_unknown,
            parent_id=None,
            child_id=record.child_id,
            workflow=record.workflow,
            origin=record.origin,
        )

    def _history_view(self, record: _Download) -> dict[str, Any]:
        return {
            "id": record.id,
            "kind": "download",
            "completed_at": record.completed_at,
            "source_filename": record.source_filename,
            "job": self._view(record).model_dump(mode="json"),
        }

    def _persist_registry(self, payload: dict[str, Any]) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._registry.with_name(f".downloads-{uuid.uuid4().hex}.tmp")
        with tmp.open("x", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self._registry)
        directory_fd = os.open(
            self._base_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load_registry(self) -> dict[str, _Download]:
        if self._registry.is_symlink() or not self._registry.is_file():
            return {}
        try:
            payload = json.loads(self._registry.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or not isinstance(payload.get("items"), list)
            ):
                return {}
            records: dict[str, _Download] = {}
            for item in payload["items"]:
                record = self._restore_record(item)
                if record is not None:
                    records[record.id] = record
            return records
        except (OSError, ValueError, TypeError):
            logger.warning("Ignoring invalid integration download registry")
            return {}

    def _restore_record(self, item: Any) -> _Download | None:
        try:
            record_id = str(uuid.UUID(item["id"]))
            if record_id != item["id"]:
                return None
            directory = self._downloads_dir / record_id
            if directory.is_symlink() or not directory.is_dir():
                return None
            owner = item["owner"]
            if not isinstance(owner, str) or not owner or len(owner) > 256:
                return None
            origin = item.get("origin", "metube")
            if not isinstance(origin, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", origin):
                return None
            state = DownloadState(item["state"])
            workflow = DownloadWorkflow(item["workflow"])
            created = _datetime(item["created_at"])
            completed = _datetime(item.get("completed_at"))
            expires = _datetime(item.get("expires_at"))
            output = OutputMetadata.model_validate(item["output"]) if item.get("output") else None
            error = JobError.model_validate(item["error"]) if item.get("error") else None
            child_id = item.get("child_id")
            if child_id is not None and str(uuid.UUID(child_id)) != child_id:
                return None
            output_file = directory / "output"
            if state in {
                DownloadState.READY,
                DownloadState.PROCESSING,
                DownloadState.AWAITING_REVIEW,
            } and (
                output is None
                or output_file.is_symlink()
                or not output_file.is_file()
                or output_file.stat().st_size != output.bytes
            ):
                return None
            if output is not None:
                expected_url = f"{self.settings.public_base_url}/v1/downloads/{record_id}/content"
                if (
                    output.filename != _safe_filename(output.filename)
                    or output.download_url != expected_url
                    or not re.fullmatch(
                        r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+",
                        output.media_type,
                    )
                ):
                    return None
            conversion = (
                ConversionOptions.model_validate(item["conversion_options"])
                if item.get("conversion_options")
                else None
            )
            compression = (
                CompressionOptions.model_validate(item["compression_options"])
                if item.get("compression_options")
                else None
            )
            return _Download(
                id=record_id,
                owner=owner,
                state=state,
                created_at=created,
                workflow=workflow,
                directory=directory,
                origin=origin,
                source_filename=_safe_filename(item.get("source_filename") or "") or None,
                completed_at=completed,
                expires_at=expires,
                output=output,
                error=error,
                child_id=child_id,
                submission_started=bool(item.get("submission_started")),
                auto_process=bool(item.get("auto_process")),
                quality=item.get("quality", "best"),
                format=item.get("format", "ios"),
                download_type=item.get("download_type", "video"),
                codec=item.get("codec", "h264"),
                conversion_options=conversion,
                compression_options=compression,
                idempotency_key=(
                    item.get("idempotency_key")
                    if isinstance(item.get("idempotency_key"), str)
                    and len(item["idempotency_key"]) <= 160
                    else None
                ),
                submission_unknown=item.get("submission_unknown") is True,
            )
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _dump_record(record: _Download) -> dict[str, Any]:
        return {
            "id": record.id,
            "owner": record.owner,
            "state": record.state.value,
            "created_at": record.created_at.isoformat(),
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "workflow": record.workflow.value,
            "origin": record.origin,
            "source_filename": record.source_filename,
            "output": record.output.model_dump(mode="json") if record.output else None,
            "error": record.error.model_dump(mode="json") if record.error else None,
            "child_id": record.child_id,
            "submission_started": record.submission_started,
            "auto_process": record.auto_process,
            "quality": record.quality,
            "format": record.format,
            "download_type": record.download_type,
            "codec": record.codec,
            "conversion_options": (
                record.conversion_options.model_dump(mode="json")
                if record.conversion_options
                else None
            ),
            "compression_options": (
                record.compression_options.model_dump(mode="json")
                if record.compression_options
                else None
            ),
            "idempotency_key": record.idempotency_key,
            "submission_unknown": record.submission_unknown,
        }


def _safe_filename(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    name = Path(value.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._() -]", "_", name).strip(" .")[:_MAX_NAME]
    if name in {"", ".", ".."}:
        return "download.media"
    return name


def _retry_after_seconds(exc: ApiError) -> float:
    raw_value = (exc.headers or {}).get("Retry-After", "2")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = _AUTO_PROCESS_RETRY_SECONDS
    return max(0.1, min(30.0, value))


def _datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        import shutil

        shutil.rmtree(path)
