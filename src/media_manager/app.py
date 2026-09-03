from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

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
    UploadView,
)
from media_manager.processor import MediaProcessor, target_capabilities

PrincipalDependency = Annotated[Principal, Depends(get_principal)]
TargetQuery = Annotated[Target, Query()]
QualityQuery = Annotated[Quality, Query()]
ResolutionQuery = Annotated[Resolution, Query()]
AudioQuery = Annotated[AudioMode, Query()]
WEB_DIR = Path(__file__).with_name("web")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    allowed_origin = _origin(resolved_settings.public_base_url)

    @app.middleware("http")
    async def secure_browser_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in UNSAFE_METHODS and _is_cross_site(request, allowed_origin):
            body = ErrorResponse(
                error=ErrorBody(
                    code="CROSS_SITE_REQUEST",
                    message="Cross-site requests are not allowed",
                )
            )
            response: Response = JSONResponse(
                status_code=403,
                content=body.model_dump(mode="json"),
            )
        else:
            response = await call_next(request)

        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; media-src blob:; connect-src 'self'; manifest-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'",
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/v1/"):
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

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

    @app.get("/", include_in_schema=False)
    async def web_app(_principal: PrincipalDependency) -> FileResponse:
        return _web_file("index.html", "text/html", cache_control="no-cache")

    @app.get("/assets/app.css", include_in_schema=False)
    async def web_styles(_principal: PrincipalDependency) -> FileResponse:
        return _web_file("app.css", "text/css")

    @app.get("/assets/app.js", include_in_schema=False)
    async def web_script(_principal: PrincipalDependency) -> FileResponse:
        return _web_file("app.js", "text/javascript")

    @app.get("/assets/logo.svg", include_in_schema=False)
    async def web_logo(_principal: PrincipalDependency) -> FileResponse:
        return _web_file("logo.svg", "image/svg+xml")

    @app.get("/favicon.svg", include_in_schema=False)
    async def web_favicon(_principal: PrincipalDependency) -> FileResponse:
        return _web_file("mark.svg", "image/svg+xml")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def web_manifest(_principal: PrincipalDependency) -> FileResponse:
        return _web_file(
            "manifest.webmanifest",
            "application/manifest+json",
            cache_control="no-cache",
        )

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker(_principal: PrincipalDependency) -> FileResponse:
        response = _web_file("sw.js", "text/javascript", cache_control="no-cache")
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.get("/assets/icon-192.png", include_in_schema=False)
    async def web_icon_small(_principal: PrincipalDependency) -> FileResponse:
        return _web_file("icon-192.png", "image/png")

    @app.get("/assets/icon-512.png", include_in_schema=False)
    async def web_icon_large(_principal: PrincipalDependency) -> FileResponse:
        return _web_file("icon-512.png", "image/png")

    @app.get("/assets/apple-touch-icon.png", include_in_schema=False)
    async def apple_touch_icon(_principal: PrincipalDependency) -> FileResponse:
        return _web_file("apple-touch-icon.png", "image/png")

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_schema(_principal: PrincipalDependency) -> JSONResponse:
        return JSONResponse(content=app.openapi())

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
            max_chunk_bytes=min(
                resolved_settings.max_chunk_bytes,
                resolved_settings.max_upload_bytes,
            ),
            result_ttl_seconds=resolved_settings.result_ttl_seconds,
        )

    @app.post(
        "/v1/uploads",
        response_model=UploadView,
        status_code=201,
        responses={
            401: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            507: {"model": ErrorResponse},
        },
    )
    async def create_upload(
        request: Request,
        principal: PrincipalDependency,
        target: TargetQuery,
        quality: QualityQuery = Quality.BALANCED,
        resolution: ResolutionQuery = Resolution.SOURCE,
        audio: AudioQuery = AudioMode.KEEP,
    ) -> UploadView:
        _validate_options(target, resolution, audio)
        upload_length = _required_header_int(request, "Upload-Length")
        if upload_length == 0:
            raise ApiError(422, "EMPTY_INPUT", "Upload body is empty")
        if upload_length > resolved_settings.max_upload_bytes:
            raise ApiError(413, "INPUT_TOO_LARGE", "Upload exceeds the size limit")

        options = ConversionOptions(
            target=target,
            quality=quality,
            resolution=resolution,
            audio=audio,
        )
        job = await manager.reserve(principal.id, options, upload_length)
        try:
            try:
                async with aiofiles.open(job.input_path, "xb"):
                    pass
            except OSError as exc:
                raise ApiError(
                    507,
                    "INSUFFICIENT_STORAGE",
                    "Upload workspace is unavailable",
                ) from exc
            await _chmod_private(job.input_path)
            return await manager.upload_view(job.id, principal.id)
        except BaseException:
            await manager.discard(job.id)
            raise

    @app.get(
        "/v1/uploads/{job_id}",
        response_model=UploadView,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def get_upload(job_id: str, principal: PrincipalDependency) -> UploadView:
        return await manager.upload_view(job_id, principal.id)

    @app.patch(
        "/v1/uploads/{job_id}",
        response_model=UploadView,
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            408: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            415: {"model": ErrorResponse},
            507: {"model": ErrorResponse},
        },
    )
    async def append_upload(
        job_id: str,
        request: Request,
        principal: PrincipalDependency,
    ) -> UploadView:
        upload_offset = _required_header_int(request, "Upload-Offset")
        content_length = _required_header_int(request, "Content-Length")
        chunk_limit = min(
            resolved_settings.max_chunk_bytes,
            resolved_settings.max_upload_bytes,
        )
        if content_length == 0 or content_length > chunk_limit:
            raise ApiError(413, "CHUNK_TOO_LARGE", "Upload chunk exceeds the size limit")
        _validate_raw_body_headers(request)

        async with manager.upload_chunk(job_id, principal.id, upload_offset) as job:
            expected = job.expected_input_bytes
            if expected is None or upload_offset + content_length > expected:
                raise ApiError(413, "INPUT_TOO_LARGE", "Upload exceeds its declared size")

            received = 0
            try:
                try:
                    async with asyncio.timeout(resolved_settings.upload_timeout_seconds):
                        async with aiofiles.open(job.input_path, "r+b") as destination:
                            await destination.seek(upload_offset)
                            async for chunk in request.stream():
                                if not chunk:
                                    continue
                                received += len(chunk)
                                if received > content_length:
                                    raise ApiError(
                                        400,
                                        "CONTENT_LENGTH_MISMATCH",
                                        "Upload chunk length does not match its header",
                                    )
                                await destination.write(chunk)
                except TimeoutError as exc:
                    raise ApiError(
                        408,
                        "UPLOAD_TIMEOUT",
                        "Upload chunk exceeded its time limit",
                    ) from exc
                except OSError as exc:
                    raise ApiError(
                        507,
                        "INSUFFICIENT_STORAGE",
                        "Upload workspace is unavailable",
                    ) from exc

                if received != content_length:
                    raise ApiError(
                        400,
                        "CONTENT_LENGTH_MISMATCH",
                        "Upload chunk length does not match its header",
                    )
                return await manager.advance_upload(job, received)
            except BaseException:
                await _truncate(job.input_path, upload_offset)
                raise

    @app.post(
        "/v1/uploads/{job_id}/complete",
        response_model=JobView,
        status_code=202,
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
        },
    )
    async def complete_upload(job_id: str, principal: PrincipalDependency) -> JobView:
        upload = await manager.upload_view(job_id, principal.id)
        return await manager.enqueue(job_id, upload.offset)

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
        content_length = _validate_upload_headers(
            request,
            resolved_settings.max_upload_bytes,
        )
        options = ConversionOptions(
            target=target,
            quality=quality,
            resolution=resolution,
            audio=audio,
        )
        job = await manager.reserve(principal.id, options, content_length)
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


