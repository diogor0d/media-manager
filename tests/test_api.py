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
from media_manager.models import (
    CompressionMetadata,
    ConversionOptions,
    MediaClass,
    MediaMetadata,
    Target,
)
from media_manager.processor import CompressionResult, ConversionCancelled, ConversionResult

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
        progress_callback=None,
    ) -> ConversionResult:
        source = input_path.read_bytes()
        self.input_paths.append(input_path)
        self.job_dirs.append(job_dir)
        self.options.append(options)
        self.inputs.append(source)
        self.convert_started.set()
        if progress_callback:
            await progress_callback("converting", 42)

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

    async def compress(
        self,
        input_path: Path,
        job_dir: Path,
        cancel_event: asyncio.Event,
        progress_callback=None,
    ) -> CompressionResult:
        converted = await self.convert(
            input_path,
            job_dir,
            ConversionOptions(target=Target.VIDEO_MP4, quality_percent=0),
            cancel_event,
            progress_callback,
        )
        return CompressionResult(
            input=converted.input,
            output_path=converted.output_path,
            output_bytes=converted.output_bytes,
            filename=converted.filename,
            media_type=converted.media_type,
            width=converted.width,
            height=converted.height,
            duration_ms=converted.duration_ms,
            compression=CompressionMetadata(
                target_bytes=20_000_000,
                aim_bytes=19_000_000,
                met_target=True,
                attempts=1,
                selected_target=Target.VIDEO_MP4,
            ),
        )


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
    body = response.json()
    all_resolutions = ["source", "480p", "720p", "1080p", "1440p", "2160p"]
    expected_matrices = {
        "video-mp4": (all_resolutions, ["keep", "drop"]),
        "video-webm": (all_resolutions, ["keep", "drop"]),
        "image-jpeg": (all_resolutions, ["keep"]),
        "image-png": (all_resolutions, ["keep"]),
        "image-webp": (all_resolutions, ["keep"]),
        "animation-gif": (all_resolutions[:4], ["keep"]),
        "audio-m4a": (["source"], ["keep"]),
        "audio-mp3": (["source"], ["keep"]),
        "audio-opus": (["source"], ["keep"]),
    }
    quality_profiles = {}
    for target in body["targets"]:
        assert (
            target.pop("allowed_resolutions"),
            target.pop("allowed_audio_modes"),
        ) == expected_matrices[target["value"]]
        quality_profiles[target["value"]] = (
            target.pop("quality_metrics"),
            target.pop("quality_note"),
        )

    assert quality_profiles["video-mp4"][0][0] == {
        "label": "H.264 CRF",
        "economy": 32,
        "balanced": 26,
        "high": 20,
        "unit": "CRF",
        "higher_is_better": False,
    }
    assert "lossless" in quality_profiles["image-png"][1].lower()

    assert body == {
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
        "quality_scale": {"minimum": 0, "maximum": 100, "step": 1, "default": 50},
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
        "max_chunk_bytes": 789,
        "result_ttl_seconds": 456,
        "compression_target_bytes": 20_000_000,
    }


async def test_authenticated_chunk_upload_resumes_by_offset_then_converts(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, max_chunk_bytes=32)
    processor = FakeProcessor(blocked=True)
    app = create_app(settings, processor=processor)

    async with running_client(app) as client:
        created = await client.post(
            "/v1/uploads",
            params={"target": "video-mp4", "resolution": "720p"},
            headers={"Upload-Length": str(len(SOURCE_CONTENT))},
        )
        assert created.status_code == 201
        upload = created.json()
        job_id = upload["id"]
        assert upload == {
            "id": job_id,
            "offset": 0,
            "length": len(SOURCE_CONTENT),
            "chunk_size": 32,
            "upload_url": f"{PUBLIC_BASE_URL}/v1/uploads/{job_id}",
            "expires_at": upload["expires_at"],
        }

        incomplete = await client.post(f"/v1/uploads/{job_id}/complete")
        assert_error(incomplete, 409, "UPLOAD_INCOMPLETE", "Upload is incomplete")

        first_chunk = SOURCE_CONTENT[:16]
        first = await client.patch(
            f"/v1/uploads/{job_id}",
            content=first_chunk,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": "0",
            },
        )
        assert first.status_code == 200
        assert first.json()["offset"] == len(first_chunk)

        wrong_offset = await client.patch(
            f"/v1/uploads/{job_id}",
            content=b"bad",
            headers={"Upload-Offset": "0"},
        )
        assert_error(
            wrong_offset,
            409,
            "UPLOAD_OFFSET_MISMATCH",
            "Upload offset does not match the server",
        )
        assert (await client.get(f"/v1/uploads/{job_id}")).json()["offset"] == 16

        second = await client.patch(
            f"/v1/uploads/{job_id}",
            content=SOURCE_CONTENT[16:],
            headers={"Upload-Offset": "16"},
        )
        assert second.status_code == 200
        assert second.json()["offset"] == len(SOURCE_CONTENT)

        completed = await client.post(f"/v1/uploads/{job_id}/complete")
        assert completed.status_code == 202
        assert completed.json()["state"] == "queued"
        await asyncio.wait_for(processor.convert_started.wait(), timeout=5)
        processor.release.set()
        await asyncio.wait_for(processor.convert_finished.wait(), timeout=5)
        ready = await wait_for_state(client, job_id, "ready")
        assert ready["output"]["bytes"] == len(OUTPUT_CONTENT)
        assert (await client.delete(f"/v1/jobs/{job_id}")).status_code == 204
        assert list(settings.work_dir.iterdir()) == []
    assert processor.inputs == [SOURCE_CONTENT]


