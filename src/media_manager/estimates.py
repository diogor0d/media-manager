from __future__ import annotations

import asyncio
import hashlib
import json
import math
import tempfile
import time
from pathlib import Path
from weakref import WeakKeyDictionary

from media_manager.config import Settings
from media_manager.errors import ProcessingError
from media_manager.models import (
    CompressionOptions,
    ConversionOptions,
    EstimateView,
    MediaClass,
    MediaMetadata,
    Resolution,
    Target,
)
from media_manager.processor import (
    TARGET_SPECS,
    ConversionCancelled,
    MediaProcessor,
    ProbeInfo,
    _CommandTimeout,
)

SAMPLE_COUNT = 3
SAMPLE_SECONDS = 2.0
SAMPLE_BUDGET_SECONDS = SAMPLE_COUNT * SAMPLE_SECONDS
ESTIMATE_DEADLINE_SECONDS = 20.0
_SAMPLE_SEMAPHORES: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)


def _sample_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _SAMPLE_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(1)
        _SAMPLE_SEMAPHORES[loop] = semaphore
    return semaphore


class EstimateService:
    def __init__(self, settings: Settings, processor: MediaProcessor) -> None:
        self._settings = settings
        self._processor = processor

    async def fast(
        self,
        path: Path,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
    ) -> EstimateView:
        source = await self._processor.inspect(path)
        return self._fast_view(source, conversion, compression, status="pending")

    async def refine(
        self,
        path: Path,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
        cancel_event: asyncio.Event,
    ) -> EstimateView:
        deadline = time.monotonic() + ESTIMATE_DEADLINE_SECONDS
        remaining = deadline - time.monotonic()
        async with asyncio.timeout(remaining):
            source_metadata = await self._processor.inspect(path, cancel_event=cancel_event)
        fast = self._fast_view(source_metadata, conversion, compression, status="pending")
        try:
            if cancel_event.is_set():
                raise ConversionCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return fast.model_copy(update={"status": "fallback"})
            async with asyncio.timeout(remaining):
                async with _sample_semaphore():
                    if cancel_event.is_set():
                        raise ConversionCancelled
                    source = await self._processor._probe(path, cancel_event=cancel_event)
                    if (
                        source.media.media_class is not MediaClass.IMAGE
                        and not source.media.duration_ms
                    ):
                        return fast.model_copy(update={"status": "fallback"})
                    options = self._sample_options(source, conversion, compression)
                    compression_attempts = (
                        self._processor._advanced_compression_attempts(
                            source,
                            self._effective_compression(conversion, compression),
                            options.target,
                        )
                        if compression is not None
                        else ()
                    )
                    if compression_attempts:
                        options = compression_attempts[0]
                    output_bytes, elapsed_seconds, sampled_seconds = await self._sample(
                        path, source, options, cancel_event, deadline
                    )
            if not sampled_seconds or not output_bytes:
                return fast.model_copy(update={"status": "fallback"})

            media_duration = (source.media.duration_ms or 0) / 1000
            if media_duration > 0 and source.media.media_class is not MediaClass.IMAGE:
                projected = output_bytes / sampled_seconds * media_duration
                # Header/container overhead and the small sample make this an
                # interval, not a claimed exact result.
                low = max(1, math.floor(projected * 0.7))
                high = max(low, math.ceil(projected * 1.4))
                eta = elapsed_seconds / sampled_seconds * media_duration
                eta_low = max(0, math.floor(eta * 0.65))
                eta_high = max(1, eta_low, math.ceil(eta * 1.8))
            else:
                low = max(1, math.floor(output_bytes * 0.7))
                high = max(low, math.ceil(output_bytes * 1.4))
                eta_low = max(0, math.floor(elapsed_seconds * 0.65))
                eta_high = max(1, eta_low, math.ceil(elapsed_seconds * 1.8))

            if compression is not None:
                effective_compression = self._effective_compression(
                    conversion, compression
                )
                # The sample measures the first real encoder candidate. Later
                # candidates may reduce size, but a difficult source can still
                # miss the requested ceiling, so never clamp the upper size.
                low = min(low, max(1, math.floor(effective_compression.target_bytes * 0.75)))
                high = max(
                    high,
                    math.ceil(effective_compression.target_bytes * 1.25),
                )
                attempt_count = max(1, len(compression_attempts))
                eta_high = max(eta_high, math.ceil(eta_high * attempt_count))

            return EstimateView(
                method="sampled",
                size_min_bytes=low,
                size_max_bytes=high,
                eta_min_seconds=eta_low,
                eta_max_seconds=eta_high,
                confidence="medium",
                status="ready",
                source=source.media,
                options_key=fast.options_key,
            )
        except ConversionCancelled:
            raise
        except (ProcessingError, OSError, _CommandTimeout, TimeoutError, ValueError):
            return fast.model_copy(update={"status": "fallback"})

    def _fast_view(
        self,
        source: MediaMetadata,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
        *,
        status: str,
    ) -> EstimateView:
        factor = self._size_factor(source, conversion, compression)
        low = max(1, math.floor(source.bytes * factor[0]))
        high = max(low, math.ceil(source.bytes * factor[1]))
        if compression is not None:
            effective = self._effective_compression(conversion, compression)
            # The chosen target guides the search but is not a guaranteed cap.
            low = min(low, max(1, math.floor(effective.target_bytes * 0.75)))
            high = max(high, math.ceil(effective.target_bytes * 1.25))
        return EstimateView(
            method="fast",
            size_min_bytes=low,
            size_max_bytes=high,
            eta_min_seconds=None,
            eta_max_seconds=None,
            confidence="low",
            status=status,
            source=source,
            options_key=self._options_key(conversion, compression),
        )

    @staticmethod
    def _size_factor(
        source: MediaMetadata,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
    ) -> tuple[float, float]:
        target = (
            conversion.target
            if conversion
            else compression.target
            if compression and compression.target
            else None
        )
        if target in {Target.IMAGE_JPEG, Target.IMAGE_WEBP}:
            factors = (0.1, 1.25)
        elif target is Target.IMAGE_PNG:
            factors = (0.25, 1.6)
        elif target in {Target.AUDIO_M4A, Target.AUDIO_MP3, Target.AUDIO_OPUS}:
            factors = (0.08, 1.1)
        elif target in {Target.VIDEO_MP4, Target.VIDEO_WEBM, Target.ANIMATION_GIF}:
            factors = (0.05, 1.5)
        else:
            factors = {
                MediaClass.IMAGE: (0.1, 1.6),
                MediaClass.AUDIO: (0.08, 1.2),
                MediaClass.ANIMATION: (0.05, 2.0),
                MediaClass.VIDEO: (0.05, 1.5),
            }[source.media_class]
        if compression is None:
            return factors

        effective = EstimateService._effective_compression(conversion, compression)
        quality_scale = 0.65 + effective.quality_percent / 100
        resolution_scale = 1.0
        if source.width and source.height and effective.resolution is not Resolution.SOURCE:
            edge = {
                Resolution.P480: 854,
                Resolution.P720: 1280,
                Resolution.P1080: 1920,
                Resolution.P1440: 2560,
                Resolution.P2160: 3840,
            }[effective.resolution]
            source_edge = max(source.width, source.height)
            resolution_scale = min(1.0, (edge / source_edge) ** 2)
        scale = quality_scale * resolution_scale
        return (max(0.01, factors[0] * scale), max(factors[1], factors[1] * scale))

    @staticmethod
    def _options_key(
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
    ) -> str:
        payload = {
            "conversion": conversion.model_dump(mode="json") if conversion else None,
            "compression": compression.model_dump(mode="json") if compression else None,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:24]

    @staticmethod
    def _conversion_options(
        source: ProbeInfo,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
    ) -> ConversionOptions:
        if conversion is not None:
            return conversion
        default_target = {
            MediaClass.VIDEO: Target.VIDEO_MP4,
            MediaClass.ANIMATION: Target.VIDEO_MP4,
            MediaClass.IMAGE: Target.IMAGE_WEBP,
            MediaClass.AUDIO: Target.AUDIO_M4A,
        }[source.media.media_class]
        if compression is None:
            return ConversionOptions(target=default_target)
        return ConversionOptions(
            target=compression.target or default_target,
            quality_percent=compression.quality_percent,
            resolution=compression.resolution,
            audio=compression.audio,
        )

    @staticmethod
    def _effective_compression(
        conversion: ConversionOptions | None,
        compression: CompressionOptions,
    ) -> CompressionOptions:
        if conversion is None:
            return compression
        return compression.model_copy(update={
            "target": conversion.target,
        })

    @classmethod
    def _sample_options(
        cls,
        source: ProbeInfo,
        conversion: ConversionOptions | None,
        compression: CompressionOptions | None,
    ) -> ConversionOptions:
        if compression is None:
            return cls._conversion_options(source, conversion, None)
        effective = cls._effective_compression(conversion, compression)
        default_target = {
            MediaClass.VIDEO: Target.VIDEO_MP4,
            MediaClass.ANIMATION: Target.VIDEO_MP4,
            MediaClass.IMAGE: Target.IMAGE_WEBP,
            MediaClass.AUDIO: Target.AUDIO_M4A,
        }[source.media.media_class]
        return ConversionOptions(
            target=effective.target or default_target,
            quality_percent=effective.quality_percent,
            resolution=effective.resolution,
            audio=effective.audio,
        )

    async def _sample(
        self,
        path: Path,
        source: ProbeInfo,
        options: ConversionOptions,
        cancel_event: asyncio.Event,
        deadline: float,
    ) -> tuple[int, float, float]:
        duration = (source.media.duration_ms or 0) / 1000
        is_still = source.media.media_class is MediaClass.IMAGE or duration <= 0
        if is_still:
            starts = [None]
            span = None
        else:
            span = min(SAMPLE_SECONDS, duration)
            starts = [
                max(0.0, (duration - span) * position)
                for position in (0.0, 0.5, 1.0)
            ]
            starts = list(dict.fromkeys(starts))

        total_bytes = 0
        elapsed_total = 0.0
        sampled_total = 0.0
        with tempfile.TemporaryDirectory(
            prefix="media-estimate-", dir=self._settings.work_dir
        ) as temporary:
            root = Path(temporary)
            for index, start in enumerate(starts[:SAMPLE_COUNT]):
                if cancel_event.is_set():
                    raise ConversionCancelled
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Estimate deadline reached")
                sample_span = (
                    None
                    if start is None
                    else min(span or SAMPLE_SECONDS, duration - start)
                )
                output = root / f"sample-{index}.{TARGET_SPECS[options.target].extension}"
                command = self._processor._build_command(
                    path,
                    output,
                    source,
                    options,
                    sample_start_seconds=start,
                    sample_duration_seconds=sample_span,
                )
                started = time.monotonic()
                result = await self._processor._run_command(
                    command,
                    timeout=max(1, min(8, math.ceil(remaining))),
                    cancel_event=cancel_event,
                    stdout_limit=8 * 1024,
                    stderr_limit=8 * 1024,
                )
                elapsed_total += time.monotonic() - started
                if result.returncode != 0 or not output.is_file():
                    raise ProcessingError("ESTIMATE_FAILED", "A bounded sample failed")
                size = output.stat().st_size
                if size <= 0:
                    raise ProcessingError("ESTIMATE_FAILED", "A bounded sample was empty")
                total_bytes += size
                sampled_total += sample_span if sample_span is not None else 1.0
                output.unlink(missing_ok=True)
        return total_bytes, elapsed_total, sampled_total
