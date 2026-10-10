from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from media_manager.integration_models import DownloadRequest, DownloadType, DownloadWorkflow
from media_manager.models import CompressionOptions, ConversionOptions, Target
from media_manager.telegram import TelegramClient, TelegramService


@pytest.mark.asyncio
async def test_telegram_file_transfer_uses_configured_origin_and_streams_bounded(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "documents/a.bin"}}
            )
        if request.url.path == "/file/bot123:token/documents/a.bin":
            return httpx.Response(200, content=b"abc")
        return httpx.Response(404)

    settings = SimpleNamespace(
        telegram_bot_token="123:token", telegram_api_base_url="https://api.telegram.org"
    )
    client = TelegramClient(settings, transport=httpx.MockTransport(handler))
    await client.start()
    destination = tmp_path / "upload.bin"
    assert await client.download_file("file-id", destination, 10) == 3
    assert destination.read_bytes() == b"abc"
    assert {request.url.host for request in seen} == {"api.telegram.org"}
    assert all("123:token" in request.url.path for request in seen)
    await client.close()


@pytest.mark.asyncio
async def test_telegram_rejects_unsafe_file_path(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"file_path": "../../attacker/file"}}
        )

    client = TelegramClient(
        SimpleNamespace(
            telegram_bot_token="token", telegram_api_base_url="https://api.telegram.org"
        ),
        transport=httpx.MockTransport(handler),
    )
    await client.start()
    with pytest.raises(ValueError, match="unsafe"):
        await client.download_file("id", tmp_path / "result", 10)
    await client.close()


@pytest.mark.asyncio
async def test_telegram_local_file_path_must_be_under_read_only_shared_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "telegram-files"
    root.mkdir()
    source = root / "file.bin"
    source.write_bytes(b"shared")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"file_path": str(source)}},
        )

    settings = SimpleNamespace(
        telegram_bot_token="token",
        telegram_api_base_url="http://telegram.local",
        telegram_file_root=root,
    )
    client = TelegramClient(settings, transport=httpx.MockTransport(handler))
    await client.start()
    destination = tmp_path / "private" / "copy.bin"
    assert await client.download_file("id", destination, 20) == 6
    assert destination.read_bytes() == b"shared"
    assert destination.stat().st_mode & 0o777 == 0o600
    await client.close()


@pytest.mark.asyncio
async def test_service_ignores_non_private_and_unauthorized_updates(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        telegram_bot_token="token",
        telegram_api_base_url="https://api.telegram.org",
        telegram_chat_id=111,
        telegram_user_id=222,
        telegram_owner_id="owner",
    )
    service = TelegramService(settings, object(), object(), object())
    calls: list[tuple[str, dict]] = []

    async def call(method: str, payload: dict | None = None) -> dict:
        calls.append((method, payload or {}))
        return {}

    service.client.call = call  # type: ignore[method-assign]
    await service._handle_message(
        {"chat": {"id": 111, "type": "group"}, "from": {"id": 222}, "text": "/start"}
    )
    await service._handle_message(
        {"chat": {"id": 111, "type": "private"}, "from": {"id": 333}, "text": "/start"}
    )
    assert calls == []
    await service._handle_message(
        {"chat": {"id": 111, "type": "private"}, "from": {"id": 222}, "text": "/start"}
    )
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["chat_id"] == 111


@pytest.mark.asyncio
async def test_telegram_session_does_not_persist_source_url(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        telegram_bot_token="token",
        telegram_api_base_url="https://api.telegram.org",
        telegram_chat_id=111,
        telegram_user_id=222,
        telegram_owner_id="owner",
    )
    service = TelegramService(settings, object(), object(), object())
    service._state_dir.mkdir(parents=True)
    service._sessions["111"] = {
        "url": "https://media.example/?token=secret",
        "phase": "download_options",
    }
    await service._save_state()
    saved = service._state_file.read_text()
    assert "media.example" not in saved
    assert "secret" not in saved


