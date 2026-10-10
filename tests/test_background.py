from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_api import SOURCE_CONTENT, FakeProcessor, make_settings, running_client, wait_for_state

from media_manager.app import create_app
from media_manager.jobs import JobManager
from media_manager.models import ConversionOptions, MediaClass, MediaMetadata, Target

pytestmark = pytest.mark.asyncio


class PreviewProcessor(FakeProcessor):
    async def inspect(self, path, cancel_event=None):
        return MediaMetadata(
            bytes=path.stat().st_size,
            media_class=MediaClass.VIDEO,
            container="mp4",
            width=1920,
            height=1080,
            duration_ms=12_000,
        )

    async def compress(self, *args, options=None, **kwargs):
        return await super().compress(*args, **kwargs)


async def test_accepted_job_survives_client_disconnect_and_restart(tmp_path: Path):
    settings = make_settings(tmp_path)
    processor = FakeProcessor(blocked=True)
    app = create_app(settings, processor=processor)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post("/v1/compressions", content=SOURCE_CONTENT)
            assert created.status_code == 202
            job_id = created.json()["id"]
            await asyncio.wait_for(processor.convert_started.wait(), 2)
            history = (await client.get("/v1/history")).json()
            assert history["items"][0]["job"]["state"] == "processing"
        # Disconnecting the caller must not cancel the server's processing task.
        assert not app.state.jobs._jobs[job_id].cancel_event.is_set()
    directory = settings.work_dir / job_id
    assert (directory / "input").read_bytes() == SOURCE_CONTENT
    assert json.loads((directory / "pending.json").read_text())["state"] == "processing"
    restarted = create_app(settings, processor=FakeProcessor())
    async with running_client(restarted) as client:
        for _ in range(100):
            view = (await client.get(f"/v1/compressions/{job_id}")).json()
            if view["state"] == "ready":
                break
            await asyncio.sleep(0.01)
        assert view["state"] == "ready"
        assert (await client.get(f"/v1/compressions/{job_id}/content")).status_code == 200
        assert (await client.get("/v1/history")).json()["total"] == 1
        assert not (directory / "input").exists()


async def test_repeated_processing_interruptions_become_retained_failure(tmp_path: Path):
    settings = make_settings(tmp_path)
    manager = JobManager(settings, FakeProcessor(blocked=True))
    await manager.start()
    job = await manager.reserve(
        "owner", ConversionOptions(target=Target.VIDEO_MP4), len(SOURCE_CONTENT)
    )
    job.input_path.write_bytes(SOURCE_CONTENT)
    await manager.enqueue(job.id, len(SOURCE_CONTENT))
    await asyncio.wait_for(manager._processor.convert_started.wait(), 2)
    await manager.stop()
    for _ in range(2):
        manager = JobManager(settings, FakeProcessor(blocked=True))
        await manager.start()
        await asyncio.wait_for(manager._processor.convert_started.wait(), 2)
        await manager.stop()
    manager = JobManager(settings, FakeProcessor())
    await manager.start()
    try:
        view = await manager.get(job.id, "owner")
        assert view.state == "failed"
        assert view.error.code == "RECOVERY_LIMIT_EXCEEDED"
        assert not job.input_path.exists()
        entries, total = await manager.history("owner", 50, 0)
        assert total == 1 and entries[0].job.state == "failed"
    finally:
        await manager.stop()


