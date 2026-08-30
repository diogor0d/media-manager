from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request

from media_manager.app import create_app
from media_manager.auth import Principal, get_principal
from media_manager.config import AuthMode, Settings
from media_manager.models import ConversionOptions, MediaClass, MediaMetadata
from media_manager.processor import ConversionCancelled, ConversionResult

pytestmark = pytest.mark.asyncio

PUBLIC_BASE_URL = "https://media.example.test"
SOURCE_CONTENT = b"\x00\x00\x00\x18ftypqt  deterministic-source-media"
OUTPUT_CONTENT = b"\x00\x00\x00\x18ftypmp42deterministic-converted-media"


class FakeProcessor:
    def __init__(self, *, blocked: bool = False, error: Exception | None = None) -> None:
        self.verify_calls = 0
        self.convert_started = asyncio.Event()
        self.convert_finished = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.error = error
        self.input_paths: list[Path] = []
        self.job_dirs: list[Path] = []
        self.options: list[ConversionOptions] = []
        self.inputs: list[bytes] = []

    async def verify(self) -> None:
        self.verify_calls += 1

    async def convert(
        self,
        input_path: Path,
        job_dir: Path,
        options: ConversionOptions,
        cancel_event: asyncio.Event,
    ) -> ConversionResult:
        source = input_path.read_bytes()
        self.input_paths.append(input_path)
        self.job_dirs.append(job_dir)
        self.options.append(options)
        self.inputs.append(source)
        self.convert_started.set()

        try:
            await self._wait_until_released(cancel_event)
            if self.error is not None:
                raise self.error

            output_path = job_dir / "result.mp4"
            output_path.write_bytes(OUTPUT_CONTENT)
            return ConversionResult(
                input=MediaMetadata(
                    bytes=len(source),
                    media_class=MediaClass.VIDEO,
                    container="mov,mp4,m4a,3gp,3g2,mj2",
                    duration_ms=12_345,
                    width=1920,
                    height=1080,
                    video_codec="h264",
                    audio_codec="aac",
                ),
                output_path=output_path,
                output_bytes=len(OUTPUT_CONTENT),
                filename="converted.mp4",
                media_type="video/mp4",
                width=1280,
                height=720,
                duration_ms=12_345,
            )
        finally:
            self.convert_finished.set()

    async def _wait_until_released(self, cancel_event: asyncio.Event) -> None:
        release_waiter = asyncio.create_task(self.release.wait())
        cancel_waiter = asyncio.create_task(cancel_event.wait())
        waiters = (release_waiter, cancel_waiter)
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if cancel_event.is_set():
                raise ConversionCancelled
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        public_base_url=PUBLIC_BASE_URL,
        max_upload_bytes=1024,
        max_live_jobs=4,
        result_ttl_seconds=300,
        cleanup_interval_seconds=3600,
    )
    return replace(settings, **overrides)


@asynccontextmanager
async def running_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        yield client


def assert_error(
    response: httpx.Response,
    status_code: int,
    code: str,
    message: str,
) -> None:
    assert response.status_code == status_code
    assert response.json() == {"error": {"code": code, "message": message}}


