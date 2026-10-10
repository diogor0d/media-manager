from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from media_manager.config import AuthMode, Settings
from media_manager.errors import ApiError
from media_manager.integration_models import DownloadRequest, DownloadState
from media_manager.jobs import JobManager
from media_manager.models import ConversionOptions, MediaClass, MediaMetadata
from media_manager.processor import ConversionResult
from media_manager.workflows import DownloadManager


class Jobs:
    def retention_seconds(self, owner: str) -> int:
        return 600


def test_download_request_accepts_website_sidecar_choices_and_bounds_tokens() -> None:
    with pytest.raises(ValueError):
        DownloadRequest(url="https://user:password@video.example/clip")
    accepted = DownloadRequest(
        url="https://video.example/clip",
        quality="worst",
        format="m4a",
        download_type="audio",
        codec="aac",
    )
    assert (accepted.quality, accepted.format, accepted.download_type.value, accepted.codec) == (
        "worst",
        "m4a",
        "audio",
        "aac",
    )
    with pytest.raises(ValueError):
        DownloadRequest(url="https://video.example/clip", format="invalid choice")
    with pytest.raises(ValueError):
        DownloadRequest(url="https://video.example/clip", codec="x" * 65)


@pytest.mark.asyncio
async def test_sidecar_health_failure_rejects_new_download_without_persisting_and_recovers(
    tmp_path: Path,
) -> None:
    health_checks = 0
    posts: list[httpx.Request] = []
    timeouts: list[dict] = []
    auth_headers: list[str] = []
    health_paths: list[str] = []
    health_status = 503

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal health_checks
        if request.url.path == "/healthz":
            health_checks += 1
            health_paths.append(request.url.path)
            auth_headers.append(request.headers.get("x-sidecar-bridge-key", ""))
            timeouts.append(request.extensions["timeout"])
            return httpx.Response(
                health_status,
                headers={"location": "https://external.example/health"}
                if health_status == 302
                else {},
            )
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(202, json={})
        return httpx.Response(200, json={"status": "downloading"})

    settings = SimpleNamespace(
        work_dir=tmp_path,
        metube_sidecar_url="http://sidecar.local",
        metube_sidecar_api_key="sidecar-secret",
        max_upload_bytes=100,
        max_output_bytes=100,
        public_base_url="https://media.example",
    )
    manager = DownloadManager(settings, Jobs(), object(), transport=httpx.MockTransport(handler))
    await manager.start()
    assert health_checks == 0
    assert await manager.available() is False
    assert await manager.available() is False
    assert health_checks == 1
    with pytest.raises(ApiError) as unavailable:
        await manager.create(DownloadRequest(url="https://video.example/clip"), "owner-a")
    assert unavailable.value.status_code == 503
    assert unavailable.value.code == "INTEGRATION_UNAVAILABLE"
    assert posts == []
    assert manager._records == {}
    assert not (tmp_path / ".integrations" / "downloads.json").exists()

    health_status = 302
    manager._health_checked_at = None
    assert await manager.available() is False
    assert health_checks == 2
    health_status = 200
    manager._health_checked_at = None
    assert await manager.available() is True
    assert health_checks == 3
    assert health_paths == ["/healthz"] * 3
    assert auth_headers == ["sidecar-secret"] * 3
    assert timeouts == [{"connect": 2, "read": 2, "write": 2, "pool": 2}] * 3
    await manager.stop()


@pytest.mark.asyncio
async def test_download_availability_is_false_when_sidecar_is_disabled(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        metube_sidecar_url=None,
        metube_sidecar_api_key=None,
        max_upload_bytes=100,
        max_output_bytes=100,
        public_base_url="https://media.example",
    )
    manager = DownloadManager(settings, Jobs(), object())
    await manager.start()
    assert await manager.available() is False
    with pytest.raises(ApiError) as unavailable:
        await manager.create(DownloadRequest(url="https://video.example/clip"), "owner-a")
    assert unavailable.value.status_code == 503
    assert manager._records == {}
    await manager.stop()