async def test_prepared_upload_restores_for_review_and_starts_idempotently(tmp_path: Path):
    settings = make_settings(tmp_path)
    async with running_client(create_app(settings, processor=PreviewProcessor())) as client:
        created = await client.post(
            "/v1/jobs?target=video-mp4&prepare_only=true",
            content=SOURCE_CONTENT,
            headers={"X-Media-Source": "shortcut"},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        assert created.json()["state"] == "awaiting_review"
        assert created.json()["origin"] == "shortcut"
        assert (await client.get("/v1/history")).json()["total"] == 1

        inspection = await client.get(f"/v1/jobs/{job_id}/inspect")
        assert inspection.status_code == 200
        assert inspection.json()["bytes"] == len(SOURCE_CONTENT)
        assert inspection.json()["duration_ms"] == 12_000
        estimate = await client.post(f"/v1/jobs/{job_id}/estimate", json={})
        assert estimate.status_code == 200
        assert estimate.json()["method"] == "fast"
        assert estimate.json()["size_max_bytes"] >= estimate.json()["size_min_bytes"]
    async with running_client(create_app(settings, processor=PreviewProcessor())) as client:
        assert (await client.get(f"/v1/jobs/{job_id}")).json()["state"] == "awaiting_review"
        started = await client.post(f"/v1/jobs/{job_id}/start", json={})
        assert started.status_code == 202
        again = await client.post(f"/v1/jobs/{job_id}/start", json={})
        assert again.status_code == 202
        await wait_for_state(client, job_id, "ready")
        assert (await client.get("/v1/history")).json()["total"] == 1


async def test_fully_received_review_source_uses_owner_retention_not_partial_upload_ttl(tmp_path):
    from datetime import UTC, datetime

    settings = make_settings(tmp_path, result_ttl_seconds=604800, upload_session_ttl_seconds=60)
    async with running_client(create_app(settings, processor=PreviewProcessor())) as client:
        prepared = await client.post(
            "/v1/jobs?target=video-mp4&prepare_only=true", content=SOURCE_CONTENT
        )
        assert prepared.status_code == 202
        expiry = datetime.fromisoformat(prepared.json()["expires_at"])
        remaining = (expiry - datetime.now(UTC)).total_seconds()
        assert 604790 <= remaining <= 604800
        await client.put("/v1/history/settings", json={"retention_days": 3})
        await client.delete(f"/v1/jobs/{prepared.json()['id']}")
        updated = await client.post(
            "/v1/jobs?target=video-mp4&prepare_only=true", content=SOURCE_CONTENT
        )
        updated_expiry = datetime.fromisoformat(updated.json()["expires_at"])
        assert 259190 <= (updated_expiry - datetime.now(UTC)).total_seconds() <= 259200


async def test_verified_aliases_share_existing_records_but_other_users_cannot(tmp_path: Path):
    settings = make_settings(tmp_path, principal_owners={"service:phone": "user:owner"})
    manager = JobManager(settings, FakeProcessor())
    await manager.start()
    try:
        job = await manager.reserve("service:phone", ConversionOptions(target=Target.VIDEO_MP4))
        job.input_path.write_bytes(SOURCE_CONTENT)
        await manager.enqueue(job.id, len(SOURCE_CONTENT))
        for _ in range(100):
            view = await manager.get(job.id, "user:owner")
            if view.state == "ready":
                break
            await asyncio.sleep(0.01)
        assert view.state == "ready"
        assert (await manager.history("user:owner", 50, 0))[1] == 1
        from media_manager.errors import ApiError

        with pytest.raises(ApiError) as forbidden:
            await manager.get(job.id, "user:someone-else")
        assert forbidden.value.status_code == 404
        await manager.set_retention_days("service:phone", 3)
        assert await manager.get_retention_days("user:owner") == 3
    finally:
        await manager.stop()


async def test_download_alert_excludes_preview_and_keeps_result(tmp_path: Path):
    app = create_app(make_settings(tmp_path), processor=FakeProcessor())
    events = []
    async with running_client(app) as client:

        async def sink(*event):
            events.append(event)

        app.state.jobs.event_sink = sink
        created = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT)
        job_id = created.json()["id"]
        await wait_for_state(client, job_id, "ready")
        assert (await client.get(f"/v1/jobs/{job_id}/content?preview=true")).status_code == 200
        assert [event[0] for event in events] == ["ready"]
        assert (await client.get(f"/v1/jobs/{job_id}/content")).status_code == 200
        assert [event[0] for event in events] == ["ready", "download"]
        assert (await client.get("/v1/history")).json()["total"] == 1


async def test_corrupt_pending_input_never_restores(tmp_path: Path):
    settings = make_settings(tmp_path)
    async with running_client(create_app(settings, processor=PreviewProcessor())) as client:
        created = await client.post(
            "/v1/jobs?target=video-mp4&prepare_only=true", content=SOURCE_CONTENT
        )
        job_id = created.json()["id"]
    (settings.work_dir / job_id / "input").write_bytes(b"partial")
    async with running_client(create_app(settings, processor=PreviewProcessor())) as client:
        assert (await client.get("/v1/history")).json()["total"] == 0
        assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 404


async def test_cancel_during_acceptance_keeps_source_and_starts_worker(tmp_path: Path):
    settings = make_settings(tmp_path)
    processor = FakeProcessor(blocked=True)
    manager = JobManager(settings, processor)
    await manager.start()
    job = await manager.reserve("owner", ConversionOptions(target=Target.VIDEO_MP4))
    job.input_path.write_bytes(SOURCE_CONTENT)
    persisting = asyncio.Event()
    release = asyncio.Event()
    persist = manager._persist_pending

    async def delayed(record):
        persisting.set()
        await release.wait()
        await persist(record)

    manager._persist_pending = delayed
    caller = asyncio.create_task(manager.enqueue(job.id, len(SOURCE_CONTENT)))
    await persisting.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    cleanup = asyncio.create_task(manager.discard(job.id))
    release.set()
    await cleanup
    await asyncio.wait_for(processor.convert_started.wait(), 2)
    assert job.input_path.read_bytes() == SOURCE_CONTENT
    assert (job.directory / "pending.json").is_file()
    processor.release.set()
    await manager.stop()