async def test_compression_job_uses_fixed_target_and_returns_downloadable_result(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path), processor=FakeProcessor())

    async with running_client(app) as client:
        invalid = await client.post(
            "/v1/compressions?quality_percent=0",
            content=SOURCE_CONTENT,
        )
        assert_error(
            invalid,
            422,
            "INVALID_OPTIONS",
            "Compression does not accept options",
        )
        created = await client.post(
            "/v1/compressions",
            content=SOURCE_CONTENT,
            headers={"Upload-Filename": "holiday%20clip.mov"},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        assert created.json()["status_url"] == f"{PUBLIC_BASE_URL}/v1/compressions/{job_id}"

        for _ in range(100):
            response = await client.get(f"/v1/compressions/{job_id}")
            assert response.status_code == 200
            job = response.json()
            if job["state"] == "ready":
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("Compression job did not become ready")

        assert job["compression"] == {
            "target_bytes": 20_000_000,
            "aim_bytes": 19_000_000,
            "met_target": True,
            "attempts": 1,
            "selected_target": "video-mp4",
        }
        assert job["output"]["filename"] == "holiday clip-compressed.mp4"
        assert job["output"]["bytes"] == len(OUTPUT_CONTENT)
        assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 404
        downloaded = await client.get(f"/v1/compressions/{job_id}/content")
        assert downloaded.content == OUTPUT_CONTENT
        assert (await client.delete(f"/v1/compressions/{job_id}")).status_code == 204


async def test_quality_percentage_is_exact_and_legacy_presets_remain_supported(
    tmp_path: Path,
) -> None:
    processor = FakeProcessor()
    app = create_app(make_settings(tmp_path), processor=processor)

    async with running_client(app) as client:
        percentage = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4", "quality_percent": "73"},
            content=SOURCE_CONTENT,
        )
        assert percentage.status_code == 202
        assert percentage.json()["quality_percent"] == 73
        assert percentage.json()["quality"] == "balanced"
        await wait_for_state(client, percentage.json()["id"], "ready")

        legacy = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4", "quality": "economy"},
            content=SOURCE_CONTENT,
        )
        assert legacy.status_code == 202
        assert legacy.json()["quality_percent"] == 0

        conflict = await client.post(
            "/v1/jobs",
            params={
                "target": "video-mp4",
                "quality": "high",
                "quality_percent": "100",
            },
            content=SOURCE_CONTENT,
        )
        assert_error(
            conflict,
            422,
            "INVALID_OPTIONS",
            "Use quality_percent or the legacy quality preset, not both",
        )

        invalid = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4", "quality_percent": "101"},
            content=SOURCE_CONTENT,
        )
        assert_error(invalid, 422, "INVALID_REQUEST", "Request parameters are invalid")

    assert processor.options[0].quality_percent == 73
    assert processor.options[1].quality_percent == 0


async def test_download_filename_uses_sanitized_source_basename(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), processor=FakeProcessor())

    async with running_client(app) as client:
        created = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            content=SOURCE_CONTENT,
            headers={"Upload-Filename": "..%2Ffolder%5CCamera%20Clip.MOV"},
        )
        ready = await wait_for_state(client, created.json()["id"], "ready")
        download = await client.get(ready["output"]["download_url"])

    assert ready["output"]["filename"] == "Camera Clip-converted.mp4"
    assert "Camera%20Clip-converted.mp4" in download.headers["content-disposition"]

async def test_chunk_size_limit_is_enforced_before_writing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_chunk_bytes=4)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)

    async with running_client(app) as client:
        created = await client.post(
            "/v1/uploads",
            params={"target": "video-mp4"},
            headers={"Upload-Length": "8"},
        )
        job_id = created.json()["id"]
        response = await client.patch(
            f"/v1/uploads/{job_id}",
            content=b"12345",
            headers={"Upload-Offset": "0"},
        )

        assert_error(response, 413, "CHUNK_TOO_LARGE", "Upload chunk exceeds the size limit")
        assert (settings.work_dir / job_id / "input").read_bytes() == b""
        assert (await client.delete(f"/v1/jobs/{job_id}")).status_code == 204