@pytest.mark.asyncio
async def test_sidecar_poll_failure_invalidates_cached_health(tmp_path: Path) -> None:
    status_failed = asyncio.Event()
    healthy = True

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200 if healthy else 503)
        if request.method == "POST":
            return httpx.Response(202, json={})
        status_failed.set()
        return httpx.Response(503)

    settings = SimpleNamespace(
        work_dir=tmp_path,
        metube_sidecar_url="http://sidecar.local",
        metube_sidecar_api_key=None,
        max_upload_bytes=100,
        max_output_bytes=100,
        public_base_url="https://media.example",
    )
    manager = DownloadManager(settings, Jobs(), object(), transport=httpx.MockTransport(handler))
    await manager.start()
    view = await manager.create(DownloadRequest(url="https://video.example/clip"), "owner-a")
    await asyncio.wait_for(status_failed.wait(), 1)
    for _ in range(100):
        if not manager._health_available:
            break
        await asyncio.sleep(0.001)
    assert manager._health_available is False
    healthy = False
    assert await manager.available() is False
    assert view.id in manager._tasks
    await manager.stop()
    assert manager._records[view.id].state in {DownloadState.QUEUED, DownloadState.DOWNLOADING}


@pytest.mark.asyncio
async def test_download_is_idempotently_addressed_and_persists_without_source_url(
    tmp_path: Path,
) -> None:
    posts: list[dict] = []
    get_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        if request.method == "POST" and request.url.path == "/internal/jobs":
            posts.append(json.loads(request.content))
            return httpx.Response(202, json={"id": posts[-1]["request_id"]})
        if request.url.path.endswith(("/file", "/download")):
            return httpx.Response(
                200,
                headers={
                    "content-disposition": 'attachment; filename="clip.mp4"',
                    "content-type": "video/mp4",
                },
                content=b"media",
            )
        get_count += 1
        return httpx.Response(200, json={"status": "complete"})

    settings = SimpleNamespace(
        work_dir=tmp_path,
        metube_sidecar_url="http://sidecar.local",
        metube_sidecar_api_key="test-secret",
        max_upload_bytes=100,
        max_output_bytes=100,
        public_base_url="https://media.example",
    )
    manager = DownloadManager(settings, Jobs(), object(), transport=httpx.MockTransport(handler))
    await manager.start()
    request = DownloadRequest(url="https://video.example/watch/123")
    view = await manager.create(request, "owner-a", idempotency_key="telegram:111:42")
    assert len(posts) == 1  # create waits for the bounded upstream submission attempt.
    duplicate = await manager.create(request, "owner-a", idempotency_key="telegram:111:42")
    assert duplicate.id == view.id
    assert len(posts) == 1
    for _ in range(100):
        current = await manager.get(view.id, "owner-a")
        if current.state is DownloadState.READY:
            break
        await asyncio.sleep(0.01)
    assert current.state is DownloadState.READY
    assert len(posts) == 1
    assert posts[0]["request_id"] == view.id
    registry = (tmp_path / ".integrations" / "downloads.json").read_text()
    assert "video.example" not in registry
    path, filename, content_type = await manager.download(view.id, "owner-a")
    assert path.read_bytes() == b"media"
    assert (filename, content_type) == ("clip.mp4", "video/mp4")
    assert path.stat().st_mode & 0o777 == 0o600
    await manager.stop()


@pytest.mark.asyncio
async def test_history_includes_accepted_downloads_while_active(tmp_path: Path) -> None:
    entered = asyncio.Event()
    submitted_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            submitted_paths.append(request.url.path)
            return httpx.Response(202, json={})
        entered.set()
        return httpx.Response(200, json={"status": "downloading"})

    settings = SimpleNamespace(
        work_dir=tmp_path,
        metube_sidecar_url="http://sidecar.local",
        metube_sidecar_api_key=None,
        max_upload_bytes=100,
        max_output_bytes=100,
        public_base_url="https://media.example",
        max_live_jobs=2,
    )
    manager = DownloadManager(settings, Jobs(), object(), transport=httpx.MockTransport(handler))
    await manager.start()
    view = await manager.create(DownloadRequest(url="https://video.example/active"), "owner-a")
    assert submitted_paths == ["/jobs"]
    await asyncio.wait_for(entered.wait(), 1)
    entries, total = await manager.history("owner-a")
    assert total == 1
    assert entries[0]["id"] == view.id
    assert entries[0]["job"]["state"] in {"queued", "downloading"}
    await manager.stop()