async def wait_for_state(
    client: httpx.AsyncClient,
    job_id: str,
    expected_state: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    observed: list[str] = []
    for _ in range(100):
        response = await client.get(f"/v1/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        job = response.json()
        observed.append(job["state"])
        if job["state"] == expected_state:
            return job
        await asyncio.sleep(0)
    pytest.fail(f"Job did not reach {expected_state!r}; observed states: {observed}")


async def test_liveness_and_readiness_follow_application_lifespan(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.work_dir.mkdir()
    orphan = settings.work_dir / "orphan"
    orphan.mkdir()
    (orphan / "partial-output").write_bytes(b"stale")

    processor = FakeProcessor()
    app = create_app(settings, processor=processor)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        not_ready = await client.get("/health/ready")
        assert not_ready.status_code == 503
        assert not_ready.json() == {"status": "not_ready"}
        assert processor.verify_calls == 0

        async with app.router.lifespan_context(app):
            ready = await client.get("/health/ready")
            assert ready.status_code == 200
            assert ready.json() == {"status": "ready"}
            assert processor.verify_calls == 1
            assert not orphan.exists()

        stopped = await client.get("/health/ready")
        assert stopped.status_code == 503
        assert stopped.json() == {"status": "not_ready"}
        assert (await client.get("/health/live")).json() == {"status": "ok"}


async def test_capabilities_report_the_supported_contract_and_limits(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_upload_bytes=789, result_ttl_seconds=456)
    app = create_app(settings, processor=FakeProcessor())

    async with running_client(app) as client:
        response = await client.get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "targets": [
            {
                "value": "video-mp4",
                "label": "MP4 (H.264)",
                "media_type": "video/mp4",
                "extension": "mp4",
                "accepts": ["animation", "video"],
            },
            {
                "value": "video-webm",
                "label": "WebM (VP9)",
                "media_type": "video/webm",
                "extension": "webm",
                "accepts": ["animation", "video"],
            },
            {
                "value": "image-jpeg",
                "label": "JPEG",
                "media_type": "image/jpeg",
                "extension": "jpg",
                "accepts": ["image"],
            },
            {
                "value": "image-png",
                "label": "PNG",
                "media_type": "image/png",
                "extension": "png",
                "accepts": ["image"],
            },
            {
                "value": "image-webp",
                "label": "WebP",
                "media_type": "image/webp",
                "extension": "webp",
                "accepts": ["image"],
            },
            {
                "value": "animation-gif",
                "label": "Animated GIF",
                "media_type": "image/gif",
                "extension": "gif",
                "accepts": ["animation", "image", "video"],
            },
            {
                "value": "audio-m4a",
                "label": "M4A (AAC)",
                "media_type": "audio/mp4",
                "extension": "m4a",
                "accepts": ["audio", "video"],
            },
            {
                "value": "audio-mp3",
                "label": "MP3",
                "media_type": "audio/mpeg",
                "extension": "mp3",
                "accepts": ["audio", "video"],
            },
            {
                "value": "audio-opus",
                "label": "Ogg Opus",
                "media_type": "audio/ogg",
                "extension": "opus",
                "accepts": ["audio", "video"],
            },
        ],
        "qualities": [
            {"value": "economy", "label": "Smaller file"},
            {"value": "balanced", "label": "Balanced"},
            {"value": "high", "label": "Higher quality"},
        ],
        "resolutions": [
            {"value": "source", "label": "Original resolution"},
            {"value": "480p", "label": "480p / 854 px long edge"},
            {"value": "720p", "label": "720p / 1280 px long edge"},
            {"value": "1080p", "label": "1080p / 1920 px long edge"},
            {"value": "1440p", "label": "1440p / 2560 px long edge"},
            {"value": "2160p", "label": "2160p / 3840 px long edge"},
        ],
        "audio_modes": [
            {"value": "keep", "label": "Keep audio"},
            {"value": "drop", "label": "Remove audio"},
        ],
        "max_upload_bytes": 789,
        "result_ttl_seconds": 456,
    }


async def test_raw_upload_transitions_to_ready_then_downloads_and_deletes_exact_result(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    processor = FakeProcessor(blocked=True)
    app = create_app(settings, processor=processor)

    async with running_client(app) as client:
        created_response = await client.post(
            "/v1/jobs",
            params={
                "target": "video-mp4",
                "quality": "high",
                "resolution": "720p",
                "audio": "drop",
            },
            content=SOURCE_CONTENT,
            headers={"Content-Type": "video/quicktime"},
        )
        assert created_response.status_code == 202
        created = created_response.json()
        job_id = created["id"]
        UUID(job_id)
        expected_status_url = f"{PUBLIC_BASE_URL}/v1/jobs/{job_id}"
        assert created == {
            "id": job_id,
            "state": "queued",
            "target": "video-mp4",
            "quality": "high",
            "resolution": "720p",
            "audio": "drop",
            "created_at": created["created_at"],
            "expires_at": None,
            "status_url": expected_status_url,
            "input": None,
            "output": None,
            "error": None,
        }
        assert datetime.fromisoformat(created["created_at"]).tzinfo is not None

        await asyncio.wait_for(processor.convert_started.wait(), timeout=5)
        processing_response = await client.get(expected_status_url)
        assert processing_response.status_code == 200
        processing = processing_response.json()
        assert processing == {**created, "state": "processing"}
        assert processor.inputs == [SOURCE_CONTENT]
        assert processor.options[0].model_dump(mode="json") == {
            "target": "video-mp4",
            "quality": "high",
            "resolution": "720p",
            "audio": "drop",
        }
        assert processor.input_paths == [settings.work_dir / job_id / "input"]
        assert processor.job_dirs == [settings.work_dir / job_id]
        assert processor.input_paths[0].read_bytes() == SOURCE_CONTENT

        processor.release.set()
        await asyncio.wait_for(processor.convert_finished.wait(), timeout=5)
        ready = await wait_for_state(client, job_id, "ready")
        assert ready == {
            "id": job_id,
            "state": "ready",
            "target": "video-mp4",
            "quality": "high",
            "resolution": "720p",
            "audio": "drop",
            "created_at": created["created_at"],
            "expires_at": ready["expires_at"],
            "status_url": expected_status_url,
            "input": {
                "bytes": len(SOURCE_CONTENT),
                "media_class": "video",
                "container": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration_ms": 12_345,
                "width": 1920,
                "height": 1080,
                "video_codec": "h264",
                "audio_codec": "aac",
            },
            "output": {
                "bytes": len(OUTPUT_CONTENT),
                "filename": "converted.mp4",
                "media_type": "video/mp4",
                "download_url": ready["output"]["download_url"],
                "width": 1280,
                "height": 720,
                "duration_ms": 12_345,
            },
            "error": None,
        }
        assert ready["expires_at"] is not None
        assert datetime.fromisoformat(ready["expires_at"]) > datetime.fromisoformat(
            created["created_at"]
        )
        assert not processor.input_paths[0].exists()
        assert (settings.work_dir / job_id / "result.mp4").read_bytes() == OUTPUT_CONTENT

        expected_download_url = f"{PUBLIC_BASE_URL}/v1/jobs/{job_id}/content"
        download = await client.get(expected_download_url)
        assert download.status_code == 200
        assert download.content == OUTPUT_CONTENT
        assert download.headers["content-type"] == "video/mp4"
        assert download.headers["content-length"] == str(len(OUTPUT_CONTENT))
        assert download.headers["content-disposition"] == 'attachment; filename="converted.mp4"'

        deleted = await client.delete(expected_status_url)
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert_error(
            await client.get(expected_status_url),
            404,
            "JOB_NOT_FOUND",
            "Job was not found",
        )
        assert_error(
            await client.get(expected_download_url),
            404,
            "JOB_NOT_FOUND",
            "Job was not found",
        )
        assert_error(
            await client.delete(expected_status_url),
            404,
            "JOB_NOT_FOUND",
            "Job was not found",
        )
        assert list(settings.work_dir.iterdir()) == []

        assert ready["output"]["download_url"] == expected_download_url


async def test_empty_upload_is_rejected_and_reserved_job_is_cleaned_up(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)

    async with running_client(app) as client:
        response = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            content=b"",
        )

        assert_error(response, 422, "EMPTY_INPUT", "Upload body is empty")
        assert processor.inputs == []
        assert list(settings.work_dir.iterdir()) == []


async def test_streamed_oversized_upload_is_rejected_and_partial_file_is_cleaned_up(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, max_upload_bytes=7)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)

    async def upload_chunks() -> AsyncIterator[bytes]:
        yield b"1234"
        yield b"5678"

    async with running_client(app) as client:
        response = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            content=upload_chunks(),
        )

        assert_error(response, 413, "INPUT_TOO_LARGE", "Upload exceeds the size limit")
        assert processor.inputs == []
        assert list(settings.work_dir.iterdir()) == []


