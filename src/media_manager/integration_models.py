from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from media_manager.models import CompressionOptions, ConversionOptions, JobError, OutputMetadata


class DownloadType(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    THUMBNAIL = "thumbnail"


class DownloadWorkflow(StrEnum):
    DOWNLOAD = "download"
    CONVERT = "convert"
    COMPRESS = "compress"
    CONVERT_COMPRESS = "convert_compress"


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    quality: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] = (
        "best"
    )
    format: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] = (
        "ios"
    )
    download_type: DownloadType = DownloadType.VIDEO
    codec: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] = (
        "h264"
    )
    workflow: DownloadWorkflow = DownloadWorkflow.DOWNLOAD
    auto_process: bool = False
    conversion_options: ConversionOptions | None = None
    compression_options: CompressionOptions | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: HttpUrl) -> HttpUrl:
        if len(str(value)) > 4096:
            raise ValueError("URL exceeds the allowed length")
        if value.username is not None or value.password is not None:
            raise ValueError("URL credentials are not allowed")
        return value


class DownloadState(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    AWAITING_REVIEW = "awaiting_review"
    READY = "ready"
    FAILED = "failed"


class DownloadView(BaseModel):
    id: str
    state: DownloadState
    created_at: datetime
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    status_url: str
    source_filename: str | None = None
    output: OutputMetadata | None = None
    error: JobError | None = None
    submission_unknown: bool = False
    parent_id: str | None = None
    child_id: str | None = None
    workflow: DownloadWorkflow = DownloadWorkflow.DOWNLOAD
    origin: str = "metube"
