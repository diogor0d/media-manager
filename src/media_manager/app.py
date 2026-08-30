from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import aiofiles
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from media_manager import __version__
from media_manager.auth import Authenticator, Principal, get_principal
from media_manager.config import Settings
from media_manager.errors import ApiError
from media_manager.jobs import JobManager, Processor
from media_manager.models import (
    AudioMode,
    Capabilities,
    CapabilityOption,
    ConversionOptions,
    ErrorBody,
    ErrorResponse,
    JobView,
    Quality,
    Resolution,
    Target,
)
from media_manager.processor import MediaProcessor, target_capabilities

PrincipalDependency = Annotated[Principal, Depends(get_principal)]
TargetQuery = Annotated[Target, Query()]
QualityQuery = Annotated[Quality, Query()]
ResolutionQuery = Annotated[Resolution, Query()]
AudioQuery = Annotated[AudioMode, Query()]


def create_app(
    settings: Settings | None = None,
    *,
    processor: Processor | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_processor = processor or MediaProcessor(resolved_settings)
    manager = JobManager(resolved_settings, resolved_processor)
    authenticator = Authenticator(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.jobs = manager
        app.state.authenticator = authenticator
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(
        title="Media Manager API",
        version=__version__,
        description="Bounded, preset-based media conversion for trusted automations.",
        lifespan=lifespan,
    )

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        body = ErrorResponse(error=ErrorBody(code=exc.code, message=exc.message))
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json"),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorBody(code="INVALID_REQUEST", message="Request parameters are invalid")
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        if manager.ready:
            return JSONResponse(status_code=200, content={"status": "ready"})
        return JSONResponse(status_code=503, content={"status": "not_ready"})

    @app.get("/v1/capabilities", response_model=Capabilities)
    async def capabilities(_principal: PrincipalDependency) -> Capabilities:
        return Capabilities(
            targets=target_capabilities(),
            qualities=[
                CapabilityOption(value=Quality.ECONOMY, label="Smaller file"),
                CapabilityOption(value=Quality.BALANCED, label="Balanced"),
                CapabilityOption(value=Quality.HIGH, label="Higher quality"),
            ],
            resolutions=[
                CapabilityOption(value=Resolution.SOURCE, label="Original resolution"),
                CapabilityOption(value=Resolution.P480, label="480p / 854 px long edge"),
                CapabilityOption(value=Resolution.P720, label="720p / 1280 px long edge"),
                CapabilityOption(value=Resolution.P1080, label="1080p / 1920 px long edge"),
                CapabilityOption(value=Resolution.P1440, label="1440p / 2560 px long edge"),
                CapabilityOption(value=Resolution.P2160, label="2160p / 3840 px long edge"),
            ],
            audio_modes=[
                CapabilityOption(value=AudioMode.KEEP, label="Keep audio"),
                CapabilityOption(value=AudioMode.DROP, label="Remove audio"),
            ],
            max_upload_bytes=resolved_settings.max_upload_bytes,
            result_ttl_seconds=resolved_settings.result_ttl_seconds,
        )

    @app.post(
        "/v1/jobs",
        response_model=JobView,
        status_code=202,
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            408: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            415: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            507: {"model": ErrorResponse},
        },
    )
    async def create_job(
        request: Request,
        principal: PrincipalDependency,
        target: TargetQuery,
        quality: QualityQuery = Quality.BALANCED,
        resolution: ResolutionQuery = Resolution.SOURCE,
        audio: AudioQuery = AudioMode.KEEP,
    ) -> JobView:
        _validate_options(target, resolution, audio)
        _validate_upload_headers(request, resolved_settings.max_upload_bytes)
        options = ConversionOptions(
            target=target,
            quality=quality,
            resolution=resolution,
            audio=audio,
        )
        job = await manager.reserve(principal.id, options)
        received = 0

        try:
            try:
                async with asyncio.timeout(resolved_settings.upload_timeout_seconds):
                    async with aiofiles.open(job.input_path, "xb") as destination:
                        async for chunk in request.stream():
                            if not chunk:
                                continue
                            received += len(chunk)
                            if received > resolved_settings.max_upload_bytes:
                                raise ApiError(
                                    413,
                                    "INPUT_TOO_LARGE",
                                    "Upload exceeds the size limit",
                                )
                            await destination.write(chunk)
            except TimeoutError as exc:
                raise ApiError(408, "UPLOAD_TIMEOUT", "Upload exceeded its time limit") from exc
            except OSError as exc:
                raise ApiError(
                    507,
                    "INSUFFICIENT_STORAGE",
                    "Upload workspace is unavailable",
                ) from exc
            await _chmod_private(job.input_path)

            if received == 0:
                raise ApiError(422, "EMPTY_INPUT", "Upload body is empty")
            return await manager.enqueue(job.id, received)
        except BaseException:
            await manager.discard(job.id)
            raise

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=JobView,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def get_job(
        job_id: str,
        principal: PrincipalDependency,
    ) -> JobView:
        return await manager.get(job_id, principal.id)

    @app.get(
        "/v1/jobs/{job_id}/content",
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            410: {"model": ErrorResponse},
        },
    )
    async def download_job(
        job_id: str,
        principal: PrincipalDependency,
    ) -> FileResponse:
        path, filename, media_type = await manager.download(job_id, principal.id)
        return FileResponse(path=path, filename=filename, media_type=media_type)

    @app.delete(
        "/v1/jobs/{job_id}",
        status_code=204,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def delete_job(
        job_id: str,
        principal: PrincipalDependency,
    ) -> Response:
        await manager.delete(job_id, principal.id)
        return Response(status_code=204)

    return app


def _validate_options(target: Target, resolution: Resolution, audio: AudioMode) -> None:
    audio_targets = {Target.AUDIO_M4A, Target.AUDIO_MP3, Target.AUDIO_OPUS}
    video_targets = {Target.VIDEO_MP4, Target.VIDEO_WEBM}
    if target in audio_targets and resolution is not Resolution.SOURCE:
        raise ApiError(422, "INVALID_OPTIONS", "Resolution does not apply to audio output")
    if target not in video_targets and audio is AudioMode.DROP:
        raise ApiError(422, "INVALID_OPTIONS", "Audio removal applies only to video output")
    if target is Target.ANIMATION_GIF and resolution in {Resolution.P1440, Resolution.P2160}:
        raise ApiError(422, "INVALID_OPTIONS", "GIF output is limited to 1080p")


def _validate_upload_headers(request: Request, max_upload_bytes: int) -> None:
    content_encoding = request.headers.get("Content-Encoding")
    if content_encoding and content_encoding.lower() != "identity":
        raise ApiError(415, "UNSUPPORTED_ENCODING", "Compressed request bodies are unsupported")

    content_type = request.headers.get("Content-Type", "")
    if content_type.lower().startswith("multipart/"):
        raise ApiError(415, "RAW_FILE_REQUIRED", "Send the media file as the raw request body")

    raw_length = request.headers.get("Content-Length")
    if raw_length is None:
        return
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise ApiError(400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid") from exc
    if content_length < 0:
        raise ApiError(400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid")
    if content_length > max_upload_bytes:
        raise ApiError(413, "INPUT_TOO_LARGE", "Upload exceeds the size limit")


async def _chmod_private(path: os.PathLike[str]) -> None:
    await asyncio.to_thread(os.chmod, path, 0o600)