async def test_multipart_upload_is_rejected_without_reserving_a_job(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)

    async with running_client(app) as client:
        response = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            files={"file": ("source.mp4", SOURCE_CONTENT, "video/mp4")},
        )

        assert_error(
            response,
            415,
            "RAW_FILE_REQUIRED",
            "Send the media file as the raw request body",
        )
        assert processor.inputs == []
        assert list(settings.work_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"target": "audio-mp3", "resolution": "720p"},
            "Resolution does not apply to audio output",
        ),
        (
            {"target": "image-png", "audio": "drop"},
            "Audio removal applies only to video output",
        ),
        (
            {"target": "animation-gif", "resolution": "1440p"},
            "GIF output is limited to 1080p",
        ),
    ],
)
async def test_invalid_option_combinations_are_rejected_before_upload(
    tmp_path: Path,
    params: dict[str, str],
    message: str,
) -> None:
    settings = make_settings(tmp_path)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)

    async with running_client(app) as client:
        response = await client.post("/v1/jobs", params=params, content=SOURCE_CONTENT)

        assert_error(response, 422, "INVALID_OPTIONS", message)
        assert processor.inputs == []
        assert list(settings.work_dir.iterdir()) == []


async def test_queue_capacity_rejects_an_additional_live_job(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_live_jobs=1)
    processor = FakeProcessor(blocked=True)
    app = create_app(settings, processor=processor)

    async with running_client(app) as client:
        first = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            content=SOURCE_CONTENT,
        )
        assert first.status_code == 202
        first_job_id = first.json()["id"]
        await asyncio.wait_for(processor.convert_started.wait(), timeout=5)

        second = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            content=b"another-source",
        )
        assert_error(second, 429, "QUEUE_FULL", "The conversion queue is full")
        assert second.headers["retry-after"] == "10"
        assert len(list(settings.work_dir.iterdir())) == 1

        processor.release.set()
        await asyncio.wait_for(processor.convert_finished.wait(), timeout=5)
        await wait_for_state(client, first_job_id, "ready")
        assert (await client.delete(f"/v1/jobs/{first_job_id}")).status_code == 204
        assert list(settings.work_dir.iterdir()) == []


