from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, urlsplit

import aiofiles
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from media_manager import __version__
from media_manager.auth import Authenticator, Principal, get_principal
from media_manager.config import Settings
from media_manager.errors import ApiError, ProcessingError
from media_manager.integration_models import DownloadRequest
from media_manager.jobs import JobManager, Processor
from media_manager.models import (
    AudioMode,
    Capabilities,
    CapabilityOption,
    CompressionJobView,
    CompressionOptions,
    ConversionOptions,
    ErrorBody,
    ErrorResponse,
    EstimateView,
    HistorySettings,
    JobView,
    MediaMetadata,
    ProcessingRequest,
    Quality,
    QualityScale,
    Resolution,
    Target,
    UploadView,
)
from media_manager.processor import COMPRESSION_TARGET_BYTES, MediaProcessor, target_capabilities
from media_manager.telegram import TelegramService
from media_manager.workflows import DownloadManager

PrincipalDependency = Annotated[Principal, Depends(get_principal)]
TargetQuery = Annotated[Target, Query()]
QualityQuery = Annotated[Quality | None, Query()]
QualityPercentQuery = Annotated[int | None, Query(ge=0, le=100)]
ResolutionQuery = Annotated[Resolution, Query()]
AudioQuery = Annotated[AudioMode, Query()]
WEB_DIR = Path(__file__).with_name("web")
UPLOAD_WRITE_BUFFER_BYTES = 512 * 1024
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
    downloads = DownloadManager(resolved_settings, manager, resolved_processor)
    telegram = TelegramService(resolved_settings, manager, downloads, resolved_processor)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.jobs = manager
        app.state.authenticator = authenticator
        app.state.downloads = downloads
        app.state.telegram = telegram
        await manager.start()
        try:
            if resolved_settings.metube_sidecar_url or downloads._registry.exists():
                await downloads.start()
            if resolved_settings.telegram_bot_token:
                await telegram.start()
            manager.event_sink = telegram.notify
            downloads.event_sink = telegram.notify
            yield
        finally:
            await telegram.stop()
            await downloads.stop()
            await manager.stop()

    app = FastAPI(
        title="Media Manager API",
        version=__version__,
        description="Bounded media conversion for trusted automations.",
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

    @app.exception_handler(ProcessingError)
    async def inspection_error_handler(_request: Request, exc: ProcessingError) -> JSONResponse:
        body = ErrorResponse(error=ErrorBody(code=exc.code, message=exc.message))
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
    async def capabilities(principal: PrincipalDependency) -> Capabilities:
        return Capabilities(
            targets=target_capabilities(),
            qualities=[
                CapabilityOption(value=Quality.ECONOMY, label="Smaller file"),
                CapabilityOption(value=Quality.BALANCED, label="Balanced"),
                CapabilityOption(value=Quality.HIGH, label="Higher quality"),
            ],
            quality_scale=QualityScale(),
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
            result_ttl_seconds=manager.retention_seconds(principal.id),
            compression_target_bytes=COMPRESSION_TARGET_BYTES,
            downloads_enabled=await downloads.available(),
        )

    @app.get(
        "/v1/history/settings",
        response_model=HistorySettings,
        responses={401: {"model": ErrorResponse}},
    )
    async def get_history_settings(principal: PrincipalDependency) -> HistorySettings:
        return HistorySettings(
            retention_days=await manager.get_retention_days(principal.id),
        )

    @app.put(
        "/v1/history/settings",
        response_model=HistorySettings,
        responses={
            401: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            507: {"model": ErrorResponse},
        },
    )
    async def put_history_settings(
        settings_body: HistorySettings,
        principal: PrincipalDependency,
    ) -> HistorySettings:
        retention_days = await manager.set_retention_days(
            principal.id,
            settings_body.retention_days,
        )
        return HistorySettings(retention_days=retention_days)

    @app.get(
        "/v1/history",
        responses={401: {"model": ErrorResponse}},
    )
    async def get_history(
        principal: PrincipalDependency,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        # Merge before pagination so remote downloads and local processing share a timeline.
        async def collect(source):
            result = []
            total = 0
            for position in range(0, offset + limit, 100):
                page, total = await source(
                    principal.id, min(100, offset + limit - position), position
                )
                result.extend(page)
                if len(result) >= total:
                    break
            return result, total

        items, local_total = await collect(manager.history)
        remote, remote_total = await collect(downloads.history)
        combined = [item.model_dump(mode="json") for item in items] + remote
        combined.sort(key=lambda entry: entry["job"]["created_at"], reverse=True)
        return {"items": combined[offset:offset + limit], "total": local_total + remote_total}

    @app.post("/v1/downloads", status_code=202)
    async def create_download(
        request: Request, body: DownloadRequest, principal: PrincipalDependency
    ):
        _validate_processing_options(body.conversion_options, body.compression_options)
        return await downloads.create(body, principal.id, origin=_request_origin(request))

    @app.get("/v1/downloads/{download_id}")
    async def get_download(download_id: str, principal: PrincipalDependency):
        return await downloads.get(download_id, principal.id)

    @app.get("/v1/downloads/{download_id}/content")
    async def download_remote(
        download_id: str, principal: PrincipalDependency, preview: bool = False
    ) -> FileResponse:
        path, filename, media_type = await downloads.download(download_id, principal.id)
        if not preview:
            view = await downloads.get(download_id, principal.id)
            await telegram.notify(
                "download", download_id, resolved_settings.owner_id(principal.id),
                filename, path.stat().st_size, view.expires_at,
            )
        return FileResponse(path, filename=filename, media_type=media_type)

    @app.delete("/v1/downloads/{download_id}", status_code=204)
    async def delete_download(download_id: str, principal: PrincipalDependency) -> Response:
        await downloads.delete(download_id, principal.id)
        return Response(status_code=204)

    @app.post("/v1/downloads/{download_id}/process", status_code=201)
    async def process_download(
        download_id: str, body: ProcessingRequest, principal: PrincipalDependency
    ):
        _validate_processing_options(body.conversion_options, body.compression_options)
        return await downloads.process(
            download_id, principal.id,
            conversion_options=body.conversion_options,
            compression_options=body.compression_options,
            auto_start=body.auto_start,
        )

    @app.get("/v1/jobs/{job_id}/inspect", response_model=MediaMetadata)
    @app.get("/v1/compressions/{job_id}/inspect", response_model=MediaMetadata)
    async def inspect_job(job_id: str, principal: PrincipalDependency) -> MediaMetadata:
        return await manager.inspect(job_id, principal.id)

    @app.post("/v1/jobs/{job_id}/estimate", response_model=EstimateView)
    @app.post("/v1/compressions/{job_id}/estimate", response_model=EstimateView)
    async def estimate_job(
        job_id: str, body: ProcessingRequest, principal: PrincipalDependency
    ) -> EstimateView:
        _validate_processing_options(body.conversion_options, body.compression_options)
        view = await manager.get(job_id, principal.id)
        conversion = body.conversion_options
        compression = body.compression_options
        if conversion is None and compression is None:
            if isinstance(view, CompressionJobView):
                compression = view.options or CompressionOptions()
            else:
                conversion = ConversionOptions(
                    target=view.target, quality_percent=view.quality_percent,
                    resolution=view.resolution, audio=view.audio,
                )
        return await manager.estimate(job_id, principal.id, conversion, compression)

    @app.get("/v1/jobs/{job_id}/estimate", response_model=EstimateView)
    @app.get("/v1/compressions/{job_id}/estimate", response_model=EstimateView)
    async def get_estimate(job_id: str, principal: PrincipalDependency) -> EstimateView:
        return await manager.latest_estimate(job_id, principal.id)

    @app.post("/v1/jobs/{job_id}/start", status_code=202)
    @app.post("/v1/compressions/{job_id}/start", status_code=202)
    async def start_prepared(
        job_id: str, body: ProcessingRequest, principal: PrincipalDependency
    ):
        _validate_processing_options(body.conversion_options, body.compression_options)
        view = await manager.get(job_id, principal.id)
        compression = body.compression_options
        if compression is None and body.conversion_options is None and isinstance(
            view, CompressionJobView
        ):
            compression = view.options or CompressionOptions()
        return await manager.start_prepared(
            job_id, principal.id, body.conversion_options, compression
        )

    @app.post("/v1/uploads/{job_id}/prepare", status_code=201)
    async def prepare_upload(job_id: str, principal: PrincipalDependency):
        upload = await manager.upload_view(job_id, principal.id)
        return await manager.prepare(job_id, upload.offset)

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
        quality: QualityQuery = None,
        quality_percent: QualityPercentQuery = None,
        resolution: ResolutionQuery = Resolution.SOURCE,
        audio: AudioQuery = AudioMode.KEEP,
    ) -> UploadView:
        _validate_options(target, resolution, audio)
        upload_length = _required_header_int(request, "Upload-Length")
        if upload_length == 0:
            raise ApiError(422, "EMPTY_INPUT", "Upload body is empty")
        if upload_length > resolved_settings.max_upload_bytes:
            raise ApiError(413, "INPUT_TOO_LARGE", "Upload exceeds the size limit")

        resolved_quality, resolved_percent = _resolve_quality(quality, quality_percent)
        options = ConversionOptions(
            target=target,
            quality=resolved_quality,
            quality_percent=resolved_percent,
            resolution=resolution,
            audio=audio,
        )
        job = await manager.reserve(
            principal.id,
            options,
            upload_length,
            _source_filename(request),
        )
        job.origin = _request_origin(request)
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
                            pending = bytearray()
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
                                if len(chunk) >= UPLOAD_WRITE_BUFFER_BYTES:
                                    if pending:
                                        await destination.write(pending)
                                        pending.clear()
                                    await destination.write(chunk)
                                else:
                                    pending.extend(chunk)
                                    if len(pending) >= UPLOAD_WRITE_BUFFER_BYTES:
                                        await destination.write(pending)
                                        pending.clear()
                            if pending:
                                await destination.write(pending)
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
        quality: QualityQuery = None,
        quality_percent: QualityPercentQuery = None,
        resolution: ResolutionQuery = Resolution.SOURCE,
        audio: AudioQuery = AudioMode.KEEP,
        prepare_only: bool = False,
    ) -> JobView:
        _validate_options(target, resolution, audio)
        content_length = _validate_upload_headers(
            request,
            resolved_settings.max_upload_bytes,
        )
        resolved_quality, resolved_percent = _resolve_quality(quality, quality_percent)
        options = ConversionOptions(
            target=target,
            quality=resolved_quality,
            quality_percent=resolved_percent,
            resolution=resolution,
            audio=audio,
        )
        job = await manager.reserve(
            principal.id,
            options,
            content_length,
            _source_filename(request),
        )
        job.origin = _request_origin(request)
        received = 0

        try:
            try:
                async with asyncio.timeout(resolved_settings.upload_timeout_seconds):
                    async with aiofiles.open(job.input_path, "xb") as destination:
                        pending = bytearray()
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
                            if len(chunk) >= UPLOAD_WRITE_BUFFER_BYTES:
                                if pending:
                                    await destination.write(pending)
                                    pending.clear()
                                await destination.write(chunk)
                            else:
                                pending.extend(chunk)
                                if len(pending) >= UPLOAD_WRITE_BUFFER_BYTES:
                                    await destination.write(pending)
                                    pending.clear()
                        if pending:
                            await destination.write(pending)
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
            if prepare_only:
                return await manager.prepare(job.id, received)
            return await manager.enqueue(job.id, received)
        except BaseException:
            await manager.discard(job.id)
            raise

    @app.post(
        "/v1/compressions",
        response_model=CompressionJobView,
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
    async def create_compression(
        request: Request,
        principal: PrincipalDependency,
        prepare_only: bool = False,
    ) -> CompressionJobView:
        if set(request.query_params) - {"prepare_only"}:
            raise ApiError(422, "INVALID_OPTIONS", "Compression does not accept options")
        content_length = _validate_upload_headers(request, resolved_settings.max_upload_bytes)
        job = await manager.reserve_compression(
            principal.id,
            content_length,
            _source_filename(request),
        )
        job.origin = _request_origin(request)
        received = 0
        try:
            try:
                async with asyncio.timeout(resolved_settings.upload_timeout_seconds):
                    async with aiofiles.open(job.input_path, "xb") as destination:
                        pending = bytearray()
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
                            if len(chunk) >= UPLOAD_WRITE_BUFFER_BYTES:
                                if pending:
                                    await destination.write(pending)
                                    pending.clear()
                                await destination.write(chunk)
                            else:
                                pending.extend(chunk)
                                if len(pending) >= UPLOAD_WRITE_BUFFER_BYTES:
                                    await destination.write(pending)
                                    pending.clear()
                        if pending:
                            await destination.write(pending)
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
            view = (
                await manager.prepare(job.id, received) if prepare_only
                else await manager.enqueue(job.id, received)
            )
            assert isinstance(view, CompressionJobView)
            return view
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
        view = await manager.get(job_id, principal.id)
        if not isinstance(view, JobView):
            raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
        return view

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
        preview: bool = False,
    ) -> FileResponse:
        path, filename, media_type = await manager.download(job_id, principal.id, False)
        if not preview:
            await manager.notify_download(job_id, principal.id)
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
        await manager.delete(job_id, principal.id, False)
        return Response(status_code=204)

    @app.get(
        "/v1/compressions/{job_id}",
        response_model=CompressionJobView,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def get_compression(
        job_id: str,
        principal: PrincipalDependency,
    ) -> CompressionJobView:
        view = await manager.get(job_id, principal.id)
        if not isinstance(view, CompressionJobView):
            raise ApiError(404, "JOB_NOT_FOUND", "Job was not found")
        return view

    @app.get(
        "/v1/compressions/{job_id}/content",
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            410: {"model": ErrorResponse},
        },
    )
    async def download_compression(
        job_id: str,
        principal: PrincipalDependency,
        preview: bool = False,
    ) -> FileResponse:
        path, filename, media_type = await manager.download(job_id, principal.id, True)
        if not preview:
            await manager.notify_download(job_id, principal.id)
        return FileResponse(path=path, filename=filename, media_type=media_type)

    @app.delete(
        "/v1/compressions/{job_id}",
        status_code=204,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def delete_compression(
        job_id: str,
        principal: PrincipalDependency,
    ) -> Response:
        await manager.delete(job_id, principal.id, True)
        return Response(status_code=204)

    return app


def _validate_processing_options(
    conversion: ConversionOptions | None, compression: CompressionOptions | None
) -> None:
    if conversion is not None:
        _validate_options(
            conversion.target,
            compression.resolution if compression else conversion.resolution,
            compression.audio if compression else conversion.audio,
        )
    elif compression is not None and compression.target is not None:
        _validate_options(compression.target, compression.resolution, compression.audio)


def _validate_options(target: Target, resolution: Resolution, audio: AudioMode) -> None:
    audio_targets = {Target.AUDIO_M4A, Target.AUDIO_MP3, Target.AUDIO_OPUS}
    video_targets = {Target.VIDEO_MP4, Target.VIDEO_WEBM}
    if target in audio_targets and resolution is not Resolution.SOURCE:
        raise ApiError(422, "INVALID_OPTIONS", "Resolution does not apply to audio output")
    if target not in video_targets and audio is AudioMode.DROP:
        raise ApiError(422, "INVALID_OPTIONS", "Audio removal applies only to video output")
    if target is Target.ANIMATION_GIF and resolution in {Resolution.P1440, Resolution.P2160}:
        raise ApiError(422, "INVALID_OPTIONS", "GIF output is limited to 1080p")


def _resolve_quality(
    quality: Quality | None,
    quality_percent: int | None,
) -> tuple[Quality, int]:
    if quality is not None and quality_percent is not None:
        raise ApiError(
            422,
            "INVALID_OPTIONS",
            "Use quality_percent or the legacy quality preset, not both",
        )
    if quality_percent is not None:
        if quality_percent < 25:
            bucket = Quality.ECONOMY
        elif quality_percent < 75:
            bucket = Quality.BALANCED
        else:
            bucket = Quality.HIGH
        return bucket, quality_percent
    legacy = quality or Quality.BALANCED
    return legacy, {
        Quality.ECONOMY: 0,
        Quality.BALANCED: 50,
        Quality.HIGH: 100,
    }[legacy]


def _source_filename(request: Request) -> str | None:
    encoded = request.headers.get("Upload-Filename")
    if not encoded:
        return None
    if len(encoded) > 768:
        raise ApiError(422, "INVALID_FILENAME", "Upload filename is too long")
    return unquote(encoded, errors="replace")


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


def _request_origin(request: Request) -> str:
    value = request.headers.get("X-Media-Source", "api")
    return value if value in {"web", "shortcut", "api"} else "api"


def _is_cross_site(request: Request, allowed_origin: str) -> bool:
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("Origin")
    if origin:
        return _origin(origin) != allowed_origin
    return "Sec-Fetch-Mode" in request.headers