@pytest.mark.asyncio
async def test_download_admission_checks_storage_before_accepting(
    tmp_path: Path, monkeypatch
) -> None:
    posts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
        return httpx.Response(200, json={"status": "downloading"})

    settings = SimpleNamespace(
        work_dir=tmp_path,
        metube_sidecar_url="http://sidecar.local",
        metube_sidecar_api_key=None,
        max_upload_bytes=100,
        max_output_bytes=100,
        public_base_url="https://media.example",
        max_live_jobs=2,
    )
    manager = DownloadManager(settings, Jobs(), object(), transport=httpx.MockTransport(handler))
    await manager.start()
    monkeypatch.setattr(
        "media_manager.workflows.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=99),
    )
    with pytest.raises(ApiError) as rejected:
        await manager.create(DownloadRequest(url="https://video.example/no-space"), "owner-a")
    assert rejected.value.code == "INSUFFICIENT_STORAGE"
    assert posts == []
    await manager.stop()


@pytest.mark.asyncio
async def test_download_owner_isolation_and_size_cap(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={})
        if request.url.path.endswith(("/file", "/download")):
            return httpx.Response(200, content=b"too-large")
        return httpx.Response(200, json={"status": "complete"})

    settings = SimpleNamespace(
        work_dir=tmp_path,
        metube_sidecar_url="http://sidecar.local",
        metube_sidecar_api_key="secret",
        max_upload_bytes=2,
        max_output_bytes=100,
        public_base_url="https://media.example",
    )
    manager = DownloadManager(settings, Jobs(), object(), transport=httpx.MockTransport(handler))
    await manager.start()
    view = await manager.create(DownloadRequest(url="https://video.example/x"), "owner-a")
    with pytest.raises(ApiError) as denied:
        await manager.get(view.id, "owner-b")
    assert denied.value.status_code == 404
    for _ in range(100):
        current = await manager.get(view.id, "owner-a")
        if current.state is DownloadState.FAILED:
            break
        await asyncio.sleep(0.01)
    assert current.state is DownloadState.FAILED
    assert not list((tmp_path / ".integrations" / "downloads" / view.id).glob("output*"))
    await manager.stop()


@pytest.mark.asyncio
async def test_uncertain_sidecar_post_is_not_repeated(tmp_path: Path) -> None:
    posts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(503)
        if request.url.path.endswith(("/file", "/download")):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(200, json={"status": "complete"})

    settings = SimpleNamespace(
        work_dir=tmp_path,
        metube_sidecar_url="http://sidecar.local",
        metube_sidecar_api_key="secret",
        max_upload_bytes=100,
        max_output_bytes=100,
        public_base_url="https://media.example",
        upload_timeout_seconds=1,
        max_live_jobs=1,
    )
    manager = DownloadManager(settings, Jobs(), object(), transport=httpx.MockTransport(handler))
    await manager.start()
    view = await manager.create(DownloadRequest(url="https://video.example/ambiguous"), "owner-a")
    assert posts == ["/internal/jobs"]
    for _ in range(100):
        current = await manager.get(view.id, "owner-a")
        if current.state in {DownloadState.READY, DownloadState.FAILED}:
            break
        await asyncio.sleep(0.01)
    assert current.state is DownloadState.READY
    assert posts == ["/internal/jobs"]
    assert current.submission_unknown is False
    await manager.stop()