async def test_unhandled_processing_failure_returns_only_a_safe_error(tmp_path: Path) -> None:
    secret = "private-ffmpeg-command-and-token"
    settings = make_settings(tmp_path)
    processor = FakeProcessor(error=RuntimeError(secret))
    app = create_app(settings, processor=processor)

    async with running_client(app) as client:
        created = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            content=SOURCE_CONTENT,
        )
        assert created.status_code == 202
        job_id = created.json()["id"]

        await asyncio.wait_for(processor.convert_finished.wait(), timeout=5)
        failed = await wait_for_state(client, job_id, "failed")
        assert failed["input"] is None
        assert failed["output"] is None
        assert failed["expires_at"] is not None
        assert failed["error"] == {
            "code": "CONVERSION_FAILED",
            "message": "The media could not be converted",
        }
        assert secret not in str(failed)
        assert list((settings.work_dir / job_id).iterdir()) == []

        assert_error(
            await client.get(f"/v1/jobs/{job_id}/content"),
            409,
            "JOB_NOT_READY",
            "Converted media is not ready",
        )
        assert (await client.delete(f"/v1/jobs/{job_id}")).status_code == 204
        assert list(settings.work_dir.iterdir()) == []


async def test_jobs_are_hidden_from_other_principals_with_not_found_responses(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)

    async def principal_from_header(request: Request) -> Principal:
        return Principal(id=f"test:{request.headers['X-Test-Principal']}")

    app.dependency_overrides[get_principal] = principal_from_header
    owner_headers = {"X-Test-Principal": "owner"}
    other_headers = {"X-Test-Principal": "other"}

    async with running_client(app) as client:
        created = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            content=SOURCE_CONTENT,
            headers=owner_headers,
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        await asyncio.wait_for(processor.convert_finished.wait(), timeout=5)
        await wait_for_state(client, job_id, "ready", headers=owner_headers)

        assert_error(
            await client.get(f"/v1/jobs/{job_id}", headers=other_headers),
            404,
            "JOB_NOT_FOUND",
            "Job was not found",
        )
        assert_error(
            await client.get(f"/v1/jobs/{job_id}/content", headers=other_headers),
            404,
            "JOB_NOT_FOUND",
            "Job was not found",
        )
        assert_error(
            await client.delete(f"/v1/jobs/{job_id}", headers=other_headers),
            404,
            "JOB_NOT_FOUND",
            "Job was not found",
        )

        owner_download = await client.get(
            f"/v1/jobs/{job_id}/content",
            headers=owner_headers,
        )
        assert owner_download.status_code == 200
        assert owner_download.content == OUTPUT_CONTENT
        assert (
            await client.delete(f"/v1/jobs/{job_id}", headers=owner_headers)
        ).status_code == 204
        assert list(settings.work_dir.iterdir()) == []
