from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse


class AuthMode(StrEnum):
    CLOUDFLARE = "cloudflare"
    DISABLED = "disabled"


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default

    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc

    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    work_dir: Path = Path("/work")
    auth_mode: AuthMode = AuthMode.CLOUDFLARE
    public_base_url: str = "http://localhost:8080"
    cloudflare_issuer: str | None = None
    cloudflare_audience: str | None = None
    ffmpeg_path: str = "/usr/bin/ffmpeg"
    ffprobe_path: str = "/usr/bin/ffprobe"
    max_upload_bytes: int = 5 * 1024 * 1024 * 1024
    max_output_bytes: int = 5 * 1024 * 1024 * 1024
    max_chunk_bytes: int = 50 * 1024 * 1024
    max_live_jobs: int = 1
    result_ttl_seconds: int = 7 * 24 * 60 * 60
    upload_session_ttl_seconds: int = 2 * 60 * 60
    cleanup_interval_seconds: int = 30
    upload_timeout_seconds: int = 5 * 60
    probe_timeout_seconds: int = 15
    max_duration_seconds: int = 10 * 60
    max_image_pixels: int = 50_000_000
    max_animation_pixels: int = 4_000_000
    max_axis_pixels: int = 16_384
    # Phone recordings can include many metadata tracks alongside video/audio.
    max_streams: int = 64
    ffmpeg_threads: int = 2
    principal_owners: dict[str, str] = field(default_factory=dict)
    metube_sidecar_url: str | None = None
    metube_sidecar_api_key: str | None = field(default=None, repr=False)
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_chat_id: int | None = None
    telegram_user_id: int | None = None
    telegram_owner_id: str | None = None
    telegram_file_root: Path | None = None

    def owner_id(self, principal_id: str) -> str:
        return self.principal_owners.get(principal_id, principal_id)

    def __post_init__(self) -> None:
        if not isinstance(self.principal_owners, dict):
            raise ValueError("PRINCIPAL_OWNERS must be a JSON object")
        for source, owner in self.principal_owners.items():
            if (
                not isinstance(source, str) or not isinstance(owner, str)
                or not source.startswith(("user:", "service:", "telegram:"))
                or not owner.startswith("user:") or len(owner) <= 5
                or source in self.principal_owners.values()
            ):
                raise ValueError("PRINCIPAL_OWNERS must map identities directly to user owners")
        for name, value in (
            ("METUBE_SIDECAR_URL", self.metube_sidecar_url),
            ("TELEGRAM_API_BASE_URL", self.telegram_api_base_url),
        ):
            if value is None:
                continue
            parsed = urlparse(value)
            if (
                parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or value.endswith("/")
            ):
                raise ValueError(f"{name} must be an origin URL")
        if self.telegram_bot_token and (
            not self.telegram_chat_id or self.telegram_chat_id <= 0
            or not self.telegram_user_id or self.telegram_user_id <= 0
            or not self.telegram_owner_id
            or not self.telegram_owner_id.startswith("user:")
        ):
            raise ValueError("Telegram requires a private chat, authorized user and owner")
        public_url = urlparse(self.public_base_url)
        if (
            public_url.scheme not in {"http", "https"}
            or not public_url.netloc
            or public_url.path not in {"", "/"}
            or public_url.query
            or public_url.fragment
            or public_url.username is not None
            or public_url.password is not None
            or self.public_base_url.endswith("/")
        ):
            raise ValueError(
                "MEDIA_MANAGER_PUBLIC_BASE_URL must be an origin URL without a trailing slash"
            )

        if self.auth_mode is AuthMode.CLOUDFLARE:
            if not self.cloudflare_issuer or not self.cloudflare_audience:
                raise ValueError(
                    "MEDIA_MANAGER_CF_ISSUER and MEDIA_MANAGER_CF_AUDIENCE are required "
                    "when Cloudflare authentication is enabled"
                )
            parsed = urlparse(self.cloudflare_issuer)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
                or not parsed.hostname
                or not parsed.hostname.endswith(".cloudflareaccess.com")
            ):
                raise ValueError("MEDIA_MANAGER_CF_ISSUER must be an HTTPS origin URL")
            if public_url.scheme != "https":
                raise ValueError(
                    "MEDIA_MANAGER_PUBLIC_BASE_URL must use HTTPS when Cloudflare "
                    "authentication is enabled"
                )

        numeric_values = {
            "max_upload_bytes": self.max_upload_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_chunk_bytes": self.max_chunk_bytes,
            "max_live_jobs": self.max_live_jobs,
            "result_ttl_seconds": self.result_ttl_seconds,
            "upload_session_ttl_seconds": self.upload_session_ttl_seconds,
            "cleanup_interval_seconds": self.cleanup_interval_seconds,
            "upload_timeout_seconds": self.upload_timeout_seconds,
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "max_duration_seconds": self.max_duration_seconds,
            "max_image_pixels": self.max_image_pixels,
            "max_animation_pixels": self.max_animation_pixels,
            "max_axis_pixels": self.max_axis_pixels,
            "max_streams": self.max_streams,
            "ffmpeg_threads": self.ffmpeg_threads,
        }
        invalid = [name for name, value in numeric_values.items() if value <= 0]
        if invalid:
            raise ValueError(f"Settings must be greater than zero: {', '.join(invalid)}")

    @classmethod
    def from_env(cls) -> Settings:
        raw_mode = os.getenv("MEDIA_MANAGER_AUTH_MODE", AuthMode.CLOUDFLARE.value)
        try:
            auth_mode = AuthMode(raw_mode.lower())
        except ValueError as exc:
            allowed = ", ".join(mode.value for mode in AuthMode)
            raise ValueError(f"MEDIA_MANAGER_AUTH_MODE must be one of: {allowed}") from exc

        issuer = os.getenv("MEDIA_MANAGER_CF_ISSUER")
        if issuer:
            issuer = issuer.rstrip("/")

        return cls(
            principal_owners=json.loads(os.getenv("MEDIA_MANAGER_PRINCIPAL_OWNERS", "{}")),
            metube_sidecar_url=os.getenv("MEDIA_MANAGER_METUBE_SIDECAR_URL") or None,
            metube_sidecar_api_key=os.getenv("MEDIA_MANAGER_METUBE_SIDECAR_API_KEY") or None,
            telegram_bot_token=os.getenv("MEDIA_MANAGER_TELEGRAM_BOT_TOKEN") or None,
            telegram_api_base_url=os.getenv(
                "MEDIA_MANAGER_TELEGRAM_API_BASE_URL", "https://api.telegram.org"
            ),
            telegram_chat_id=_optional_int("MEDIA_MANAGER_TELEGRAM_CHAT_ID"),
            telegram_user_id=_optional_int("MEDIA_MANAGER_TELEGRAM_USER_ID"),
            telegram_owner_id=os.getenv("MEDIA_MANAGER_TELEGRAM_OWNER_ID") or None,
            telegram_file_root=(
                Path(os.environ["MEDIA_MANAGER_TELEGRAM_FILE_ROOT"])
                if os.getenv("MEDIA_MANAGER_TELEGRAM_FILE_ROOT") else None
            ),
            work_dir=Path(os.getenv("MEDIA_MANAGER_WORK_DIR", "/work")),
            auth_mode=auth_mode,
            public_base_url=os.getenv(
                "MEDIA_MANAGER_PUBLIC_BASE_URL", "http://localhost:8080"
            ),
            cloudflare_issuer=issuer,
            cloudflare_audience=os.getenv("MEDIA_MANAGER_CF_AUDIENCE"),
            ffmpeg_path=os.getenv("MEDIA_MANAGER_FFMPEG_PATH", "/usr/bin/ffmpeg"),
            ffprobe_path=os.getenv("MEDIA_MANAGER_FFPROBE_PATH", "/usr/bin/ffprobe"),
            max_upload_bytes=_positive_int(
                "MEDIA_MANAGER_MAX_UPLOAD_BYTES", 5 * 1024 * 1024 * 1024
            ),
            max_output_bytes=_positive_int(
                "MEDIA_MANAGER_MAX_OUTPUT_BYTES", 5 * 1024 * 1024 * 1024
            ),
            max_chunk_bytes=_positive_int(
                "MEDIA_MANAGER_MAX_CHUNK_BYTES", 50 * 1024 * 1024
            ),
            max_live_jobs=_positive_int("MEDIA_MANAGER_MAX_LIVE_JOBS", 1),
            result_ttl_seconds=_positive_int(
                "MEDIA_MANAGER_RESULT_TTL_SECONDS", 7 * 24 * 60 * 60
            ),
            upload_session_ttl_seconds=_positive_int(
                "MEDIA_MANAGER_UPLOAD_SESSION_TTL_SECONDS", 2 * 60 * 60
            ),
            cleanup_interval_seconds=_positive_int(
                "MEDIA_MANAGER_CLEANUP_INTERVAL_SECONDS", 30
            ),
            upload_timeout_seconds=_positive_int(
                "MEDIA_MANAGER_UPLOAD_TIMEOUT_SECONDS", 5 * 60
            ),
            probe_timeout_seconds=_positive_int("MEDIA_MANAGER_PROBE_TIMEOUT_SECONDS", 15),
            max_duration_seconds=_positive_int("MEDIA_MANAGER_MAX_DURATION_SECONDS", 10 * 60),
            max_image_pixels=_positive_int("MEDIA_MANAGER_MAX_IMAGE_PIXELS", 50_000_000),
            max_animation_pixels=_positive_int(
                "MEDIA_MANAGER_MAX_ANIMATION_PIXELS", 4_000_000
            ),
            max_axis_pixels=_positive_int("MEDIA_MANAGER_MAX_AXIS_PIXELS", 16_384),
            max_streams=_positive_int("MEDIA_MANAGER_MAX_STREAMS", 64),
            ffmpeg_threads=_positive_int("MEDIA_MANAGER_FFMPEG_THREADS", 2),
        )


def _optional_int(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value else None
