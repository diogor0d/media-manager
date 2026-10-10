from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from media_manager.integration_models import DownloadRequest, DownloadType, DownloadWorkflow
from media_manager.models import (
    CompressionOptions,
    ConversionOptions,
    Target,
)

logger = logging.getLogger(__name__)
_TELEGRAM_MAX_DOCUMENT_BYTES = 2_000_000_000
_TELEGRAM_POLL_SECONDS = 25
_SEND_PROGRESS_INTERVAL = 5.0


class TelegramClient:
    """Small Bot API client whose requests always use the configured API origin."""

    def __init__(self, settings: Any, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        token = self.settings.telegram_bot_token
        base = self.settings.telegram_api_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{base}/bot{token}/",
            timeout=httpx.Timeout(35, connect=5),
            follow_redirects=False,
            transport=self._transport,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        response = await self._request("POST", method, json=payload or {})
        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError("Telegram Bot API request failed")
        return body.get("result")

    async def download_file(self, file_id: str, destination: Path, maxbytes: int) -> int:
        result = await self.call("getFile", {"file_id": file_id})
        if not isinstance(result, dict):
            raise ValueError("Telegram returned an invalid file record")
        file_path = result.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise ValueError("Telegram returned an unsafe file path")
        if Path(file_path).is_absolute():
            return await self._copy_local_file(Path(file_path), destination, maxbytes)
        if file_path.startswith("\\"):
            raise ValueError("Telegram returned an unsafe file path")
        parts = Path(file_path).parts
        if any(part in {".", ".."} for part in parts) or "\\" in file_path:
            raise ValueError("Telegram returned an unsafe file path")
        response = await self._request(
            "GET",
            f"../file/bot{self.settings.telegram_bot_token}/{quote(file_path, safe='/')}",
            stream=True,
        )
        total = 0
        temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with temp.open("xb") as stream:
                os.chmod(temp, 0o600)
                async for chunk in response.aiter_bytes(128 * 1024):
                    total += len(chunk)
                    if total > maxbytes:
                        raise ValueError("Telegram file exceeds the configured size limit")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, destination)
            return total
        finally:
            await response.aclose()
            temp.unlink(missing_ok=True)

    async def _copy_local_file(self, source: Path, destination: Path, maxbytes: int) -> int:
        configured_root = getattr(self.settings, "telegram_file_root", None)
        if configured_root is None:
            raise ValueError("Telegram local file access is not configured")
        root = Path(configured_root)
        try:
            relative = source.relative_to(root)
        except ValueError as exc:
            raise ValueError("Telegram local file path is outside the shared mount") from exc
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise ValueError("Telegram returned an unsafe local file path")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")

        def copy() -> int:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            current_fd = root_fd
            try:
                for part in relative.parts[:-1]:
                    child_fd = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current_fd,
                    )
                    if current_fd != root_fd:
                        os.close(current_fd)
                    current_fd = child_fd
                source_fd = os.open(
                    relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd
                )
                info = os.fstat(source_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > maxbytes:
                    os.close(source_fd)
                    raise ValueError("Telegram file is not regular or exceeds the size limit")
                total = 0
                with os.fdopen(source_fd, "rb") as source_stream, temp.open("xb") as target:
                    os.chmod(temp, 0o600)
                    while chunk := source_stream.read(128 * 1024):
                        total += len(chunk)
                        if total > maxbytes:
                            raise ValueError("Telegram file exceeds the configured size limit")
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temp, destination)
                return total
            finally:
                if current_fd != root_fd:
                    os.close(current_fd)
                os.close(root_fd)

        try:
            return await asyncio.to_thread(copy)
        finally:
            temp.unlink(missing_ok=True)

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        filename: str,
        caption: str = "",
    ) -> Any:
        size = path.stat().st_size
        if size > _TELEGRAM_MAX_DOCUMENT_BYTES:
            raise ValueError("Telegram document is too large")
        with path.open("rb") as stream:
            response = await self._request(
                "POST",
                "sendDocument",
                data={"chat_id": str(chat_id), "caption": caption[:1024]},
                files={"document": (filename, stream, "application/octet-stream")},
                timeout=httpx.Timeout(300, connect=5),
            )
        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError("Telegram document upload failed")
        return body.get("result")

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("Telegram client is not started")
        stream = kwargs.pop("stream", False)
        if stream:
            request = self._client.build_request(method, path, **kwargs)
            response = await self._client.send(request, stream=True)
        else:
            response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response