async def test_deleting_an_active_chunk_waits_then_removes_partial_upload(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, max_chunk_bytes=16)
    app = create_app(settings, processor=FakeProcessor())
    chunk_started = asyncio.Event()
    release_chunk = asyncio.Event()

    async def slow_chunk() -> AsyncIterator[bytes]:
        yield b"1234"
        chunk_started.set()
        await release_chunk.wait()
        yield b"5678"

    async with running_client(app) as client:
        created = await client.post(
            "/v1/uploads",
            params={"target": "video-mp4"},
            headers={"Upload-Length": "8"},
        )
        job_id = created.json()["id"]
        patch_task = asyncio.create_task(
            client.patch(
                f"/v1/uploads/{job_id}",
                content=slow_chunk(),
                headers={"Upload-Offset": "0", "Content-Length": "8"},
            )
        )
        await asyncio.wait_for(chunk_started.wait(), timeout=5)
        delete_task = asyncio.create_task(client.delete(f"/v1/jobs/{job_id}"))
        await asyncio.sleep(0)
        assert not delete_task.done()

        release_chunk.set()
        patch_response, delete_response = await asyncio.gather(patch_task, delete_task)

        assert patch_response.status_code == 404
        assert delete_response.status_code == 204
        assert list(settings.work_dir.iterdir()) == []


async def test_upload_sessions_are_hidden_from_other_principals(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_chunk_bytes=16)
    app = create_app(settings, processor=FakeProcessor())

    async def principal_from_header(request: Request) -> Principal:
        return Principal(id=f"test:{request.headers['X-Test-Principal']}")

    app.dependency_overrides[get_principal] = principal_from_header
    owner = {"X-Test-Principal": "owner"}
    other = {"X-Test-Principal": "other"}

    async with running_client(app) as client:
        created = await client.post(
            "/v1/uploads",
            params={"target": "video-mp4"},
            headers={**owner, "Upload-Length": "8"},
        )
        job_id = created.json()["id"]

        assert_error(
            await client.get(f"/v1/uploads/{job_id}", headers=other),
            404,
            "JOB_NOT_FOUND",
            "Job was not found",
        )
        assert_error(
            await client.patch(
                f"/v1/uploads/{job_id}",
                content=b"1234",
                headers={**other, "Upload-Offset": "0"},
            ),
            404,
            "JOB_NOT_FOUND",
            "Job was not found",
        )
        assert_error(
            await client.post(f"/v1/uploads/{job_id}/complete", headers=other),
            404,
            "JOB_NOT_FOUND",
            "Job was not found",
        )
        assert (await client.delete(f"/v1/jobs/{job_id}", headers=owner)).status_code == 204


