from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import Request
from test_api import SOURCE_CONTENT, FakeProcessor, make_settings, wait_for_state

from media_manager import jobs as jobs_module
from media_manager.app import create_app
from media_manager.auth import Principal, get_principal
from media_manager.config import AuthMode, Settings

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def client_for(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        yield client


def principal_override(app):
    async def principal_from_header(request: Request) -> Principal:
        return Principal(id=f"test:{request.headers.get('X-Test-Principal', 'owner')}")

    app.dependency_overrides[get_principal] = principal_from_header


async def test_history_isolated_and_paginated_for_terminal_jobs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_live_jobs=4)
    app = create_app(settings, processor=FakeProcessor())
    principal_override(app)
    owner = {"X-Test-Principal": "owner"}
    other = {"X-Test-Principal": "other"}

    async with client_for(app) as client:
        first = await client.post(
            "/v1/jobs?target=video-mp4",
            content=SOURCE_CONTENT,
            headers={**owner, "Upload-Filename": "first.mov"},
        )
        second = await client.post(
            "/v1/compressions",
            content=SOURCE_CONTENT,
            headers={**owner, "Upload-Filename": "second.mov"},
        )
        foreign = await client.post(
            "/v1/jobs?target=video-mp4",
            content=SOURCE_CONTENT,
            headers=other,
        )
        assert first.status_code == second.status_code == foreign.status_code == 202
        await wait_for_state(client, first.json()["id"], "ready", headers=owner)
        second_id = second.json()["id"]
        for _ in range(100):
            second_status = await client.get(f"/v1/compressions/{second_id}", headers=owner)
            assert second_status.status_code == 200
            if second_status.json()["state"] == "ready":
                break
        else:
            pytest.fail("Compression did not become ready")
        await wait_for_state(client, foreign.json()["id"], "ready", headers=other)

        history = await client.get("/v1/history?limit=1", headers=owner)
        assert history.status_code == 200
        assert history.json()["total"] == 2
        assert len(history.json()["items"]) == 1
        assert history.json()["items"][0]["kind"] == "compression"
        assert (await client.get("/v1/history", headers=other)).json()["total"] == 1
        assert (await client.get("/v1/history?offset=1", headers=owner)).json()["total"] == 2

        first_id = first.json()["id"]
        downloaded = await client.get(f"/v1/jobs/{first_id}/content", headers=owner)
        assert downloaded.status_code == 200
        assert (await client.delete(f"/v1/jobs/{first_id}", headers=owner)).status_code == 204
        assert (await client.get("/v1/history", headers=owner)).json()["total"] == 1


async def test_history_and_preferences_restore_across_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, result_ttl_seconds=60)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)
    principal_override(app)
    owner = {"X-Test-Principal": "owner"}

    async with client_for(app) as client:
        preference = await client.put(
            "/v1/history/settings",
            json={"retention_days": 2},
            headers=owner,
        )
        assert preference.json() == {"retention_days": 2}
        created = await client.post(
            "/v1/jobs?target=video-mp4",
            content=SOURCE_CONTENT,
            headers=owner,
        )
        await wait_for_state(client, created.json()["id"], "ready", headers=owner)
        job_id = created.json()["id"]
        assert (settings.work_dir / job_id / "history.json").stat().st_mode & 0o777 == 0o600
        assert not (settings.work_dir / job_id / "input").exists()

    restarted = create_app(
        replace(settings, public_base_url="https://new-origin.example"),
        processor=FakeProcessor(),
    )
    principal_override(restarted)
    async with client_for(restarted) as client:
        assert (await client.get("/v1/history/settings", headers=owner)).json() == {
            "retention_days": 2
        }
        history = await client.get("/v1/history", headers=owner)
        assert history.status_code == 200
        restored = history.json()["items"][0]["job"]
        assert restored["id"] == job_id
        assert restored["output"]["download_url"] == (
            f"https://new-origin.example/v1/jobs/{job_id}/content"
        )
        assert (await client.get(f"/v1/jobs/{job_id}", headers=owner)).status_code == 200