async def test_prepared_compression_child_keeps_settings_after_restart(tmp_path: Path):
    from media_manager.models import CompressionOptions

    settings = make_settings(tmp_path)
    source = tmp_path / "download.mp4"
    source.write_bytes(SOURCE_CONTENT)
    options = CompressionOptions(target_bytes=750_000, target=Target.VIDEO_WEBM, audio="drop")
    manager = JobManager(settings, PreviewProcessor())
    await manager.start()
    child = await manager.prepare_from_path(
        source, "owner", source.name, compression_options=options, origin="metube"
    )
    await manager.stop()
    restored = JobManager(settings, PreviewProcessor())
    await restored.start()
    view = await restored.get(child.id, "owner")
    assert view.state.value == "awaiting_review"
    assert view.options == options
    assert view.origin == "metube"
    await restored.stop()


async def test_combined_processing_uses_compression_controls_and_conversion_format(tmp_path):
    from media_manager.models import CompressionOptions

    manager = JobManager(make_settings(tmp_path), PreviewProcessor(blocked=True))
    await manager.start()
    job = await manager.reserve("owner", ConversionOptions(target=Target.VIDEO_MP4))
    job.input_path.write_bytes(SOURCE_CONTENT)
    await manager.prepare(job.id, len(SOURCE_CONTENT))
    compression = CompressionOptions(
        target=Target.VIDEO_MP4, quality_percent=76, resolution="480p",
        audio="drop", target_bytes=700_000,
    )
    try:
        started = await manager.start_prepared(
            job.id, "owner", ConversionOptions(target=Target.VIDEO_WEBM), compression
        )
        assert started.options.target == Target.VIDEO_WEBM
        assert started.options.quality_percent == 76
        assert started.options.resolution.value == "480p"
        assert started.options.audio.value == "drop"
        assert started.options.target_bytes == 700_000
    finally:
        await manager.stop()


async def test_terminal_storage_outage_preserves_accepted_source_for_restart(tmp_path, monkeypatch):
    from media_manager import jobs as jobs_module

    settings = make_settings(tmp_path)
    original_write = jobs_module._atomic_write_json

    def unavailable_history(path, payload, mode=0o600):
        if path.name == "history.json":
            raise OSError("synthetic history storage outage")
        original_write(path, payload, mode)

    monkeypatch.setattr(jobs_module, "_atomic_write_json", unavailable_history)
    async with running_client(create_app(settings, processor=FakeProcessor())) as client:
        created = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT)
        job_id = created.json()["id"]
        failed = await wait_for_state(client, job_id, "failed")
        assert failed["error"]["code"] == "RESULT_PERSISTENCE_FAILED"
        directory = settings.work_dir / job_id
        assert (directory / "input").read_bytes() == SOURCE_CONTENT
        assert (directory / "pending.json").is_file()
    monkeypatch.setattr(jobs_module, "_atomic_write_json", original_write)
    async with running_client(create_app(settings, processor=FakeProcessor())) as client:
        await wait_for_state(client, job_id, "ready")
        assert (await client.get(f"/v1/jobs/{job_id}/content")).status_code == 200
        assert not (directory / "input").exists()
        assert (await client.get("/v1/history")).json()["total"] == 1


async def test_processing_preempts_sample_and_unchanged_estimate_is_cached(tmp_path, monkeypatch):
    from media_manager.estimates import EstimateService

    sampling = asyncio.Event()
    stopped = asyncio.Event()
    calls = 0

    async def blocked_sample(self, *args):
        nonlocal calls
        calls += 1
        sampling.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(EstimateService, "refine", blocked_sample)
    settings = make_settings(tmp_path)
    app = create_app(settings, processor=PreviewProcessor())
    async with running_client(app) as client:
        prepared = await client.post(
            "/v1/jobs?target=video-mp4&prepare_only=true", content=SOURCE_CONTENT
        )
        job_id = prepared.json()["id"]
        first = (await client.post(f"/v1/jobs/{job_id}/estimate", json={})).json()
        await asyncio.wait_for(sampling.wait(), 1)
        cached = (await client.post(f"/v1/jobs/{job_id}/estimate", json={})).json()
        assert first == cached and calls == 1
        started = await client.post(f"/v1/jobs/{job_id}/start", json={})
        assert started.status_code == 202
        await wait_for_state(client, job_id, "ready")
        await asyncio.wait_for(stopped.wait(), 1)
        assert (await client.get(f"/v1/jobs/{job_id}/estimate")).json()["status"] == "fallback"


async def test_invalid_estimate_is_safe_and_rejects_oversized_target(tmp_path: Path):
    settings = make_settings(tmp_path)
    async with running_client(create_app(settings, processor=PreviewProcessor())) as client:
        created = await client.post("/v1/compressions?prepare_only=true", content=SOURCE_CONTENT)
        job_id = created.json()["id"]
        estimate = await client.post(
            f"/v1/jobs/{job_id}/estimate",
            json={"compression_options": {"target_bytes": settings.max_output_bytes + 1}},
        )
        assert estimate.status_code == 422
        invalid = await client.post(
            f"/v1/jobs/{job_id}/start",
            json={"conversion_options": {"target": "audio-mp3", "resolution": "1080p"}},
        )
        assert invalid.status_code == 422
        assert (await client.get(f"/v1/compressions/{job_id}")).json()["state"] == "awaiting_review"
