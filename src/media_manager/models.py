from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Target(StrEnum):
    VIDEO_MP4 = "video-mp4"
    VIDEO_WEBM = "video-webm"
    IMAGE_JPEG = "image-jpeg"
    IMAGE_PNG = "image-png"
    IMAGE_WEBP = "image-webp"
    ANIMATION_GIF = "animation-gif"
    AUDIO_M4A = "audio-m4a"
    AUDIO_MP3 = "audio-mp3"
    AUDIO_OPUS = "audio-opus"


class Quality(StrEnum):
    ECONOMY = "economy"
    BALANCED = "balanced"
    HIGH = "high"


class Resolution(StrEnum):
    SOURCE = "source"
    P480 = "480p"
    P720 = "720p"
    P1080 = "1080p"
    P1440 = "1440p"
    P2160 = "2160p"


class AudioMode(StrEnum):
    KEEP = "keep"
    DROP = "drop"


class JobState(StrEnum):
    UPLOADING = "uploading"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class MediaClass(StrEnum):
    VIDEO = "video"
    IMAGE = "image"
    ANIMATION = "animation"
    AUDIO = "audio"


class ConversionOptions(BaseModel):
    model_config = ConfigDict(frozen=True)

    target: Target
    quality: Quality = Quality.BALANCED
    resolution: Resolution = Resolution.SOURCE
    audio: AudioMode = AudioMode.KEEP


class MediaMetadata(BaseModel):
    bytes: int = Field(ge=0)
    media_class: MediaClass
    container: str
    duration_ms: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    video_codec: str | None = None
    audio_codec: str | None = None


class OutputMetadata(BaseModel):
    bytes: int = Field(ge=0)
    filename: str
    media_type: str
    download_url: str
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    duration_ms: int | None = Field(default=None, ge=0)


class JobError(BaseModel):
    code: str
    message: str


class JobView(BaseModel):
    id: str
    state: JobState
    target: Target
    quality: Quality
    resolution: Resolution
    audio: AudioMode
    created_at: datetime
    expires_at: datetime | None = None
    status_url: str
    input: MediaMetadata | None = None
    output: OutputMetadata | None = None
    error: JobError | None = None


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class CapabilityOption(BaseModel):
    value: str
    label: str


class TargetCapability(BaseModel):
    value: Target
    label: str
    media_type: str
    extension: str
    accepts: list[MediaClass]
    allowed_resolutions: list[Resolution]
    allowed_audio_modes: list[AudioMode]


class Capabilities(BaseModel):
    targets: list[TargetCapability]
    qualities: list[CapabilityOption]
    resolutions: list[CapabilityOption]
    audio_modes: list[CapabilityOption]
    max_upload_bytes: int
    max_chunk_bytes: int
    result_ttl_seconds: int


class UploadView(BaseModel):
    id: str
    offset: int = Field(ge=0)
    length: int = Field(gt=0)
    chunk_size: int = Field(gt=0)
    upload_url: str
    expires_at: datetime