@pytest.mark.asyncio
async def test_client_cancellation_does_not_cancel_persisted_sidecar_submission(
    tmp_path: Path,
) -> None:
    entered_post = asyncio.Event()
    release_post = asyncio.Event()
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            entered_post.set()
            await release_post.wait()
            return httpx.Response(202, json={})
        if request.url.path.endswith(("/file", "/download")):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(200, json={"status": "complete"})

    settings = SimpleNamespace(
        work_dir=tmp_path,
        metube_sidecar_url="http://sidecar.local",
        metube_sidecar_api_key=None,
        max_upload_bytes=100,
        max_output_bytes=100,
        public_base_url="https://media.example",
        max_live_jobs=2,
    )
    manager = DownloadManager(settings, Jobs(), object(), transport=httpx.MockTransport(handler))
    await manager.start()
    request_task = asyncio.create_task(
        manager.create(
            DownloadRequest(url="https://video.example/cancelled-request"),
            "owner-a",
            idempotency_key="telegram:111:84",
        )
    )
    await asyncio.wait_for(entered_post.wait(), 1)
    record_id = next(iter(manager._records))
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    release_post.set()
    for _ in range(200):
        current = await manager.get(record_id, "owner-a")
        if current.state is DownloadState.READY:
            break
        await asyncio.sleep(0.01)
    assert current.state is DownloadState.READY
    assert posts == 1
    await manager.stop()


class FakeProcessor:
    async def verify(self) -> None:
        return None

    async def convert(
        self,
        input_path: Path,
        job_dir: Path,
        options: ConversionOptions,
        cancel_event: asyncio.Event,
        progress_callback=None,
    ) -> ConversionResult:
        output = job_dir / "converted.mp4"
        output.write_bytes(b"converted")
        metadata = MediaMetadata(
            bytes=input_path.stat().st_size, media_class=MediaClass.VIDEO, container="mp4"
        )
        return ConversionResult(
            metadata, output, output.stat().st_size, output.name, "video/mp4", None, None, None
        )

    async def compress(self, *args, **kwargs):
        raise AssertionError("compression was not requested")


@pytest.mark.asyncio
async def test_auto_process_persists_parent_link_before_starting_child(tmp_path: Path) -> None:
    posts: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(202, json={"id": posts[-1]["request_id"]})
        if request.url.path.endswith(("/file", "/download")):
            return httpx.Response(200, content=b"source")
        return httpx.Response(200, json={"status": "complete"})

    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        public_base_url="http://localhost:8080",
        max_upload_bytes=1024,
        max_output_bytes=1024,
        max_live_jobs=2,
        cleanup_interval_seconds=3600,
        result_ttl_seconds=600,
        metube_sidecar_url="http://sidecar.local",
        metube_sidecar_api_key="sidecar-secret",
    )
    jobs = JobManager(settings, FakeProcessor())
    await jobs.start()
    downloads = DownloadManager(
        settings,
        jobs,
        FakeProcessor(),
        transport=httpx.MockTransport(handler),
    )
    await downloads.start()
    request = DownloadRequest(
        url="https://video.example/clip",
        workflow="convert",
        auto_process=True,
        conversion_options=ConversionOptions(target="video-mp4"),
    )
    parent = await downloads.create(request, "user:test", origin="telegram")
    assert parent.origin == "telegram"
    for _ in range(200):
        view = await downloads.get(parent.id, "user:test")
        if view.child_id and view.state is DownloadState.READY:
            child = await jobs.get(view.child_id, "user:test")
            if child.state.value in {"ready", "failed"}:
                break
        await asyncio.sleep(0.01)
    assert posts[0]["request_id"] == parent.id
    assert view.child_id is not None
    assert child.state.value == "ready"
    assert child.output is not None
    assert child.origin == "telegram"
    registry_item = json.loads(downloads._registry.read_text())["items"][0]
    assert registry_item["origin"] == "telegram"
    restored = downloads._restore_record(registry_item)
    assert restored is not None and restored.origin == "telegram"
    legacy_item = dict(registry_item)
    legacy_item.pop("origin")
    legacy_restored = downloads._restore_record(legacy_item)
    assert legacy_restored is not None and legacy_restored.origin == "metube"
    await downloads.stop()
    await jobs.stop()