async def test_web_interface_and_assets_are_served_with_browser_security_headers(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path), processor=FakeProcessor())

    async with running_client(app) as client:
        responses = {
            path: await client.get(path)
            for path in (
                "/",
                "/assets/app.css",
                "/assets/app.js",
                "/assets/logo.svg",
                "/assets/icon-192.png",
                "/assets/icon-512.png",
                "/assets/apple-touch-icon.png",
                "/favicon.svg",
                "/manifest.webmanifest",
                "/sw.js",
            )
        }
        schema = await client.get("/openapi.json")
        capabilities = await client.get("/v1/capabilities")

    assert "Drop one media file" in responses["/"].text
    assert 'rel="manifest" href="/manifest.webmanifest"' in responses["/"].text
    assert 'rel="apple-touch-icon"' in responses["/"].text
    assert 'apple-mobile-web-app-capable" content="yes"' in responses["/"].text
    assert "--aperture" in responses["/assets/app.css"].text
    assert "color-scheme: dark" in responses["/assets/app.css"].text
    assert '"use strict"' in responses["/assets/app.js"].text
    assert 'navigator.serviceWorker.register("/sw.js"' in responses["/assets/app.js"].text
    assert "apiJson(createdResponse)" in responses["/assets/app.js"].text
    assert (
        'if (!state.capabilities) return window.location.reload()'
        in responses["/assets/app.js"].text
    )
    assert '$("#file-input").disabled = !available' in responses["/assets/app.js"].text
    assert 'state.resumeAction = "upload"' in responses["/assets/app.js"].text
    assert 'state.resumeAction = "poll"' in responses["/assets/app.js"].text
    assert 'download.setAttribute("aria-disabled"' in responses["/assets/app.js"].text
    assert 'type="range" min="0" max="100" step="1" value="50"' in responses["/"].text
    assert 'data-stage="converting"' in responses["/"].text
    assert 'quality_percent: qualityPercent' in responses["/assets/app.js"].text
    assert '"Upload-Filename": encodeURIComponent(state.file.name)' in responses[
        "/assets/app.js"
    ].text
    assert 'job.progress?.percent' in responses["/assets/app.js"].text
    assert "Media Manager" in responses["/assets/logo.svg"].text
    assert responses["/favicon.svg"].headers["content-type"] == "image/svg+xml"
    manifest = responses["/manifest.webmanifest"]
    assert manifest.headers["content-type"] == "application/manifest+json"
    assert manifest.headers["cache-control"] == "no-cache"
    assert manifest.json() == {
        "id": "/",
        "name": "Media Manager Converter",
        "short_name": "Converter",
        "description": "Private, disposable media conversion.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0a0f12",
        "theme_color": "#0a0f12",
        "icons": [
            {
                "src": "/assets/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/assets/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    worker = responses["/sw.js"]
    assert worker.headers["cache-control"] == "no-cache"
    assert worker.headers["service-worker-allowed"] == "/"
    assert 'url.pathname.startsWith("/v1/")' in worker.text
    assert 'request.mode === "navigate" && url.pathname === "/"' in worker.text
    assert '["/assets/app.js?v=pwa3", "text/javascript"]' in worker.text
    assert 'navigator.canShare?.({ files: [file] })' in responses["/assets/app.js"].text
    assert 'sessionStorage.setItem(RECOVERY_KEY, job.id)' in responses["/assets/app.js"].text
    assert 'item.setAttribute("aria-current", "step")' in responses["/assets/app.js"].text
    assert 'env(safe-area-inset-bottom)' in responses["/assets/app.css"].text
    assert 'event.data === "SKIP_WAITING"' in worker.text
    assert "event.waitUntil(cacheShell())" in worker.text
    assert ").then(() => self.clients.claim())" in worker.text
    assert "Invalid application shell response" in worker.text
    for path in (
        "/assets/icon-192.png",
        "/assets/icon-512.png",
        "/assets/apple-touch-icon.png",
    ):
        assert responses[path].headers["content-type"] == "image/png"
        assert responses[path].content.startswith(b"\x89PNG\r\n\x1a\n")
    for response in responses.values():
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert "manifest-src 'self'" in response.headers["content-security-policy"]
    assert capabilities.headers["cache-control"] == "private, no-store"
    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "Media Manager API"


async def test_cross_site_browser_upload_is_rejected_before_job_reservation(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)

    async with running_client(app) as client:
        response = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            content=SOURCE_CONTENT,
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        missing_origin = await client.post(
            "/v1/jobs",
            params={"target": "video-mp4"},
            content=SOURCE_CONTENT,
            headers={"User-Agent": "Mozilla/5.0", "Sec-Fetch-Mode": "cors"},
        )

    assert_error(response, 403, "CROSS_SITE_REQUEST", "Cross-site requests are not allowed")
    assert_error(
        missing_origin,
        403,
        "CROSS_SITE_REQUEST",
        "Cross-site requests are not allowed",
    )
    assert processor.inputs == []
    assert list(settings.work_dir.iterdir()) == []


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
            headers={
                "Content-Type": "video/quicktime",
                "Upload-Filename": "Holiday.Final.MOV",
            },
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
            "quality_percent": 100,
            "resolution": "720p",
            "audio": "drop",
            "created_at": created["created_at"],
            "expires_at": None,
            "status_url": expected_status_url,
            "input": None,
            "output": None,
            "error": None,
            "progress": {"stage": "queued", "percent": None},
        }
        assert datetime.fromisoformat(created["created_at"]).tzinfo is not None

        await asyncio.wait_for(processor.convert_started.wait(), timeout=5)
        processing_response = await client.get(expected_status_url)
        assert processing_response.status_code == 200
        processing = processing_response.json()
        assert processing == {
            **created,
            "state": "processing",
            "progress": {"stage": "converting", "percent": 42},
        }
        assert processor.inputs == [SOURCE_CONTENT]
        assert processor.options[0].model_dump(mode="json") == {
            "target": "video-mp4",
            "quality": "high",
            "quality_percent": 100,
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
            "quality_percent": 100,
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
                "filename": "Holiday.Final-converted.mp4",
                "media_type": "video/mp4",
                "download_url": ready["output"]["download_url"],
                "width": 1280,
                "height": 720,
                "duration_ms": 12_345,
            },
            "error": None,
            "progress": None,
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
        assert ready["output"]["filename"] == "Holiday.Final-converted.mp4"
        assert "Holiday.Final-converted.mp4" in download.headers["content-disposition"]

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