class TelegramService:
    """Private-chat controller. Its durable state contains only offset/session IDs."""

    def __init__(self, settings: Any, jobs: Any, downloads: Any, processor: Any) -> None:
        self.settings = settings
        self.jobs = jobs
        self.downloads = downloads
        self.processor = processor
        self.client = TelegramClient(settings)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._offset = 0
        self._sessions: dict[str, dict[str, Any]] = {}
        self._outbox: list[dict[str, Any]] = []
        self._delivered_events: list[str] = []
        self._state_lock = asyncio.Lock()
        self._last_progress: dict[str, float] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._started = False
        self._processing_update_key: str | None = None
        self._outbox_task: asyncio.Task[None] | None = None
        self._outbox_wakeup = asyncio.Event()

    @property
    def _state_dir(self) -> Path:
        return Path(self.settings.work_dir) / ".integrations" / "telegram"

    @property
    def _state_file(self) -> Path:
        return self._state_dir / "state.json"

    async def start(self) -> None:
        if not self.settings.telegram_bot_token:
            return
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._state_dir.is_symlink() or self._state_dir.parent.is_symlink():
            raise RuntimeError("Telegram storage path is unsafe")
        await asyncio.to_thread(self._load_state)
        await self.client.start()
        self._started = True
        self._stopping = False
        self._task = asyncio.create_task(self._poll(), name="telegram-updates")
        self._outbox_task = asyncio.create_task(self._outbox_worker(), name="telegram-outbox")
        for chat_key, session in self._sessions.items():
            if (
                session.get("phase") == "watching"
                and isinstance(session.get("job_id"), str)
                and isinstance(session.get("message_id"), int)
            ):
                self._watch_job(session["job_id"], int(chat_key), session["message_id"])
            elif (
                session.get("phase") == "watching"
                and isinstance(session.get("download_id"), str)
                and isinstance(session.get("download_message_id"), int)
            ):
                self._watch_download(
                    session["download_id"], int(chat_key), session["download_message_id"]
                )
        if self._outbox:
            self._outbox_wakeup.set()

    async def stop(self) -> None:
        if not self._started:
            return
        self._stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._outbox_task:
            self._outbox_task.cancel()
            await asyncio.gather(self._outbox_task, return_exceptions=True)
            self._outbox_task = None
        watchers = list(self._watchers.values())
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
        self._watchers.clear()
        await self._save_state()
        await self.client.close()
        self._started = False

    async def notify(
        self,
        event: str,
        job_id: str,
        owner: str,
        filename: str | None,
        size: int | None,
        expires_at: datetime | None,
    ) -> None:
        if not self.settings.telegram_bot_token or owner != self._telegram_owner():
            return
        async with self._state_lock:
            item = {
                "event": event,
                "event_id": str(uuid.uuid4()) if event == "download" else None,
                "job_id": job_id,
                "owner": owner,
                "filename": filename,
                "size": size,
                "expires_at": expires_at.isoformat() if expires_at else None,
            }
            event_key = self._event_key(item)
            if event != "download" and (
                event_key in self._delivered_events
                or any(self._event_key(existing) == event_key for existing in self._outbox)
            ):
                return
            self._outbox.append(item)
            self._outbox = self._outbox[-100:]
            await self._persist_state_locked()
        if self._started:
            self._outbox_wakeup.set()

    async def _outbox_worker(self) -> None:
        while not self._stopping:
            try:
                await self._outbox_wakeup.wait()
                self._outbox_wakeup.clear()
                await self._flush_outbox()
                if self._outbox and not self._stopping:
                    await asyncio.sleep(2)
                    self._outbox_wakeup.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram outbox delivery failed (%s)", type(exc).__name__)
                await asyncio.sleep(2)
                if self._outbox:
                    self._outbox_wakeup.set()

    async def _poll(self) -> None:
        while not self._stopping:
            try:
                updates = await self.client.call(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": _TELEGRAM_POLL_SECONDS,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in updates if isinstance(updates, list) else []:
                    if not isinstance(update, dict):
                        continue
                    await self._handle_update(update)
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = max(self._offset, update_id + 1)
                        await self._save_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram polling failed (%s)", type(exc).__name__)
                await asyncio.sleep(3)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        previous_key = self._processing_update_key
        update_id = update.get("update_id")
        self._processing_update_key = str(update_id) if isinstance(update_id, int) else None
        try:
            if isinstance(update.get("message"), dict):
                await self._handle_message(update["message"])
            elif isinstance(update.get("callback_query"), dict):
                await self._handle_callback(update["callback_query"])
        finally:
            self._processing_update_key = previous_key

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat_id, user_id = chat.get("id"), sender.get("id")
        if chat.get("type") != "private" or not self._authorized(chat_id, user_id):
            return
        text = message.get("text")
        if isinstance(text, str) and text.startswith("/start"):
            await self.client.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Send a URL or choose Convert / Compress. Upload a file to process it.",
                    "reply_markup": self._main_keyboard(),
                },
            )
            return
        if isinstance(text, str) and text.startswith(("/status", "/history")):
            await self._send_history(chat_id)
            return
        session = self._sessions.setdefault(str(chat_id), {})
        if isinstance(text, str) and session.get("phase") in {
            "input_quality",
            "input_target_mb",
        }:
            await self._handle_advanced_value(chat_id, session, text.strip())
            return
        if isinstance(text, str) and _looks_like_url(text.strip()):
            session.update({"url": text.strip(), "phase": "download_options"})
            await self._save_state()
            await self.client.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Choose the source preset:",
                    "reply_markup": self._url_keyboard(
                        has_saved_conversion=bool(session.get("conversion_options")),
                        auto_process=bool(session.get("auto_process")),
                    ),
                },
            )
            return
        media = self._telegram_media(message)
        if media is not None:
            await self._handle_document(chat_id, media)
            return

    async def _send_history(self, chat_id: int) -> None:
        owner = self._telegram_owner()
        jobs, _ = await self.jobs.history(owner, 10, 0)
        downloads, _ = await self.downloads.history(owner, 10, 0)
        entries: list[tuple[datetime, dict[str, Any], str]] = []
        for item in jobs:
            view = item.get("job", {})
            stamp = item.get("completed_at") or view.get("created_at")
            if isinstance(stamp, str):
                with suppress(ValueError):
                    entries.append((datetime.fromisoformat(stamp), item, "job"))
        for item in downloads:
            view = item.get("job", {})
            stamp = view.get("completed_at") or view.get("created_at")
            if isinstance(stamp, str):
                with suppress(ValueError):
                    entries.append((datetime.fromisoformat(stamp), item, "download"))
        entries.sort(key=lambda entry: entry[0], reverse=True)
        rows = []
        labels = []
        for _stamp, item, kind in entries[:8]:
            view = item.get("job", {})
            entry_id = item.get("id")
            if not isinstance(entry_id, str):
                continue
            filename = view.get("source_filename") or item.get("source_filename") or entry_id[:8]
            state = view.get("state", "unknown")
            labels.append(f"{filename}: {state}")
            buttons = []
            if state in {"ready", "processing", "awaiting_review"} and view.get("output"):
                buttons.append(
                    {
                        "text": "Retrieve",
                        "callback_data": f"history:retrieve:{kind}:{entry_id}",
                    }
                )
            if (
                kind == "download"
                and state in {"ready", "awaiting_review"}
            ):
                buttons.append({"text": "Process", "callback_data": f"history:process:{entry_id}"})
            buttons.append({"text": "Delete", "callback_data": f"history:delete:{kind}:{entry_id}"})
            rows.append(buttons)
        if not rows:
            await self.client.call(
                "sendMessage", {"chat_id": chat_id, "text": "No recent media history."}
            )
            return
        await self.client.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Recent media:\n" + "\n".join(labels),
                "reply_markup": {"inline_keyboard": rows},
            },
        )

    async def _handle_history_action(
        self, chat_id: int, message: dict[str, Any], data: str
    ) -> None:
        parts = data.split(":")
        owner = self._telegram_owner()
        try:
            if len(parts) == 3 and parts[1] == "process":
                download_id = parts[2]
                view = await self.downloads.get(download_id, owner)
                workflow = getattr(view.workflow, "value", view.workflow)
                if workflow == DownloadWorkflow.DOWNLOAD.value:
                    await self.client.call(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": "Choose what to do with this source:",
                            "reply_markup": {
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "Convert",
                                            "callback_data": (
                                                f"history:process:{download_id}:convert"
                                            ),
                                        },
                                        {
                                            "text": "Compress",
                                            "callback_data": (
                                                f"history:process:{download_id}:compress"
                                            ),
                                        },
                                        {
                                            "text": "Convert + compress",
                                            "callback_data": (
                                                f"history:process:{download_id}:convert_compress"
                                            ),
                                        },
                                    ]
                                ]
                            },
                        },
                    )
                    return
                await self._prepare_history_review(
                    chat_id, download_id, owner, None, None
                )
                return
            elif len(parts) == 4 and parts[1] == "process":
                download_id, workflow = parts[2], parts[3]
                if workflow not in {"convert", "compress", "convert_compress"}:
                    raise ValueError("Unsupported history workflow")
                conversion = (
                    ConversionOptions(target=Target.VIDEO_MP4)
                    if workflow in {"convert", "convert_compress"}
                    else None
                )
                compression = (
                    CompressionOptions()
                    if workflow in {"compress", "convert_compress"}
                    else None
                )
                await self._prepare_history_review(
                    chat_id, download_id, owner, conversion, compression
                )
                return
            elif len(parts) == 4 and parts[1] == "retrieve":
                kind, item_id = parts[2], parts[3]
                if kind == "download":
                    sent = await self._send_download_result(item_id, owner)
                elif kind == "job":
                    sent = await self.send_result(item_id, owner)
                else:
                    sent = False
                text = "Result sent." if sent else "That result is no longer available."
            elif len(parts) == 4 and parts[1] == "delete":
                kind, item_id = parts[2], parts[3]
                if kind == "download":
                    view = await self.downloads.get(item_id, owner)
                    if view.child_id:
                        with suppress(Exception):
                            await self.jobs.delete(view.child_id, owner)
                    await self.downloads.delete(item_id, owner)
                elif kind == "job":
                    await self.jobs.delete(item_id, owner)
                else:
                    raise ValueError("Unsupported history item")
                text = "History item deleted."
            else:
                return
        except Exception as exc:
            logger.info("Telegram history action failed (%s)", type(exc).__name__)
            text = "That item could not be accessed. Refresh /history and try again."
        await self.client.call("sendMessage", {"chat_id": chat_id, "text": text})

    async def _prepare_history_review(
        self,
        chat_id: int,
        download_id: str,
        owner: str,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
    ) -> None:
        child = await self.downloads.process(
            download_id,
            owner,
            conversion_options=conversion,
            compression_options=compression,
            auto_start=False,
        )
        child = await self.jobs.get(child.id, owner)
        conversion, compression = self._review_options_from_child(child)
        workflow = (
            "convert_compress"
            if conversion is not None and compression is not None
            else "compress"
            if compression is not None
            else "convert"
        )
        session = self._sessions.setdefault(str(chat_id), {})
        review = {
            "job_id": child.id,
            "workflow": workflow,
            "phase": "review",
            "conversion_options": (
                conversion.model_dump(mode="json") if conversion is not None else None
            ),
            "compression_options": (
                compression.model_dump(mode="json") if compression is not None else None
            ),
        }
        session.setdefault("reviews", {})[child.id] = review
        await self._save_state()
        await self._send_estimate_review(chat_id, child.id, conversion, compression)

    @staticmethod
    def _review_options_from_child(
        child: Any,
    ) -> tuple[ConversionOptions | None, CompressionOptions | None]:
        compression = getattr(child, "options", None)
        if compression is not None:
            return None, CompressionOptions.model_validate(compression)
        target = getattr(child, "target", None)
        if target is None:
            return None, None
        conversion = ConversionOptions.model_validate(
            {
                "target": target,
                "quality": getattr(child, "quality", "balanced"),
                "quality_percent": getattr(child, "quality_percent", None),
                "resolution": getattr(child, "resolution", "source"),
                "audio": getattr(child, "audio", "keep"),
            }
        )
        return conversion, None

    async def _handle_callback(self, query: dict[str, Any]) -> None:
        message = query.get("message") if isinstance(query.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = query.get("from") if isinstance(query.get("from"), dict) else {}
        chat_id, user_id = chat.get("id"), sender.get("id")
        if chat.get("type") != "private" or not self._authorized(chat_id, user_id):
            return
        data = query.get("data")
        await self.client.call("answerCallbackQuery", {"callback_query_id": query.get("id")})
        session = self._sessions.setdefault(str(chat_id), {})
        if data == "history:list":
            await self._send_history(chat_id)
        elif isinstance(data, str) and data.startswith("history:"):
            await self._handle_history_action(chat_id, message, data)
        elif data in {"menu:convert", "menu:compress"}:
            session["workflow"] = "convert" if data.endswith("convert") else "compress"
            session["phase"] = "preset"
            await self._save_state()
            await self.client.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Choose an output format:"
                    if session["workflow"] == "convert"
                    else "Send a file to compress, or configure compression options.",
                    "reply_markup": self._preset_keyboard()
                    if session["workflow"] == "convert"
                    else self._advanced_keyboard("compress", session),
                },
            )
        elif isinstance(data, str) and data.startswith("file:"):
            parts = data.split(":")
            action = parts[1] if len(parts) == 3 else None
            callback_job_id = parts[2] if len(parts) == 3 else None
            reviews = session.get("reviews", {})
            review_context = (
                reviews.get(callback_job_id) if isinstance(reviews, dict) else None
            )
            callback_session = (
                review_context
                if isinstance(review_context, dict)
                else session
                if session.get("job_id") == callback_job_id
                else None
            )
            job_id = callback_session.get("job_id") if callback_session else None
            if action not in {"start", "refresh", "discard", "settings"} or (
                not isinstance(job_id, str) or callback_job_id != job_id
            ):
                await self.client.call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": "That review is stale. Use the latest file review.",
                    },
                )
                return
            session = callback_session
            if action == "settings":
                workflow = session.get("workflow", "convert")
                self._sessions.setdefault(str(chat_id), {}).update(
                    {key: value for key, value in session.items() if key != "reviews"}
                )
                session = self._sessions[str(chat_id)]
                session["phase"] = "advanced"
                await self._save_state()
                await self.client.call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": self._advanced_summary(workflow, session),
                        "reply_markup": self._advanced_keyboard(workflow, session),
                    },
                )
                return
            conversion = (
                ConversionOptions.model_validate(session["conversion_options"])
                if session.get("conversion_options")
                else None
            )
            compression = (
                CompressionOptions.model_validate(session["compression_options"])
                if session.get("compression_options")
                else None
            )
            if action == "refresh":
                await self._send_estimate_review(chat_id, job_id, conversion, compression)
            elif action == "discard":
                await self.jobs.delete(job_id, self._telegram_owner())
                reviews = self._sessions.setdefault(str(chat_id), {}).get("reviews", {})
                if isinstance(reviews, dict):
                    reviews.pop(job_id, None)
                session.pop("job_id", None)
                session["phase"] = "awaiting_file"
                await self._save_state()
                await self.client.call(
                    "sendMessage", {"chat_id": chat_id, "text": "Prepared job discarded."}
                )
            else:
                child = await self.jobs.start_prepared(
                    job_id, self._telegram_owner(), conversion, compression
                )
                session["phase"] = "watching"
                await self._save_state()
                message_id = message.get("message_id")
                if isinstance(message_id, int):
                    self._watch_job(child.id, chat_id, message_id)
                await self.client.call(
                    "sendMessage", {"chat_id": chat_id, "text": f"Started: {child.id}"}
                )
        elif data == "advanced":
            workflow = session.get("workflow", "download" if session.get("url") else "convert")
            session["workflow"] = workflow
            session["phase"] = "advanced"
            await self._save_state()
            await self.client.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": self._advanced_summary(workflow, session),
                    "reply_markup": self._advanced_keyboard(workflow, session),
                },
            )
        elif isinstance(data, str) and data.startswith("adv:"):
            await self._handle_advanced_callback(chat_id, session, data)
        elif data == "url:auto" and session.get("url"):
            session["auto_process"] = not bool(session.get("auto_process"))
            await self._save_state()
            await self.client.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Automatic processing is "
                    + ("on." if session["auto_process"] else "off; review is on."),
                    "reply_markup": self._url_keyboard(
                        has_saved_conversion=bool(session.get("conversion_options")),
                        auto_process=bool(session["auto_process"]),
                    ),
                },
            )
        elif isinstance(data, str) and data.startswith("url:") and session.get("url"):
            preset = data.split(":", 1)[1]
            if preset == "advanced":
                session["workflow"] = "download"
                session["phase"] = "advanced"
                await self._save_state()
                await self.client.call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": self._advanced_summary("download", session),
                        "reply_markup": self._advanced_keyboard("download", session),
                    },
                )
                return
            if preset == "convert_compress":
                session["workflow"] = preset
                session.setdefault(
                    "conversion_options",
                    ConversionOptions(target="video-mp4").model_dump(mode="json"),
                )
                session.setdefault(
                    "compression_options", CompressionOptions().model_dump(mode="json")
                )
                session["phase"] = "advanced"
                await self._save_state()
                await self.client.call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": self._advanced_summary(preset, session),
                        "reply_markup": self._advanced_keyboard(preset, session),
                    },
                )
                return
            download_type = DownloadType.AUDIO if preset == "audio" else DownloadType.VIDEO
            if preset == "convert-saved" and session.get("conversion_options"):
                saved_options = ConversionOptions.model_validate(session["conversion_options"])
                audio_target = saved_options.target in {
                    Target.AUDIO_M4A,
                    Target.AUDIO_MP3,
                    Target.AUDIO_OPUS,
                }
                request = DownloadRequest(
                    url=session["url"],
                    workflow="convert",
                    auto_process=bool(session.get("auto_process")),
                    download_type=DownloadType.AUDIO if audio_target else DownloadType.VIDEO,
                    format="m4a" if audio_target else "ios",
                    codec="auto" if audio_target else "h264",
                    conversion_options=saved_options,
                )
            elif preset.startswith("convert-"):
                target = Target(preset.removeprefix("convert-"))
                request = DownloadRequest(
                    url=session["url"],
                    workflow="convert",
                    auto_process=bool(session.get("auto_process")),
                    download_type=(
                        DownloadType.AUDIO
                        if target in {Target.AUDIO_M4A, Target.AUDIO_MP3, Target.AUDIO_OPUS}
                        else DownloadType.VIDEO
                    ),
                    format=(
                        "m4a"
                        if target in {Target.AUDIO_M4A, Target.AUDIO_MP3, Target.AUDIO_OPUS}
                        else "ios"
                    ),
                    codec=(
                        "auto"
                        if target in {Target.AUDIO_M4A, Target.AUDIO_MP3, Target.AUDIO_OPUS}
                        else "h264"
                    ),
                    conversion_options=ConversionOptions(target=target),
                )
            elif preset == "compress":
                request = DownloadRequest(
                    url=session["url"],
                    workflow="compress",
                    auto_process=bool(session.get("auto_process")),
                    compression_options=CompressionOptions(),
                )
            else:
                quality = "1080p" if preset == "1080p" else "best"
                request = DownloadRequest(
                    url=session["url"],
                    quality=quality,
                    download_type=download_type,
                    format="m4a" if download_type is DownloadType.AUDIO else "ios",
                    codec="auto" if download_type is DownloadType.AUDIO else "h264",
                )
            view = await self.downloads.create(
                request,
                self._telegram_owner(),
                idempotency_key=self._download_idempotency_key(chat_id),
                origin="telegram",
            )
            session.update(
                {
                    "download_id": view.id,
                    "workflow": request.workflow.value,
                    "phase": "watching",
                }
            )
            await self._save_state()
            sent = await self.client.call(
                "sendMessage", {"chat_id": chat_id, "text": f"Download queued: {view.id}"}
            )
            message_id = sent.get("message_id") if isinstance(sent, dict) else None
            if isinstance(message_id, int):
                session["download_message_id"] = message_id
                await self._save_state()
                self._watch_download(view.id, chat_id, message_id)
        elif isinstance(data, str) and data.startswith("preset:"):
            preset = data.split(":", 1)[1]
            if preset == "compress":
                session["workflow"] = "compress"
                session.pop("conversion_options", None)
            else:
                session["workflow"] = "convert"
                session["conversion_options"] = ConversionOptions(target=Target(preset)).model_dump(
                    mode="json"
                )
            session["phase"] = "awaiting_file"
            await self._save_state()
            await self.client.call(
                "sendMessage",
                {"chat_id": chat_id, "text": "Send the file to process with this preset."},
            )

    async def _handle_advanced_callback(
        self, chat_id: int, session: dict[str, Any], data: str
    ) -> None:
        _, action, *value_parts = data.split(":")
        value = ":".join(value_parts)
        workflow = session.get("workflow", "download")
        if action == "workflow" and value in {
            "download",
            "convert",
            "compress",
            "convert_compress",
        }:
            session["workflow"] = value
            if value in {"convert", "convert_compress"}:
                session.setdefault(
                    "conversion_options",
                    ConversionOptions(target="video-mp4").model_dump(mode="json"),
                )
            elif value == "compress":
                session.pop("conversion_options", None)
            if value == "convert":
                session.pop("compression_options", None)
            if value in {"compress", "convert_compress"}:
                session.setdefault(
                    "compression_options", CompressionOptions().model_dump(mode="json")
                )
            elif value in {"download", "convert"}:
                session.pop("compression_options", None)
            if value == "download":
                session.pop("conversion_options", None)
            await self._save_state()
            await self.client.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": self._advanced_summary(value, session),
                    "reply_markup": self._advanced_keyboard(value, session),
                },
            )
            return
        if action == "auto":
            session["auto_process"] = not bool(session.get("auto_process"))
            await self._save_state()
            await self.client.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": self._advanced_summary(workflow, session),
                    "reply_markup": self._advanced_keyboard(workflow, session),
                },
            )
            return
        if action == "use":
            if session.get("url"):
                request = self._request_from_session(session)
                view = await self.downloads.create(
                    request,
                    self._telegram_owner(),
                    idempotency_key=self._download_idempotency_key(chat_id),
                    origin="telegram",
                )
                session.update(
                    {
                        "download_id": view.id,
                        "workflow": request.workflow.value,
                        "phase": "watching",
                    }
                )
                await self._save_state()
                sent = await self.client.call(
                    "sendMessage", {"chat_id": chat_id, "text": f"Download queued: {view.id}"}
                )
                message_id = sent.get("message_id") if isinstance(sent, dict) else None
                if isinstance(message_id, int):
                    session["download_message_id"] = message_id
                    await self._save_state()
                    self._watch_download(view.id, chat_id, message_id)
            elif isinstance(session.get("job_id"), str):
                session["phase"] = "review"
                await self._save_state()
                conversion = (
                    ConversionOptions.model_validate(session["conversion_options"])
                    if session.get("conversion_options")
                    else None
                )
                compression = (
                    CompressionOptions.model_validate(session["compression_options"])
                    if session.get("compression_options")
                    else None
                )
                await self._send_estimate_review(
                    chat_id, session["job_id"], conversion, compression
                )
            else:
                session["phase"] = "awaiting_file"
                await self._save_state()
                await self.client.call(
                    "sendMessage", {"chat_id": chat_id, "text": "Settings saved. Send a file."}
                )
            return
        if action in {"quality_percent", "target_mb"}:
            session["phase"] = "input_quality" if action == "quality_percent" else "input_target_mb"
            await self._save_state()
            text = (
                "Enter a quality percentage from 0 to 100."
                if action == "quality_percent"
                else "Enter the maximum size in whole megabytes."
            )
            await self.client.call("sendMessage", {"chat_id": chat_id, "text": text})
            return
        if workflow == "download":
            fields = {
                "quality": "source_quality",
                "type": "source_type",
                "format": "source_format",
                "codec": "source_codec",
            }
            if action in fields:
                session[fields[action]] = value
                if action == "type":
                    self._normalize_source_choices(session)
        elif workflow in {"compress", "convert_compress"}:
            options = CompressionOptions.model_validate(
                session.get("compression_options", {})
            ).model_dump(mode="json")
            if action == "target" and workflow == "compress":
                options["target"] = value
            elif action == "resolution":
                options["resolution"] = value
            elif action == "audio":
                options["audio"] = value
            elif action == "quality":
                options["quality_percent"] = {"economy": 25, "balanced": 50, "high": 80}[value]
            session["compression_options"] = CompressionOptions.model_validate(options).model_dump(
                mode="json"
            )
        if workflow in {"convert", "convert_compress"}:
            options = ConversionOptions.model_validate(
                session.get("conversion_options", {"target": "video-mp4"})
            ).model_dump(mode="json")
            if action == "target":
                options["target"] = value
            elif workflow == "convert" and action == "resolution":
                options["resolution"] = value
            elif workflow == "convert" and action == "audio":
                options["audio"] = value
            elif workflow == "convert" and action == "quality":
                options["quality"] = value
            session["conversion_options"] = ConversionOptions.model_validate(options).model_dump(
                mode="json"
            )
        session["phase"] = "advanced"
        await self._save_state()
        await self.client.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": self._advanced_summary(workflow, session),
                "reply_markup": self._advanced_keyboard(workflow, session),
            },
        )

    async def _handle_advanced_value(
        self, chat_id: int, session: dict[str, Any], value: str
    ) -> None:
        if not re.fullmatch(r"\d{1,6}", value):
            await self.client.call(
                "sendMessage",
                {"chat_id": chat_id, "text": "Enter a whole number within the requested range."},
            )
            return
        number = int(value)
        workflow = session.get("workflow", "convert")
        if session["phase"] == "input_quality":
            if number > 100:
                await self.client.call(
                    "sendMessage", {"chat_id": chat_id, "text": "Quality must be from 0 to 100."}
                )
                return
            if workflow in {"compress", "convert_compress"}:
                options = CompressionOptions.model_validate(session.get("compression_options", {}))
                session["compression_options"] = options.model_copy(
                    update={"quality_percent": number}
                ).model_dump(mode="json")
            else:
                options = ConversionOptions.model_validate(
                    session.get("conversion_options", {"target": "video-mp4"})
                )
                session["conversion_options"] = options.model_copy(
                    update={"quality_percent": number}
                ).model_dump(mode="json")
        else:
            target_bytes = number * 1_000_000
            if number == 0 or target_bytes > self.settings.max_output_bytes:
                await self.client.call(
                    "sendMessage",
                    {"chat_id": chat_id, "text": "Size must fit the configured output limit."},
                )
                return
            options = CompressionOptions.model_validate(session.get("compression_options", {}))
            session["compression_options"] = options.model_copy(
                update={"target_bytes": target_bytes}
            ).model_dump(mode="json")
        session["phase"] = "advanced"
        await self._save_state()
        await self.client.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": self._advanced_summary(workflow, session),
                "reply_markup": self._advanced_keyboard(workflow, session),
            },
        )

    def _request_from_session(self, session: dict[str, Any]) -> DownloadRequest:
        workflow = session.get("workflow", "download")
        common = {
            "url": session["url"],
            "quality": session.get("source_quality", "best"),
            "format": session.get("source_format", "ios"),
            "download_type": session.get("source_type", "video"),
            "codec": session.get("source_codec", "h264"),
        }
        if workflow == "convert":
            return DownloadRequest(
                **common,
                workflow="convert",
                auto_process=bool(session.get("auto_process")),
                conversion_options=ConversionOptions.model_validate(
                    session.get("conversion_options", {"target": "video-mp4"})
                ),
            )
        if workflow == "compress":
            return DownloadRequest(
                **common,
                workflow="compress",
                auto_process=bool(session.get("auto_process")),
                compression_options=CompressionOptions.model_validate(
                    session.get("compression_options", {})
                ),
            )
        if workflow == "convert_compress":
            return DownloadRequest(
                **common,
                workflow="convert_compress",
                auto_process=bool(session.get("auto_process")),
                conversion_options=ConversionOptions.model_validate(
                    session.get("conversion_options", {"target": "video-mp4"})
                ),
                compression_options=CompressionOptions.model_validate(
                    session.get("compression_options", {})
                ),
            )
        return DownloadRequest(**common)

    def _advanced_summary(self, workflow: str, session: dict[str, Any]) -> str:
        if workflow == "download":
            return (
                f"Source: {session.get('source_quality', 'best')}, "
                f"{session.get('source_type', 'video')}, "
                f"{session.get('source_format', 'ios')}, "
                f"{session.get('source_codec', 'h264')}"
            )
        if workflow == "compress":
            options = CompressionOptions.model_validate(session.get("compression_options", {}))
            return (
                f"Compression: up to {options.target_bytes // 1_000_000} MB, "
                f"{options.resolution.value}, {options.audio.value}, "
                f"quality {options.quality_percent}%"
            )
        if workflow == "convert_compress":
            conversion = ConversionOptions.model_validate(
                session.get("conversion_options", {"target": "video-mp4"})
            )
            compression = CompressionOptions.model_validate(session.get("compression_options", {}))
            return (
                f"Output format: {conversion.target.value}. Then compress to "
                f"{compression.target_bytes // 1_000_000} MB at "
                f"{compression.quality_percent}% quality, {compression.resolution.value}, "
                f"audio {compression.audio.value}"
            )
        options = ConversionOptions.model_validate(
            session.get("conversion_options", {"target": "video-mp4"})
        )
        return (
            f"Convert to {options.target.value}, {options.quality.value}, "
            f"{options.resolution.value}, audio {options.audio.value}"
            + (
                f", quality {options.quality_percent}%"
                if options.quality_percent is not None
                else ""
            )
        )

    def _advanced_keyboard(self, workflow: str, session: dict[str, Any]) -> dict[str, Any]:
        if workflow == "download":
            source_type = session.get("source_type", "video")
            formats = {
                "video": ["ios", "mp4", "webm", "mkv", "auto"],
                "audio": ["audio", "auto", "m4a", "mp3", "opus", "wav", "flac"],
                "thumbnail": ["jpg", "auto"],
            }.get(source_type, ["ios", "mp4", "webm", "mkv", "auto"])
            codecs = {
                "video": ["h264", "h265", "vp9", "av1", "auto"],
                "audio": ["aac", "opus", "auto"],
                "thumbnail": ["auto"],
            }.get(source_type, ["h264", "h265", "vp9", "av1", "auto"])
            rows = []
            rows.extend(
                _button_rows(
                    [
                        {"text": value, "callback_data": f"adv:quality:{value}"}
                        for value in ("best", "1080", "720", "480", "360", "worst")
                    ]
                )
            )
            rows.append(
                [
                    {"text": value.title(), "callback_data": f"adv:type:{value}"}
                    for value in ("video", "audio", "thumbnail")
                ]
            )
            rows.extend(
                _button_rows(
                    [
                        {"text": value.upper(), "callback_data": f"adv:format:{value}"}
                        for value in formats
                    ]
                )
            )
            rows.extend(
                _button_rows(
                    [
                        {"text": value.upper(), "callback_data": f"adv:codec:{value}"}
                        for value in codecs
                    ]
                )
            )
            rows.extend(
                _button_rows(
                    [
                        {"text": label, "callback_data": f"adv:workflow:{value}"}
                        for value, label in (
                            ("download", "Download only"),
                            ("convert", "Convert"),
                            ("compress", "Compress"),
                            ("convert_compress", "Convert + compress"),
                        )
                    ]
                )
            )
            rows.extend(
                [
                    [
                    {
                        "text": f"Auto-process: {'on' if session.get('auto_process') else 'off'}",
                        "callback_data": "adv:auto",
                    }
                    ],
                    [{"text": "Start download", "callback_data": "adv:use"}],
                ]
            )
        else:
            targets = [
                ("video-mp4", "MP4 (H.264)"),
                ("video-webm", "WebM (VP9)"),
                ("animation-gif", "GIF animation"),
                ("image-webp", "WebP"),
                ("image-jpeg", "JPEG"),
                ("image-png", "PNG"),
                ("audio-mp3", "MP3"),
                ("audio-m4a", "M4A"),
                ("audio-opus", "Ogg Opus"),
            ]
            rows = (
                [
                    [
                        {"text": label, "callback_data": f"adv:target:{value}"}
                        for value, label in targets[index : index + 3]
                    ]
                    for index in range(0, len(targets), 3)
                ]
                if workflow in {"convert", "convert_compress"}
                else []
            )
            if workflow in {"convert", "convert_compress"}:
                selected_target = session.get("conversion_options", {}).get("target", "video-mp4")
            else:
                selected_target = session.get("compression_options", {}).get("target")
            is_audio = str(selected_target or "").startswith("audio-")
            is_video = str(selected_target or "").startswith(("video-", "animation-"))
            resolutions = (
                ["source"] if is_audio else ["source", "480p", "720p", "1080p", "1440p", "2160p"]
            )
            if selected_target == "animation-gif":
                resolutions = ["source", "480p", "720p", "1080p"]
            audio_modes = ["keep", "drop"] if is_video else ["keep"]
            rows.extend(
                _button_rows(
                    [
                        {"text": item, "callback_data": f"adv:resolution:{item}"}
                        for item in resolutions
                    ]
                )
            )
            rows.extend(
                _button_rows(
                    [
                        {"text": item.title(), "callback_data": f"adv:audio:{item}"}
                        for item in audio_modes
                    ]
                )
            )
            rows.extend(
                _button_rows(
                    [
                        {"text": item, "callback_data": f"adv:quality:{item}"}
                        for item in ("economy", "balanced", "high")
                    ]
                )
            )
            rows.append([{"text": "Set quality 0-100", "callback_data": "adv:quality_percent"}])
            workflows = (
                (
                    ("convert", "Convert"),
                    ("compress", "Compress"),
                    ("convert_compress", "Convert + compress"),
                )
                if isinstance(session.get("job_id"), str)
                else (
                    ("download", "Download only"),
                    ("convert", "Convert"),
                    ("compress", "Compress"),
                    ("convert_compress", "Convert + compress"),
                )
            )
            rows.extend(
                _button_rows(
                    [
                        {"text": label, "callback_data": f"adv:workflow:{value}"}
                        for value, label in workflows
                    ]
                )
            )
            if workflow in {"compress", "convert_compress"}:
                rows.append([{"text": "Set target size (MB)", "callback_data": "adv:target_mb"}])
            rows.append(
                [
                    {
                        "text": f"Auto-process: {'on' if session.get('auto_process') else 'off'}",
                        "callback_data": "adv:auto",
                    }
                ]
            )
            rows.append([{"text": "Use these settings", "callback_data": "adv:use"}])
        return {"inline_keyboard": rows}

    async def _handle_document(self, chat_id: int, document: dict[str, Any]) -> None:
        maxbytes = min(self.settings.max_upload_bytes, self.settings.max_output_bytes)
        declared_size = document.get("file_size")
        if isinstance(declared_size, int) and declared_size > maxbytes:
            await self.client.call(
                "sendMessage",
                {"chat_id": chat_id, "text": "This file exceeds the configured upload limit."},
            )
            return
        file_id = document.get("file_id")
        if not isinstance(file_id, str):
            return
        source = self._state_dir / f"telegram-{uuid.uuid4()}.upload"
        try:
            await self.client.download_file(file_id, source, maxbytes)
            session = self._sessions.get(str(chat_id), {})
            workflow = session.setdefault("workflow", "convert")
            if workflow in {"convert", "convert_compress"}:
                session.setdefault(
                    "conversion_options",
                    ConversionOptions(target="video-mp4").model_dump(mode="json"),
                )
            if workflow in {"compress", "convert_compress"}:
                session.setdefault(
                    "compression_options", CompressionOptions().model_dump(mode="json")
                )
            conversion = (
                ConversionOptions.model_validate(session["conversion_options"])
                if session.get("conversion_options")
                else None
            )
            compression = (
                CompressionOptions.model_validate(session.get("compression_options", {}))
                if workflow in {"compress", "convert_compress"}
                else None
            )
            parent_id = self._upload_parent_id(chat_id)
            child = (
                await self.jobs.find_child(parent_id, self._telegram_owner())
                if parent_id is not None
                else None
            )
            if child is None:
                child = await self.jobs.prepare_from_path(
                    source,
                    self._telegram_owner(),
                    filename=document.get("file_name") or "telegram-upload",
                    conversion_options=conversion,
                    compression_options=compression,
                    parent_id=parent_id,
                    origin="telegram",
                    auto_start=False,
                )
            session = self._sessions.setdefault(str(chat_id), {})
            session.update({"job_id": child.id, "phase": "watching"})
            await self._save_state()
            session.update({"job_id": child.id, "phase": "review"})
            await self._save_state()
            sent = await self._send_estimate_review(chat_id, child.id, conversion, compression)
            message_id = sent.get("message_id") if isinstance(sent, dict) else None
            if isinstance(message_id, int):
                session.update({"job_id": child.id, "message_id": message_id})
                await self._save_state()
        except Exception as exc:
            logger.warning("Telegram file processing failed (%s)", type(exc).__name__)
            await self.client.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "The file could not be processed. You can retry from the web app.",
                },
            )
        finally:
            source.unlink(missing_ok=True)

    async def _send_estimate_review(
        self,
        chat_id: int,
        job_id: str,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
    ) -> Any:
        estimate = await self.jobs.estimate(job_id, self._telegram_owner(), conversion, compression)
        eta = "unknown"
        if estimate.eta_min_seconds is not None and estimate.eta_max_seconds is not None:
            eta = f"{estimate.eta_min_seconds:g}-{estimate.eta_max_seconds:g} sec"
        source_bytes = estimate.source.bytes
        reduction = "unknown"
        if source_bytes > 0:
            reduction_min = (source_bytes - estimate.size_max_bytes) * 100 / source_bytes
            reduction_max = (source_bytes - estimate.size_min_bytes) * 100 / source_bytes
            reduction = f"{reduction_min:.1f}-{reduction_max:.1f}%"
        status = (
            "Estimate is being refined; you can start now."
            if estimate.status == "pending"
            else "Review the estimate before starting."
        )
        return await self.client.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"{status}\nEstimated size: {estimate.size_min_bytes}-"
                    f"{estimate.size_max_bytes} bytes\nEstimated reduction: {reduction} "
                    f"(confidence: {estimate.confidence})\nEstimated processing time: {eta}"
                ),
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "Start", "callback_data": f"file:start:{job_id}"},
                            {
                                "text": "Refresh estimate",
                                "callback_data": f"file:refresh:{job_id}",
                            },
                            {"text": "Discard", "callback_data": f"file:discard:{job_id}"},
                        ],
                        [
                            {
                                "text": "Change settings",
                                "callback_data": f"file:settings:{job_id}",
                            }
                        ],
                    ]
                },
            },
        )

    @staticmethod
    def _telegram_media(message: dict[str, Any]) -> dict[str, Any] | None:
        for kind in ("document", "video", "audio", "animation", "voice", "video_note"):
            value = message.get(kind)
            if isinstance(value, dict) and isinstance(value.get("file_id"), str):
                item = dict(value)
                item.setdefault("file_name", f"telegram-{kind}")
                return item
        photos = message.get("photo")
        if isinstance(photos, list):
            for value in reversed(photos):
                if isinstance(value, dict) and isinstance(value.get("file_id"), str):
                    item = dict(value)
                    item.setdefault("file_name", "telegram-photo.jpg")
                    return item
        return None

    def _authorized(self, chat_id: Any, user_id: Any) -> bool:
        configured_chat = self.settings.telegram_chat_id
        configured_user = self.settings.telegram_user_id
        return (
            isinstance(chat_id, int)
            and isinstance(user_id, int)
            and configured_chat is not None
            and configured_user is not None
            and chat_id == configured_chat
            and user_id == configured_user
        )

    def _download_idempotency_key(self, chat_id: int) -> str | None:
        if self._processing_update_key is None:
            return None
        return f"telegram:{chat_id}:{self._processing_update_key}"

    def _upload_parent_id(self, chat_id: int) -> str | None:
        if self._processing_update_key is None:
            return None
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"media-manager:telegram:{chat_id}:{self._processing_update_key}",
            )
        )

    def _telegram_owner(self) -> str:
        owner = self.settings.telegram_owner_id
        resolver = getattr(self.settings, "owner_id", None)
        return resolver(owner) if owner and callable(resolver) else owner or ""

    @staticmethod
    def _normalize_source_choices(session: dict[str, Any]) -> None:
        source_type = session.get("source_type", "video")
        source_format = session.get("source_format", "ios")
        source_codec = session.get("source_codec", "h264")
        supported = {
            "video": {
                "formats": {"ios", "mp4", "webm", "mkv", "auto"},
                "codecs": {"h264", "h265", "vp9", "av1", "auto"},
                "default_format": "ios",
                "default_codec": "h264",
            },
            "audio": {
                "formats": {"audio", "auto", "m4a", "mp3", "opus", "wav", "flac"},
                "codecs": {"aac", "opus", "auto"},
                "default_format": "m4a",
                "default_codec": "auto",
            },
            "thumbnail": {
                "formats": {"jpg", "auto"},
                "codecs": {"auto"},
                "default_format": "jpg",
                "default_codec": "auto",
            },
        }.get(source_type)
        if supported is None:
            return
        if source_format not in supported["formats"]:
            session["source_format"] = supported["default_format"]
        if source_codec not in supported["codecs"]:
            session["source_codec"] = supported["default_codec"]

    async def _flush_outbox(self) -> None:
        chat_id = self.settings.telegram_chat_id
        if chat_id is None:
            return
        while self._outbox:
            item = self._outbox[0]
            delivery = await self._delivery_kind(item)
            if delivery == "job":
                sent = await self.send_result(item["job_id"], item["owner"])
                if not sent:
                    if not await self._retry_outbox(item):
                        return
                    continue
                async with self._state_lock:
                    if self._outbox and self._outbox[0] == item:
                        await self._complete_outbox_locked(item)
                continue
            if delivery == "download":
                sent = await self._send_download_result(item["job_id"], item["owner"])
                if not sent:
                    if not await self._retry_outbox(item):
                        return
                    continue
                async with self._state_lock:
                    if self._outbox and self._outbox[0] == item:
                        await self._complete_outbox_locked(item)
                continue
            text = await self._notification_text(item)
            try:
                await self.client.call("sendMessage", {"chat_id": chat_id, "text": text[:4000]})
            except Exception:
                if not await self._retry_outbox(item):
                    return
                continue
            async with self._state_lock:
                if self._outbox and self._outbox[0] == item:
                    await self._complete_outbox_locked(item)

    async def _complete_outbox_locked(self, item: dict[str, Any]) -> None:
        self._outbox.pop(0)
        self._delivered_events.append(self._event_key(item))
        self._delivered_events = self._delivered_events[-500:]
        await self._persist_state_locked()

    @staticmethod
    def _event_key(item: dict[str, Any]) -> str:
        if item.get("event") == "download" and isinstance(item.get("event_id"), str):
            return f"download:{item['event_id']}"
        return f"{item.get('event')}:{item.get('owner')}:{item.get('job_id')}"

    async def _retry_outbox(self, item: dict[str, Any]) -> bool:
        async with self._state_lock:
            if not self._outbox or self._outbox[0] != item:
                return bool(self._outbox)
            attempts = int(item.get("attempts", 0)) + 1
            if attempts >= 5:
                self._outbox.pop(0)
                keep_going = bool(self._outbox)
            else:
                item["attempts"] = attempts
                keep_going = False
            await self._persist_state_locked()
        if attempts < 5:
            await asyncio.sleep(min(2**attempts, 15))
        return keep_going

    async def _delivery_kind(self, item: dict[str, Any]) -> str | None:
        event, job_id = item.get("event"), item.get("job_id")
        owner = item.get("owner")
        if event == "ready":
            with suppress(Exception):
                view = await self.jobs.get(job_id, owner)
                if getattr(view, "origin", None) == "telegram":
                    return "job"
        elif event == "download_ready":
            with suppress(Exception):
                view = await self.downloads.get(job_id, owner)
                workflow = getattr(view, "workflow", DownloadWorkflow.DOWNLOAD)
                workflow = getattr(workflow, "value", workflow)
                if (
                    getattr(view, "origin", None) == "telegram"
                    and workflow == DownloadWorkflow.DOWNLOAD.value
                ):
                    return "download"
        return None

    async def _notification_text(self, item: dict[str, Any]) -> str:
        label = item.get("filename") or item.get("job_id")
        size = f" ({item['size']} bytes)" if item.get("size") is not None else ""
        expiry = item.get("expires_at")
        details = f"; expires {expiry}" if expiry else ""
        url = ""
        view = None
        if item.get("event") in {"ready", "download"}:
            with suppress(Exception):
                view = await self.jobs.get(item["job_id"], item["owner"])
        if view is None:
            with suppress(Exception):
                view = await self.downloads.get(item["job_id"], item["owner"])
        url = getattr(getattr(view, "output", None), "download_url", "")
        link = f". Download: {url}" if url else ""
        return f"{item.get('event')}: {label}{size}{details}{link}"

    async def send_result(self, job_id: str, owner: str) -> bool:
        if owner != self._telegram_owner() or self.settings.telegram_chat_id is None:
            return False
        try:
            view = await self.jobs.get(job_id, owner)
            if not view.output:
                return False
            path, filename, _media_type = await self.jobs.download(job_id, owner)
            await self.notify(
                "download",
                job_id,
                owner,
                filename,
                path.stat().st_size,
                view.expires_at,
            )
            if path.stat().st_size > _TELEGRAM_MAX_DOCUMENT_BYTES:
                await self.client.call(
                    "sendMessage",
                    {
                        "chat_id": self.settings.telegram_chat_id,
                        "text": (
                            "Result is too large for Telegram. "
                            + self._result_caption(filename, path.stat().st_size, view)
                        ),
                    },
                )
                return True
            await self.client.send_document(
                self.settings.telegram_chat_id,
                path,
                filename,
                self._result_caption(filename, path.stat().st_size, view),
            )
            return True
        except Exception as exc:
            logger.warning("Telegram result delivery failed (%s)", type(exc).__name__)
            return False

    async def _send_download_result(self, download_id: str, owner: str) -> bool:
        if owner != self._telegram_owner() or self.settings.telegram_chat_id is None:
            return False
        try:
            view = await self.downloads.get(download_id, owner)
            if view.output is None:
                return False
            path, filename, _media_type = await self.downloads.download(download_id, owner)
            await self.notify(
                "download",
                download_id,
                owner,
                filename,
                path.stat().st_size,
                view.expires_at,
            )
            if path.stat().st_size > _TELEGRAM_MAX_DOCUMENT_BYTES:
                await self.client.call(
                    "sendMessage",
                    {
                        "chat_id": self.settings.telegram_chat_id,
                        "text": (
                            "Source is too large for Telegram. "
                            + self._result_caption(filename, path.stat().st_size, view)
                        ),
                    },
                )
                return True
            await self.client.send_document(
                self.settings.telegram_chat_id,
                path,
                filename,
                self._result_caption(filename, path.stat().st_size, view),
            )
            return True
        except Exception as exc:
            logger.warning("Telegram source delivery failed (%s)", type(exc).__name__)
            return False

    @staticmethod
    def _result_caption(filename: str, size: int, view: Any) -> str:
        output = getattr(view, "output", None)
        expiry = getattr(view, "expires_at", None)
        link = getattr(output, "download_url", None)
        details = [filename, f"{size} bytes"]
        if expiry is not None:
            details.append(f"expires {expiry.isoformat()}")
        if link:
            details.append(f"Recovery: {link}")
        return " · ".join(details)

    def _watch_job(self, job_id: str, chat_id: int, message_id: int) -> None:
        if job_id in self._watchers:
            return
        task = asyncio.create_task(
            self._watch_job_loop(job_id, chat_id, message_id), name=f"telegram-job-{job_id}"
        )
        self._watchers[job_id] = task
        task.add_done_callback(lambda _task, key=job_id: self._watchers.pop(key, None))

    def _watch_download(self, download_id: str, chat_id: int, message_id: int) -> None:
        if download_id in self._watchers:
            return
        task = asyncio.create_task(
            self._watch_download_loop(download_id, chat_id, message_id),
            name=f"telegram-download-{download_id}",
        )
        self._watchers[download_id] = task
        task.add_done_callback(lambda _task, key=download_id: self._watchers.pop(key, None))

    async def _watch_download_loop(self, download_id: str, chat_id: int, message_id: int) -> None:
        while not self._stopping:
            try:
                view = await self.downloads.get(download_id, self._telegram_owner())
                await self.edit_progress(chat_id, message_id, view.state.value, download_id)
                if view.child_id:
                    self._watch_job(view.child_id, chat_id, message_id)
                    return
                if view.state.value == "awaiting_review" and view.output is not None:
                    child = await self.downloads.process(
                        download_id, self._telegram_owner(), auto_start=False
                    )
                    child = await self.jobs.get(child.id, self._telegram_owner())
                    conversion, compression = self._review_options_from_child(child)
                    workflow = (
                        "convert_compress"
                        if conversion is not None and compression is not None
                        else "compress"
                        if compression is not None
                        else "convert"
                    )
                    session = self._sessions.setdefault(str(chat_id), {})
                    review = {
                        "job_id": child.id,
                        "workflow": workflow,
                        "phase": "review",
                        "conversion_options": (
                            conversion.model_dump(mode="json")
                            if conversion is not None
                            else None
                        ),
                        "compression_options": (
                            compression.model_dump(mode="json")
                            if compression is not None
                            else None
                        ),
                    }
                    session.setdefault("reviews", {})[child.id] = review
                    if session.get("download_id") == download_id:
                        session.update(
                            {
                                key: value
                                for key, value in review.items()
                                if key != "job_id"
                            }
                        )
                        session["job_id"] = child.id
                    await self._save_state()
                    await self._send_estimate_review(
                        chat_id, child.id, conversion, compression
                    )
                    return
                if view.state.value in {"ready", "failed", "awaiting_review"}:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("Telegram download monitor stopped (%s)", type(exc).__name__)
                return
            await asyncio.sleep(2)

    async def _watch_job_loop(self, job_id: str, chat_id: int, message_id: int) -> None:
        while not self._stopping:
            try:
                job = await self.jobs.get(job_id, self._telegram_owner())
                progress = getattr(job, "progress", None)
                text = job.state.value
                if progress is not None:
                    text += f": {progress.stage.value}"
                    if progress.percent is not None:
                        text += f" {progress.percent}%"
                    if progress.eta_seconds is not None:
                        text += f" · about {progress.eta_seconds} sec remaining"
                await self.edit_progress(chat_id, message_id, text, job_id)
                if job.state.value in {"ready", "failed"}:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("Telegram job monitor stopped (%s)", type(exc).__name__)
                return
            await asyncio.sleep(2)

    async def edit_progress(self, chat_id: int, message_id: int, text: str, key: str) -> None:
        now = asyncio.get_running_loop().time()
        if now - self._last_progress.get(key, 0) < _SEND_PROGRESS_INTERVAL:
            return
        self._last_progress[key] = now
        await self.client.call(
            "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
        )

    def _main_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "Convert", "callback_data": "menu:convert"},
                    {"text": "Compress", "callback_data": "menu:compress"},
                ],
                [{"text": "Recent history", "callback_data": "history:list"}],
            ]
        }

    def _preset_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "MP4 (H.264)", "callback_data": "preset:video-mp4"},
                    {"text": "WebM (VP9)", "callback_data": "preset:video-webm"},
                    {"text": "MP3", "callback_data": "preset:audio-mp3"},
                ],
                [{"text": "Advanced options", "callback_data": "advanced"}],
            ]
        }

    def _url_keyboard(
        self, *, has_saved_conversion: bool = False, auto_process: bool = False
    ) -> dict[str, Any]:
        rows = [
            [
                {"text": "Download best", "callback_data": "url:best"},
                {"text": "Download 1080p", "callback_data": "url:1080p"},
                {"text": "Audio", "callback_data": "url:audio"},
            ],
            [
                {"text": "Convert to MP4", "callback_data": "url:convert-video-mp4"},
                {"text": "Convert to WebM", "callback_data": "url:convert-video-webm"},
                {"text": "Compress", "callback_data": "url:compress"},
            ],
            [{"text": "Convert + compress", "callback_data": "url:convert_compress"}],
        ]
        if has_saved_conversion:
            rows.append(
                [{"text": "Use saved advanced conversion", "callback_data": "url:convert-saved"}]
            )
        rows.append([{"text": "Advanced source options", "callback_data": "url:advanced"}])
        rows.append(
            [
                {
                    "text": f"Auto-process: {'on' if auto_process else 'off'}",
                    "callback_data": "url:auto",
                }
            ]
        )
        return {"inline_keyboard": rows}

    async def _save_state(self) -> None:
        async with self._state_lock:
            await self._persist_state_locked()

    async def _persist_state_locked(self) -> None:
        await asyncio.to_thread(self._write_state)

    def _write_state(self) -> None:
        tmp = self._state_file.with_name(f".telegram-{uuid.uuid4().hex}.tmp")
        safe_sessions = {
            key: {field: value for field, value in session.items() if field != "url"}
            for key, session in self._sessions.items()
        }
        payload = {
            "version": 1,
            "offset": self._offset,
            "sessions": safe_sessions,
            "outbox": self._outbox[-100:],
            "delivered_events": self._delivered_events[-500:],
        }
        with tmp.open("x", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self._state_file)
        directory_fd = os.open(
            self._state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load_state(self) -> None:
        if self._state_file.is_symlink() or not self._state_file.is_file():
            return
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return
            offset = payload.get("offset", 0)
            sessions = payload.get("sessions", {})
            outbox = payload.get("outbox", [])
            delivered = payload.get("delivered_events", [])
            if isinstance(offset, int) and offset >= 0:
                self._offset = offset
            if isinstance(sessions, dict):
                allowed_chat = str(self.settings.telegram_chat_id)
                session = sessions.get(allowed_chat)
                if isinstance(session, dict):
                    self._sessions = {allowed_chat: self._validated_session(session)}
            if isinstance(outbox, list):
                self._outbox = [
                    valid
                    for item in outbox[-100:]
                    if isinstance(item, dict)
                    if (valid := self._validated_outbox_item(item)) is not None
                ]
            if isinstance(delivered, list):
                self._delivered_events = [
                    item for item in delivered[-500:] if isinstance(item, str) and len(item) <= 500
                ]
        except (OSError, ValueError, TypeError):
            logger.warning("Ignoring invalid Telegram state file")

    @staticmethod
    def _validated_session(value: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in ("job_id", "download_id"):
            item_id = value.get(name)
            if isinstance(item_id, str):
                try:
                    if str(uuid.UUID(item_id)) == item_id:
                        result[name] = item_id
                except ValueError:
                    pass
        for name in ("message_id", "download_message_id"):
            message_id = value.get(name)
            if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
                result[name] = message_id
        workflow = value.get("workflow")
        if workflow in {"download", "convert", "compress", "convert_compress"}:
            result["workflow"] = workflow
        if isinstance(value.get("auto_process"), bool):
            result["auto_process"] = value["auto_process"]
        phase = value.get("phase")
        if isinstance(phase, str) and phase in {
            "download_options",
            "watching",
            "preset",
            "awaiting_file",
            "review",
            "advanced",
            "input_quality",
            "input_target_mb",
        }:
            result["phase"] = phase
        for name in ("source_quality", "source_type", "source_format", "source_codec"):
            item = value.get(name)
            if isinstance(item, str) and len(item) <= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", item):
                result[name] = item
        if isinstance(value.get("conversion_options"), dict):
            with suppress(Exception):
                result["conversion_options"] = ConversionOptions.model_validate(
                    value["conversion_options"]
                ).model_dump(mode="json")
        if isinstance(value.get("compression_options"), dict):
            with suppress(Exception):
                result["compression_options"] = CompressionOptions.model_validate(
                    value["compression_options"]
                ).model_dump(mode="json")
        return result

    @staticmethod
    def _validated_outbox_item(value: dict[str, Any]) -> dict[str, Any] | None:
        event, job_id, owner = value.get("event"), value.get("job_id"), value.get("owner")
        if event not in {"ready", "download_ready", "download", "failed", "download_failed"}:
            return None
        if not isinstance(job_id, str) or not isinstance(owner, str) or not owner:
            return None
        try:
            if str(uuid.UUID(job_id)) != job_id:
                return None
        except ValueError:
            return None
        filename = value.get("filename")
        if filename is not None and (
            not isinstance(filename, str)
            or len(filename) > 180
            or Path(filename).name != filename
            or any(char in filename for char in "\r\n")
        ):
            return None
        size = value.get("size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            return None
        expires = value.get("expires_at")
        if expires is not None:
            try:
                if datetime.fromisoformat(expires).tzinfo is None:
                    return None
            except (TypeError, ValueError):
                return None
        attempts = value.get("attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts < 5:
            return None
        event_id = value.get("event_id")
        if event_id is not None and (not isinstance(event_id, str) or len(event_id) > 64):
            return None
        return {
            "event": event,
            "event_id": event_id,
            "job_id": job_id,
            "owner": owner,
            "filename": filename,
            "size": size,
            "expires_at": expires,
            "attempts": attempts,
        }


def _looks_like_url(value: str) -> bool:
    return value.startswith(("https://", "http://")) and len(value) <= 4096


def _button_rows(buttons: list[dict[str, str]], width: int = 3) -> list[list[dict[str, str]]]:
    return [buttons[index : index + width] for index in range(0, len(buttons), width)]