def _validate_upload_headers(request: Request, max_upload_bytes: int) -> int | None:
    _validate_raw_body_headers(request)
    raw_length = request.headers.get("Content-Length")
    if raw_length is None:
        return None
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise ApiError(400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid") from exc
    if content_length < 0:
        raise ApiError(400, "INVALID_CONTENT_LENGTH", "Content-Length is invalid")
    if content_length > max_upload_bytes:
        raise ApiError(413, "INPUT_TOO_LARGE", "Upload exceeds the size limit")
    return content_length


def _validate_raw_body_headers(request: Request) -> None:
    content_encoding = request.headers.get("Content-Encoding")
    if content_encoding and content_encoding.lower() != "identity":
        raise ApiError(415, "UNSUPPORTED_ENCODING", "Compressed request bodies are unsupported")

    content_type = request.headers.get("Content-Type", "")
    if content_type.lower().startswith("multipart/"):
        raise ApiError(415, "RAW_FILE_REQUIRED", "Send the media file as the raw request body")


def _required_header_int(request: Request, name: str) -> int:
    value = request.headers.get(name)
    if value is None:
        raise ApiError(400, "UPLOAD_HEADER_REQUIRED", f"{name} header is required")
    return _parse_nonnegative_int(value, name)


def _parse_nonnegative_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiError(400, "INVALID_UPLOAD_HEADER", f"{name} header is invalid") from exc
    if parsed < 0:
        raise ApiError(400, "INVALID_UPLOAD_HEADER", f"{name} header is invalid")
    return parsed


async def _chmod_private(path: os.PathLike[str]) -> None:
    await asyncio.to_thread(os.chmod, path, 0o600)


async def _truncate(path: os.PathLike[str], length: int) -> None:
    def truncate() -> None:
        with open(path, "r+b") as file:
            file.truncate(length)

    await asyncio.to_thread(truncate)


def _web_file(
    name: str,
    media_type: str,
    *,
    cache_control: str = "public, max-age=3600",
) -> FileResponse:
    return FileResponse(
        WEB_DIR / name,
        media_type=media_type,
        headers={"Cache-Control": cache_control},
    )


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _is_cross_site(request: Request, allowed_origin: str) -> bool:
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("Origin")
    if origin:
        return _origin(origin) != allowed_origin
    return "Sec-Fetch-Mode" in request.headers