@pytest.mark.asyncio
async def test_manual_child_state_syncs_parent_and_preserves_source_for_retry(
    tmp_path: Path,
) -> None:
    class SwitchProcessor(FakeProcessor):
        fail_conversion = True

        async def convert(self, *args, **kwargs):
            if self.fail_conversion:
                raise RuntimeError("synthetic conversion failure")
            return await super().convert(*args, **kwargs)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={})
        if request.url.path.endswith(("/file", "/download")):
            return httpx.Response(
                200,
                headers={
                    "content-disposition": 'attachment; filename="manual-source.mp4"',
                    "content-type": "video/mp4",
                },
                content=b"original-source",
            )
        return httpx.Response(200, json={"status": "complete"})

    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        public_base_url="http://localhost:8080",
        max_upload_bytes=1024,
        max_output_bytes=1024,
        max_live_jobs=2,
        cleanup_interval_seconds=3600,
        result_ttl_seconds=600,
        metube_sidecar_url="http://sidecar.local",
    )
    processor = SwitchProcessor()
    jobs = JobManager(settings, processor)
    await jobs.start()
    downloads = DownloadManager(
        settings, jobs, processor, transport=httpx.MockTransport(handler)
    )
    await downloads.start()
    options = ConversionOptions(target="video-mp4")
    parent = await downloads.create(
        DownloadRequest(
            url="https://video.example/manual-child",
            workflow="convert",
            conversion_options=options,
        ),
        "user:test",
    )
    for _ in range(200):
        parent_view = await downloads.get(parent.id, "user:test")
        if parent_view.state is DownloadState.AWAITING_REVIEW:
            break
        await asyncio.sleep(0.01)
    assert parent_view.state is DownloadState.AWAITING_REVIEW

    failed_child = await downloads.process(
        parent.id, "user:test", conversion_options=options, auto_start=False
    )
    await jobs.start_prepared(failed_child.id, "user:test", options)
    for _ in range(200):
        failed_child = await jobs.get(failed_child.id, "user:test")
        if failed_child.state.value == "failed":
            break
        await asyncio.sleep(0.01)
    assert failed_child.state.value == "failed"

    review = await downloads.get(parent.id, "user:test")
    assert review.state is DownloadState.AWAITING_REVIEW
    assert review.error is not None and review.error.code == "PROCESSING_FAILED"
    source_path, _, _ = await downloads.download(parent.id, "user:test")
    assert source_path.read_bytes() == b"original-source"

    processor.fail_conversion = False
    retry_child = await downloads.process(
        parent.id, "user:test", conversion_options=options, auto_start=False
    )
    await jobs.start_prepared(retry_child.id, "user:test", options)
    for _ in range(200):
        retry_child = await jobs.get(retry_child.id, "user:test")
        if retry_child.state.value == "ready":
            break
        await asyncio.sleep(0.01)
    assert retry_child.state.value == "ready"

    ready = await downloads.get(parent.id, "user:test")
    entries, _ = await downloads.history("user:test")
    history_parent = next(item for item in entries if item["id"] == parent.id)
    assert ready.state is DownloadState.READY
    assert history_parent["job"]["state"] == DownloadState.READY.value
    assert ready.error is None
    source_path, _, _ = await downloads.download(parent.id, "user:test")
    assert source_path.read_bytes() == b"original-source"
    await downloads.stop()
    await jobs.stop()