async def test_default_settings_and_strict_preference_validation(tmp_path: Path) -> None:
    default = Settings(auth_mode=AuthMode.DISABLED, public_base_url="https://media.example.test")
    assert default.result_ttl_seconds == 7 * 24 * 60 * 60
    settings = make_settings(tmp_path)
    app = create_app(settings, processor=FakeProcessor())
    principal_override(app)

    async with client_for(app) as client:
        assert (await client.get("/v1/history/settings")).json() == {"retention_days": 1}
        for value in (0, 91, "2", True):
            response = await client.put("/v1/history/settings", json={"retention_days": value})
            assert response.status_code == 422


async def test_restart_discards_incomplete_and_corrupt_workspace_records(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.work_dir.mkdir(parents=True)
    incomplete = settings.work_dir / str(uuid4())
    incomplete.mkdir()
    (incomplete / "input").write_bytes(b"partial")
    corrupt = settings.work_dir / str(uuid4())
    corrupt.mkdir()
    (corrupt / "history.json").write_text("{not-json", encoding="utf-8")

    app = create_app(settings, processor=FakeProcessor())
    async with client_for(app) as client:
        assert (await client.get("/v1/history")).json() == {"items": [], "total": 0}

    assert not incomplete.exists()
    assert not corrupt.exists()


async def test_expiry_is_enforced_before_janitor_and_removes_result(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, result_ttl_seconds=1, cleanup_interval_seconds=3600)
    app = create_app(settings, processor=FakeProcessor())

    async with client_for(app) as client:
        created = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT)
        job_id = created.json()["id"]
        await wait_for_state(client, job_id, "ready")
        await asyncio.sleep(1.1)

        content = await client.get(f"/v1/jobs/{job_id}/content")
        assert content.status_code == 410
        assert content.json() == {
            "error": {"code": "RESULT_EXPIRED", "message": "Converted media has expired"}
        }
        assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 404
        assert (await client.get("/v1/history")).json() == {"items": [], "total": 0}

    assert not (settings.work_dir / job_id).exists()


async def test_retention_is_owned_persistent_and_only_affects_future_jobs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, result_ttl_seconds=120, max_live_jobs=2)
    app = create_app(settings, processor=FakeProcessor())
    principal_override(app)
    owner = {"X-Test-Principal": "owner"}
    other = {"X-Test-Principal": "other"}

    async with client_for(app) as client:
        old = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT, headers=owner)
        old_view = await wait_for_state(client, old.json()["id"], "ready", headers=owner)
        owner_setting = await client.put(
            "/v1/history/settings",
            json={"retention_days": 2},
            headers=owner,
        )
        assert owner_setting.json() == {
            "retention_days": 2
        }
        assert (await client.get("/v1/history/settings", headers=other)).json() == {
            "retention_days": 1
        }
        new = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT, headers=owner)
        new_view = await wait_for_state(client, new.json()["id"], "ready", headers=owner)

        old_expiry = datetime.fromisoformat(old_view["expires_at"])
        new_expiry = datetime.fromisoformat(new_view["expires_at"])
        assert new_expiry - old_expiry > timedelta(days=1)
        other_setting = await client.put(
            "/v1/history/settings",
            json={"retention_days": 5},
            headers=other,
        )
        assert other_setting.json() == {
            "retention_days": 5
        }
        assert (await client.get("/v1/history/settings", headers=owner)).json() == {
            "retention_days": 2
        }

    restarted = create_app(settings, processor=FakeProcessor())
    principal_override(restarted)
    async with client_for(restarted) as client:
        assert (await client.get("/v1/history/settings", headers=owner)).json() == {
            "retention_days": 2
        }
        assert (await client.get("/v1/history/settings", headers=other)).json() == {
            "retention_days": 5
        }
        assert (await client.get("/v1/history", headers=owner)).json()["total"] == 2