@pytest.mark.asyncio
async def test_disabled_telegram_service_does_not_create_or_write_state(tmp_path: Path) -> None:
    settings = SimpleNamespace(work_dir=tmp_path, telegram_bot_token=None)
    service = TelegramService(settings, object(), object(), object())
    await service.start()
    await service.stop()
    assert not (tmp_path / ".integrations").exists()


def test_advanced_controls_cover_conversion_parameters_without_json() -> None:
    service = TelegramService(SimpleNamespace(), object(), object(), object())
    rows = service._advanced_keyboard("convert", {"conversion_options": {"target": "video-mp4"}})[
        "inline_keyboard"
    ]
    callbacks = {button["callback_data"] for row in rows for button in row}
    assert "adv:target:video-mp4" in callbacks
    assert "adv:resolution:1080p" in callbacks
    assert "adv:audio:drop" in callbacks
    assert "adv:quality:high" in callbacks
    assert "adv:quality_percent" in callbacks
    assert "adv:use" in callbacks


def test_telegram_menu_rows_and_choices_match_request_and_conversion_models() -> None:
    service = TelegramService(SimpleNamespace(), object(), object(), object())
    for source_type, formats, codecs in (
        ("video", ("ios", "mp4", "webm", "mkv", "auto"), ("h264", "h265", "vp9", "av1", "auto")),
        ("audio", ("audio", "auto", "m4a", "mp3", "opus", "wav", "flac"), ("aac", "opus", "auto")),
        ("thumbnail", ("jpg", "auto"), ("auto",)),
    ):
        session = {"source_type": source_type}
        keyboard = service._advanced_keyboard("download", session)["inline_keyboard"]
        assert all(isinstance(row, list) and 1 <= len(row) <= 3 for row in keyboard)
        callbacks = {button["callback_data"] for row in keyboard for button in row}
        assert {f"adv:format:{value}" for value in formats} <= callbacks
        assert {f"adv:codec:{value}" for value in codecs} <= callbacks
        for fmt in formats:
            for codec in codecs:
                DownloadRequest(
                    url="https://video.example/clip",
                    format=fmt,
                    download_type=DownloadType(source_type),
                    codec=codec,
                )

    session = {"job_id": "prepared", "workflow": "convert"}
    keyboard = service._advanced_keyboard("convert", session)["inline_keyboard"]
    assert all(isinstance(row, list) and 1 <= len(row) <= 3 for row in keyboard)
    callbacks = {button["callback_data"] for row in keyboard for button in row}
    targets = {item.value for item in Target}
    assert {f"adv:target:{target}" for target in targets} <= callbacks
    workflows = {f"adv:workflow:{name}" for name in ("convert", "compress", "convert_compress")}
    assert workflows <= callbacks
    for target in targets:
        ConversionOptions(target=target)

    compression = service._advanced_keyboard("compress", session)["inline_keyboard"]
    compression_callbacks = {button["callback_data"] for row in compression for button in row}
    compression_options = {
        "adv:resolution:source",
        "adv:audio:keep",
        "adv:quality:balanced",
        "adv:target_mb",
    }
    assert compression_options <= compression_callbacks
    assert CompressionOptions().target_bytes == 20_000_000


def test_audio_and_thumbnail_source_switches_choose_valid_defaults() -> None:
    session = {"source_type": "video", "source_format": "ios", "source_codec": "h264"}
    for source_type, expected_format, expected_codec in (
        ("audio", "m4a", "auto"),
        ("thumbnail", "jpg", "auto"),
    ):
        session["source_type"] = source_type
        TelegramService._normalize_source_choices(session)
        assert (session["source_format"], session["source_codec"]) == (
            expected_format,
            expected_codec,
        )
        DownloadRequest(
            url="https://video.example/clip",
            download_type=source_type,
            format=session["source_format"],
            codec=session["source_codec"],
        )