@pytest.mark.asyncio
async def test_auto_process_retries_queue_full_child_preparation_after_restart(
    tmp_path: Path,
) -> None:
    class FullOnceJobs(JobManager):
        def __init__(self, settings, processor):
            super().__init__(settings, processor)
            self.prepare_attempts = 0

        async def prepare_from_path(self, *args, **kwargs):
            self.prepare_attempts += 1
            if self.prepare_attempts == 1:
                raise ApiError(
                    429,
                    "QUEUE_FULL",
                    "The processing queue is full",
                    headers={"Retry-After": "30"},
                )
            return await super().prepare_from_path(*args, **kwargs)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={})
        if request.url.path.endswith(("/file", "/download")):
            return httpx.Response(200, content=b"source")
        return httpx.Response(200, json={"status": "complete"})

    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        public_base_url="http://localhost:8080",
        max_upload_bytes=1024,
        max_output_bytes=1024,
        max_live_jobs=2,
        cleanup_interval_seconds=3600,
        result_ttl_seconds=600,
        metube_sidecar_url="http://sidecar.local",
    )
    processor = FakeProcessor()
    jobs = FullOnceJobs(settings, processor)
    await jobs.start()
    request = DownloadRequest(
        url="https://video.example/retry",
        workflow="convert",
        auto_process=True,
        conversion_options=ConversionOptions(target="video-mp4"),
    )
    first = DownloadManager(settings, jobs, processor, transport=httpx.MockTransport(handler))
    await first.start()
    parent = await first.create(request, "user:test")
    for _ in range(300):
        record = first._records[parent.id]
        if jobs.prepare_attempts == 1 and record.output is not None:
            break
        await asyncio.sleep(0.01)
    assert jobs.prepare_attempts == 1
    assert record.output is not None
    assert record.state is DownloadState.PROCESSING
    await first.stop()

    restarted = DownloadManager(settings, jobs, processor, transport=httpx.MockTransport(handler))
    await restarted.start()
    for _ in range(300):
        current = await restarted.get(parent.id, "user:test")
        if current.child_id and current.state is DownloadState.READY:
            child = await jobs.get(current.child_id, "user:test")
            if child.state.value == "ready":
                break
        await asyncio.sleep(0.01)
    assert jobs.prepare_attempts == 2
    assert current.child_id is not None
    assert child.state.value == "ready"
    await restarted.stop()
    await jobs.stop()


@pytest.mark.asyncio
async def test_automatic_processing_failure_preserves_downloaded_original(tmp_path: Path) -> None:
    class BrokenProcessor(FakeProcessor):
        fail_conversion = True

        async def convert(self, *args, **kwargs):
            if self.fail_conversion:
                raise RuntimeError("synthetic processing error")
            return await super().convert(*args, **kwargs)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={})
        if request.url.path.endswith(("/file", "/download")):
            return httpx.Response(200, content=b"original")
        return httpx.Response(200, json={"status": "complete"})

    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        public_base_url="http://localhost:8080",
        max_upload_bytes=1024,
        max_output_bytes=1024,
        max_live_jobs=2,
        cleanup_interval_seconds=3600,
        result_ttl_seconds=600,
        metube_sidecar_url="http://sidecar.local",
    )
    processor = BrokenProcessor()
    jobs = JobManager(settings, processor)
    await jobs.start()
    downloads = DownloadManager(settings, jobs, processor, transport=httpx.MockTransport(handler))
    await downloads.start()
    parent = await downloads.create(
        DownloadRequest(
            url="https://video.example/clip",
            workflow="convert",
            auto_process=True,
            conversion_options=ConversionOptions(target="video-mp4"),
        ),
        "user:test",
    )
    for _ in range(200):
        view = await downloads.get(parent.id, "user:test")
        if view.state is DownloadState.AWAITING_REVIEW:
            break
        await asyncio.sleep(0.01)
    assert view.state is DownloadState.AWAITING_REVIEW
    assert view.output is not None
    assert view.error is not None and view.error.code == "PROCESSING_FAILED"
    path, _, _ = await downloads.download(parent.id, "user:test")
    assert path.read_bytes() == b"original"

    previous_child_id = view.child_id
    assert previous_child_id is not None
    processor.fail_conversion = False
    retried = await downloads.process(
        parent.id,
        "user:test",
        conversion_options=ConversionOptions(target="video-mp4", quality="economy"),
        auto_start=True,
    )
    for _ in range(200):
        retried = await jobs.get(retried.id, "user:test")
        if retried.state.value == "ready":
            break
        await asyncio.sleep(0.01)
    assert retried.state.value == "ready"
    assert retried.id != previous_child_id
    assert retried.quality.value == "economy"
    previous_child = await jobs.get(previous_child_id, "user:test")
    assert previous_child.state.value == "failed"
    current = await downloads.get(parent.id, "user:test")
    assert current.state is DownloadState.READY
    assert current.child_id == retried.id
    assert current.error is None
    assert path.read_bytes() == b"original"
    await downloads.stop()
    await jobs.stop()