async def test_corrupt_symlink_and_traversal_records_are_discarded(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, processor=FakeProcessor())

    async with client_for(app) as client:
        created = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT)
        job_id = created.json()["id"]
        await wait_for_state(client, job_id, "ready")
    source_record = json.loads(
        (settings.work_dir / job_id / "history.json").read_text(encoding="utf-8")
    )
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"private")

    traversal_dir = settings.work_dir / str(uuid4())
    traversal_dir.mkdir()
    traversal = dict(source_record, id=traversal_dir.name, output_basename="../outside.bin")
    (traversal_dir / "history.json").write_text(json.dumps(traversal), encoding="utf-8")
    symlink_dir = settings.work_dir / str(uuid4())
    symlink_dir.mkdir()
    symlink_record = dict(source_record, id=symlink_dir.name, output_basename="result.mp4")
    (symlink_dir / "history.json").write_text(json.dumps(symlink_record), encoding="utf-8")
    os.symlink(outside, symlink_dir / "result.mp4")
    linked_history_dir = settings.work_dir / str(uuid4())
    linked_history_dir.mkdir()
    os.symlink(settings.work_dir / job_id / "history.json", linked_history_dir / "history.json")

    restarted = create_app(settings, processor=FakeProcessor())
    async with client_for(restarted) as client:
        history = await client.get("/v1/history")
        assert history.status_code == 200
        assert [entry["id"] for entry in history.json()["items"]] == [job_id]

    assert not traversal_dir.exists()
    assert not symlink_dir.exists()
    assert not linked_history_dir.exists()
    assert outside.read_bytes() == b"private"


async def test_restart_removes_extra_partial_files_from_valid_record(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, processor=FakeProcessor())
    async with client_for(app) as client:
        created = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT)
        job_id = created.json()["id"]
        await wait_for_state(client, job_id, "ready")
    directory = settings.work_dir / job_id
    (directory / "partial.tmp").write_bytes(b"stale")
    (directory / "input").write_bytes(b"stale source")

    restarted = create_app(settings, processor=FakeProcessor())
    async with client_for(restarted) as client:
        assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 200

    assert not (directory / "partial.tmp").exists()
    assert not (directory / "input").exists()
    assert (directory / "history.json").exists()


async def test_delete_during_blocked_persistence_cannot_resurrect_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)
    original_write = jobs_module._atomic_write_json
    write_started = threading.Event()
    release_write = threading.Event()

    def blocked_write(path: Path, payload: object, mode: int = 0o600) -> None:
        write_started.set()
        assert release_write.wait(5)
        original_write(path, payload, mode)

    monkeypatch.setattr(jobs_module, "_atomic_write_json", blocked_write)
    async with client_for(app) as client:
        created = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT)
        job_id = created.json()["id"]
        assert await asyncio_wait_thread_event(write_started)
        deleting = asyncio.create_task(client.delete(f"/v1/jobs/{job_id}"))
        await asyncio.sleep(0)
        assert not deleting.done()
        release_write.set()
        deleted = await deleting
        assert deleted.status_code == 204
        assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 404

    assert not (settings.work_dir / job_id).exists()


async def test_persistence_failure_becomes_safe_failure_and_worker_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path, max_live_jobs=1)
    processor = FakeProcessor()
    app = create_app(settings, processor=processor)
    original_write = jobs_module._atomic_write_json
    calls = 0

    def fail_once(path: Path, payload: object, mode: int = 0o600) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated persistence failure")
        original_write(path, payload, mode)

    monkeypatch.setattr(jobs_module, "_atomic_write_json", fail_once)
    async with client_for(app) as client:
        failed_job = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT)
        failed_view = await wait_for_state(client, failed_job.json()["id"], "failed")
        assert failed_view["error"]["code"] == "RESULT_PERSISTENCE_FAILED"
        monkeypatch.setattr(jobs_module, "_atomic_write_json", original_write)
        next_job = await client.post("/v1/jobs?target=video-mp4", content=SOURCE_CONTENT)
        next_view = await wait_for_state(client, next_job.json()["id"], "ready")
        assert next_view["state"] == "ready"


async def test_preference_storage_failure_preserves_previous_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, processor=FakeProcessor())
    principal_override(app)
    manager = None

    async with client_for(app) as client:
        manager = app.state.jobs
        initial_setting = await client.put(
            "/v1/history/settings", json={"retention_days": 2}
        )
        assert initial_setting.status_code == 200

        async def fail_write(_preferences: dict[str, int]) -> None:
            raise OSError("simulated preference storage failure")

        monkeypatch.setattr(manager, "_write_preferences", fail_write)
        response = await client.put("/v1/history/settings", json={"retention_days": 3})
        assert response.status_code == 507
        assert (await client.get("/v1/history/settings")).json() == {"retention_days": 2}


async def asyncio_wait_thread_event(event: threading.Event) -> bool:
    return await asyncio.to_thread(event.wait, 5)