@pytest.mark.asyncio
async def test_telegram_update_correlation_and_outbox_deduplication(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        telegram_bot_token="token",
        telegram_api_base_url="https://api.telegram.org",
        telegram_chat_id=111,
        telegram_user_id=222,
        telegram_owner_id="owner",
    )
    service = TelegramService(settings, object(), object(), object())
    service._state_dir.mkdir(parents=True)
    service._processing_update_key = "731"
    assert service._download_idempotency_key(111) == "telegram:111:731"
    assert service._upload_parent_id(111) == service._upload_parent_id(111)

    await service.notify(
        "download_ready", "5f5b5443-66bf-46e8-b33a-c8053220f2c9", "owner", "clip.mp4", 10, None
    )
    await service.notify(
        "download_ready", "5f5b5443-66bf-46e8-b33a-c8053220f2c9", "owner", "clip.mp4", 10, None
    )
    assert len(service._outbox) == 1
    service._delivered_events.append(service._event_key(service._outbox[0]))
    service._outbox.clear()
    await service.notify(
        "download_ready", "5f5b5443-66bf-46e8-b33a-c8053220f2c9", "owner", "clip.mp4", 10, None
    )
    assert service._outbox == []


@pytest.mark.asyncio
async def test_notify_only_persists_and_wakes_worker_without_waiting_for_delivery(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        telegram_bot_token="token",
        telegram_api_base_url="https://api.telegram.org",
        telegram_chat_id=111,
        telegram_user_id=222,
        telegram_owner_id="owner",
    )
    service = TelegramService(settings, object(), object(), object())
    service._state_dir.mkdir(parents=True)
    service._started = True
    reached_delivery = False

    async def blocked_flush() -> None:
        nonlocal reached_delivery
        reached_delivery = True
        await asyncio.Event().wait()

    service._flush_outbox = blocked_flush  # type: ignore[method-assign]
    await asyncio.wait_for(
        service.notify(
            "ready", "5f5b5443-66bf-46e8-b33a-c8053220f2c9", "owner", "clip.mp4", 10, None
        ),
        timeout=0.1,
    )
    assert reached_delivery is False
    assert service._outbox_wakeup.is_set()


@pytest.mark.asyncio
async def test_history_lists_owner_scoped_retrieve_process_and_delete_controls(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        telegram_bot_token="token",
        telegram_api_base_url="https://api.telegram.org",
        telegram_chat_id=111,
        telegram_user_id=222,
        telegram_owner_id="owner",
    )

    async def job_history(*_args):
        return (
            [
                {
                    "id": "5f5b5443-66bf-46e8-b33a-c8053220f2c9",
                    "kind": "conversion",
                    "completed_at": "2026-10-09T10:00:00+00:00",
                    "job": {
                        "state": "ready",
                        "created_at": "2026-10-09T09:00:00+00:00",
                        "output": {"filename": "clip.mp4"},
                    },
                }
            ],
            1,
        )

    async def download_history(*_args):
        return (
            [
                {
                    "id": "6f5b5443-66bf-46e8-b33a-c8053220f2c9",
                    "kind": "download",
                    "completed_at": "2026-10-09T10:01:00+00:00",
                    "job": {
                        "state": "awaiting_review",
                        "created_at": "2026-10-09T09:01:00+00:00",
                        "output": {"filename": "source.mp4"},
                        "workflow": "convert",
                    },
                },
                {
                    "id": "7f5b5443-66bf-46e8-b33a-c8053220f2c9",
                    "kind": "download",
                    "completed_at": "2026-10-09T10:02:00+00:00",
                    "job": {
                        "state": "ready",
                        "created_at": "2026-10-09T09:02:00+00:00",
                        "output": {"filename": "original.mp4"},
                        "workflow": "download",
                    },
                },
            ],
            2,
        )

    jobs = SimpleNamespace(history=job_history)
    downloads = SimpleNamespace(history=download_history)
    service = TelegramService(settings, jobs, downloads, object())
    calls: list[dict] = []

    async def call(method: str, payload: dict | None = None) -> dict:
        calls.append(payload or {})
        return {}

    service.client.call = call  # type: ignore[method-assign]
    await service._send_history(111)
    callbacks = {
        button["callback_data"]
        for row in calls[0]["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert "history:retrieve:job:5f5b5443-66bf-46e8-b33a-c8053220f2c9" in callbacks
    assert "history:process:6f5b5443-66bf-46e8-b33a-c8053220f2c9" in callbacks
    assert "history:process:7f5b5443-66bf-46e8-b33a-c8053220f2c9" in callbacks
    assert "history:delete:download:6f5b5443-66bf-46e8-b33a-c8053220f2c9" in callbacks


@pytest.mark.asyncio
async def test_history_process_prepares_estimate_review_without_starting_child(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        telegram_owner_id="owner",
        telegram_chat_id=111,
        telegram_user_id=222,
    )
    child_id = "5f5b5443-66bf-46e8-b33a-c8053220f2c9"
    child = SimpleNamespace(
        id=child_id,
        target=Target.VIDEO_MP4,
        quality="balanced",
        quality_percent=50,
        resolution="source",
        audio="keep",
    )

    class Jobs:
        async def get(self, *_args):
            return child

        async def estimate(self, *_args):
            self.estimated = True
            return SimpleNamespace(
                eta_min_seconds=1,
                eta_max_seconds=2,
                status="ready",
                size_min_bytes=10,
                size_max_bytes=20,
                confidence="medium",
                source=SimpleNamespace(bytes=100),
            )

        async def start_prepared(self, *_args):
            self.started = True
            return child

    class Downloads:
        async def get(self, *_args):
            return SimpleNamespace(workflow=DownloadWorkflow.CONVERT)

        async def process(self, *_args, **kwargs):
            self.process_args = kwargs
            return child

    jobs = Jobs()
    downloads = Downloads()
    service = TelegramService(settings, jobs, downloads, object())
    calls: list[dict] = []

    async def call(_method: str, payload: dict | None = None):
        calls.append(payload or {})
        return {"message_id": 8}

    service.client.call = call  # type: ignore[method-assign]
    service._state_dir.mkdir(parents=True)
    await service._handle_history_action(
        111,
        {"message_id": 4},
        "history:process:6f5b5443-66bf-46e8-b33a-c8053220f2c9",
    )

    assert downloads.process_args["auto_start"] is False
    assert jobs.estimated is True
    keyboard = calls[-1]["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["callback_data"] == f"file:start:{child_id}"
    assert service._sessions["111"]["reviews"][child_id]["phase"] == "review"
    assert not getattr(jobs, "started", False)

    service._watch_job = lambda *_args: None  # type: ignore[method-assign]
    await service._handle_callback(
        {
            "id": "start-callback",
            "from": {"id": 222},
            "message": {"message_id": 9, "chat": {"id": 111, "type": "private"}},
            "data": f"file:start:{child_id}",
        }
    )
    assert jobs.started is True


@pytest.mark.asyncio
async def test_default_url_download_sends_review_without_hijacking_newer_session(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(work_dir=tmp_path, telegram_owner_id="owner")
    child_id = "5f5b5443-66bf-46e8-b33a-c8053220f2c9"
    child = SimpleNamespace(
        id=child_id,
        target=Target.VIDEO_MP4,
        quality="balanced",
        quality_percent=50,
        resolution="source",
        audio="keep",
    )

    class Jobs:
        async def get(self, *_args):
            return child

        async def estimate(self, *_args):
            return SimpleNamespace(
                eta_min_seconds=1,
                eta_max_seconds=2,
                status="ready",
                size_min_bytes=10,
                size_max_bytes=20,
                confidence="medium",
                source=SimpleNamespace(bytes=100),
            )

    class Downloads:
        async def get(self, *_args):
            return SimpleNamespace(
                state=SimpleNamespace(value="awaiting_review"),
                output=SimpleNamespace(bytes=100),
                child_id=None,
            )

        async def process(self, *_args, **kwargs):
            self.process_args = kwargs
            return child

    jobs = Jobs()
    downloads = Downloads()
    service = TelegramService(settings, jobs, downloads, object())
    service._sessions["111"] = {"download_id": "newer-download", "url": "https://example"}
    calls: list[tuple[str, dict]] = []

    async def call(method: str, payload: dict | None = None):
        calls.append((method, payload or {}))
        return {"message_id": 10}

    service.client.call = call  # type: ignore[method-assign]
    service._state_dir.mkdir(parents=True)
    await service._watch_download_loop("old-download", 111, 7)

    assert downloads.process_args["auto_start"] is False
    assert service._sessions["111"]["download_id"] == "newer-download"
    assert service._sessions["111"]["url"] == "https://example"
    assert service._sessions["111"]["reviews"][child_id]["phase"] == "review"
    review_call = next(payload for method, payload in calls if method == "sendMessage")
    assert review_call["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        f"file:start:{child_id}"
    )


@pytest.mark.asyncio
async def test_history_process_for_download_only_source_offers_workflow_choice(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(work_dir=tmp_path, telegram_owner_id="owner")

    class Downloads:
        async def get(self, *_args):
            return SimpleNamespace(workflow=DownloadWorkflow.DOWNLOAD)

    service = TelegramService(settings, object(), Downloads(), object())
    calls: list[dict] = []

    async def call(_method: str, payload: dict | None = None):
        calls.append(payload or {})
        return {}

    service.client.call = call  # type: ignore[method-assign]
    await service._handle_history_action(
        111,
        {},
        "history:process:6f5b5443-66bf-46e8-b33a-c8053220f2c9",
    )
    callbacks = {
        button["callback_data"]
        for row in calls[-1]["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert callbacks == {
        "history:process:6f5b5443-66bf-46e8-b33a-c8053220f2c9:convert",
        "history:process:6f5b5443-66bf-46e8-b33a-c8053220f2c9:compress",
        "history:process:6f5b5443-66bf-46e8-b33a-c8053220f2c9:convert_compress",
    }


@pytest.mark.asyncio
async def test_download_request_event_never_triggers_automatic_document_delivery(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        telegram_bot_token="token",
        telegram_api_base_url="https://api.telegram.org",
        telegram_chat_id=111,
        telegram_user_id=222,
        telegram_owner_id="owner",
    )
    job_id = "5f5b5443-66bf-46e8-b33a-c8053220f2c9"

    class Jobs:
        async def get(self, *_args):
            return SimpleNamespace(origin="telegram")

    service = TelegramService(settings, Jobs(), object(), object())
    service._sessions["111"] = {"job_id": "6f5b5443-66bf-46e8-b33a-c8053220f2c9"}
    item = {"event": "download", "job_id": job_id, "owner": "owner"}
    assert await service._delivery_kind(item) is None
    assert await service._delivery_kind({**item, "event": "ready"}) == "job"


@pytest.mark.asyncio
async def test_download_ready_delivery_uses_persisted_telegram_origin() -> None:
    service = TelegramService(SimpleNamespace(), object(), object(), object())

    class Downloads:
        async def get(self, *_args):
            return SimpleNamespace(origin="telegram", workflow="download")

    service.downloads = Downloads()
    service._sessions["111"] = {"download_id": "a-different-latest-download"}
    assert await service._delivery_kind(
        {"event": "download_ready", "job_id": "earlier-job", "owner": "owner"}
    ) == "download"


@pytest.mark.asyncio
async def test_stale_file_review_callback_cannot_start_the_latest_job(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        telegram_chat_id=111,
        telegram_user_id=222,
        telegram_owner_id="owner",
    )
    jobs = SimpleNamespace(start_prepared=None)
    service = TelegramService(settings, jobs, object(), object())
    old_job = "5f5b5443-66bf-46e8-b33a-c8053220f2c9"
    latest_job = "6f5b5443-66bf-46e8-b33a-c8053220f2c9"
    service._sessions["111"] = {"job_id": latest_job, "phase": "review"}
    calls: list[tuple[str, dict]] = []

    async def call(method, payload=None):
        calls.append((method, payload or {}))
        return {}

    service.client.call = call  # type: ignore[method-assign]
    await service._handle_callback(
        {
            "id": "callback",
            "from": {"id": 222},
            "message": {"chat": {"id": 111, "type": "private"}},
            "data": f"file:start:{old_job}",
        }
    )
    assert [method for method, _ in calls] == ["answerCallbackQuery", "sendMessage"]
    assert "stale" in calls[-1][1]["text"].lower()


@pytest.mark.asyncio
async def test_plain_file_upload_can_configure_all_workflows_before_explicit_start(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        max_upload_bytes=10_000_000,
        max_output_bytes=50_000_000,
        upload_timeout_seconds=30,
        telegram_chat_id=111,
        telegram_user_id=222,
        telegram_owner_id="owner",
    )

    class Jobs:
        async def prepare_from_path(self, _path, _owner, **kwargs):
            self.prepared = kwargs
            return SimpleNamespace(id="5f5b5443-66bf-46e8-b33a-c8053220f2c9")

        async def estimate(self, *_args):
            return SimpleNamespace(
                eta_min_seconds=1,
                eta_max_seconds=2,
                status="ready",
                size_min_bytes=10,
                size_max_bytes=20,
                confidence="medium",
                source=SimpleNamespace(bytes=100),
            )

        async def start_prepared(self, job_id, owner, conversion, compression):
            self.started = (job_id, owner, conversion, compression)
            return SimpleNamespace(id=job_id)

    jobs = Jobs()
    service = TelegramService(settings, jobs, object(), object())
    service._state_dir.mkdir(parents=True)

    async def download_file(_file_id, destination, _maxbytes):
        destination.write_bytes(b"media")
        return 5

    service.client.download_file = download_file  # type: ignore[method-assign]
    calls: list[tuple[str, dict]] = []

    async def call(method, payload=None):
        calls.append((method, payload or {}))
        return {"message_id": 7}

    service.client.call = call  # type: ignore[method-assign]
    await service._handle_document(111, {"file_id": "file", "file_name": "clip.mp4"})
    assert jobs.prepared["conversion_options"].target is Target.VIDEO_MP4
    job_id = "5f5b5443-66bf-46e8-b33a-c8053220f2c9"
    assert calls[-1][1]["reply_markup"]["inline_keyboard"][1][0]["callback_data"] == (
        f"file:settings:{job_id}"
    )

    callback_message = {"chat": {"id": 111, "type": "private"}}
    await service._handle_callback(
        {
            "from": {"id": 222},
            "message": callback_message,
            "data": f"file:settings:{job_id}",
        }
    )
    keyboard = calls[-1][1]["reply_markup"]["inline_keyboard"]
    callbacks = {button["callback_data"] for row in keyboard for button in row}
    assert "adv:target:animation-gif" in callbacks
    assert "adv:workflow:convert_compress" in callbacks

    for data in (
        "adv:workflow:convert_compress",
        "adv:target:animation-gif",
        "adv:resolution:720p",
        "adv:audio:drop",
        "adv:quality_percent",
    ):
        await service._handle_advanced_callback(111, service._sessions["111"], data)
    await service._handle_advanced_value(111, service._sessions["111"], "75")
    await service._handle_advanced_callback(111, service._sessions["111"], "adv:target_mb")
    await service._handle_advanced_value(111, service._sessions["111"], "12")
    await service._handle_advanced_callback(111, service._sessions["111"], "adv:use")
    assert calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == (
        f"file:start:{job_id}"
    )
    review_text = calls[-1][1]["text"]
    assert "Estimated reduction: 80.0-90.0%" in review_text

    await service._handle_callback(
        {
            "from": {"id": 222},
            "message": callback_message,
            "data": f"file:start:{job_id}",
        }
    )
    started = jobs.started
    assert started[2].target is Target.ANIMATION_GIF
    assert started[2].resolution.value == "source"
    assert started[2].quality_percent is None
    assert started[3].target_bytes == 12_000_000
    assert started[3].quality_percent == 75
    assert started[3].resolution.value == "720p"
    assert started[3].audio.value == "drop"


@pytest.mark.asyncio
async def test_result_delivery_queues_download_alert_before_sending_document(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        work_dir=tmp_path,
        telegram_bot_token="token",
        telegram_api_base_url="https://api.telegram.org",
        telegram_chat_id=111,
        telegram_user_id=222,
        telegram_owner_id="owner",
    )
    source = tmp_path / "result.mp4"
    source.write_bytes(b"result")
    view = SimpleNamespace(
        output=SimpleNamespace(download_url="https://media.example/content"),
        expires_at=datetime.now(UTC),
    )

    class Jobs:
        async def get(self, *_args):
            return view

        async def download(self, *_args):
            return source, "result.mp4", "video/mp4"

    service = TelegramService(settings, Jobs(), object(), object())
    order: list[str] = []

    async def notify(*_args):
        order.append("notify")

    async def send_document(*_args):
        order.append("document")

    service.notify = notify  # type: ignore[method-assign]
    service.client.send_document = send_document  # type: ignore[method-assign]
    assert await service.send_result("5f5b5443-66bf-46e8-b33a-c8053220f2c9", "owner")
    assert order == ["notify", "document"]


def test_url_presets_default_to_review_and_expose_auto_toggle() -> None:
    service = TelegramService(SimpleNamespace(), object(), object(), object())
    keyboard = service._url_keyboard()
    callbacks = {button["callback_data"] for row in keyboard["inline_keyboard"] for button in row}
    assert "url:auto" in callbacks
    assert service._url_keyboard(auto_process=True)["inline_keyboard"][-1][0]["text"] == (
        "Auto-process: on"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['job', 'download'])
async def test_large_result_uses_recovery_link_with_complete_metadata(tmp_path, monkeypatch, kind):
    from media_manager import telegram as telegram_module

    monkeypatch.setattr(telegram_module, '_TELEGRAM_MAX_DOCUMENT_BYTES', 10)
    path = tmp_path / 'result.webm'
    path.write_bytes(b'x' * 20)
    expiry = datetime(2026, 10, 16, tzinfo=UTC)
    view = SimpleNamespace(
        output=SimpleNamespace(download_url='https://media.example/v1/jobs/result/content'),
        expires_at=expiry,
    )

    class Results:
        async def get(self, *_args):
            return view

        async def download(self, *_args):
            return path, path.name, 'video/webm'

    class Client:
        def __init__(self):
            self.messages = []

        async def call(self, method, body):
            assert method == 'sendMessage'
            self.messages.append(body['text'])
            return {'message_id': 1}

        async def send_document(self, *_args):
            raise AssertionError('oversized document must use website recovery')

    settings = SimpleNamespace(
        work_dir=tmp_path, telegram_bot_token='',
        telegram_api_base_url='http://telegram.local:8081',
        telegram_chat_id=111, telegram_user_id=222, telegram_owner_id='owner',
    )
    results = Results()
    service = TelegramService(settings, results, results, object())
    service.client = Client()
    if kind == 'download':
        assert await service._send_download_result('result', 'owner')
    else:
        assert await service.send_result('result', 'owner')
    text = service.client.messages[0]
    assert path.name in text and '20 bytes' in text
    assert expiry.isoformat() in text and view.output.download_url in text
