from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_manager.config import Settings
from media_manager.errors import ProcessingError
from media_manager.models import (
    AudioMode,
    ConversionOptions,
    MediaClass,
    MediaMetadata,
    Quality,
    Resolution,
    Target,
    TargetCapability,
)

INPUT_FORMAT_WHITELIST = ",".join(
    (
        "aac",
        "aiff",
        "apng",
        "asf",
        "avi",
        "bmp_pipe",
        "flac",
        "flv",
        "gif",
        "h264",
        "hevc",
        "image2",
        "image2pipe",
        "ivf",
        "jpeg_pipe",
        "m4v",
        "matroska",
        "mjpeg",
        "mov",
        "mp3",
        "mpeg",
        "mpegts",
        "ogg",
        "png_pipe",
        "tiff_pipe",
        "wav",
        "webm",
        "webp_pipe",
    )
)

STILL_FORMATS = frozenset(
    {
        "apng",
        "bmp_pipe",
        "image2",
        "image2pipe",
        "jpeg_pipe",
        "png_pipe",
        "tiff_pipe",
        "webp_pipe",
    }
)
ANIMATION_FORMATS = frozenset({"gif"})
RESOLUTION_MAX_EDGE = {
    Resolution.P480: 854,
    Resolution.P720: 1280,
    Resolution.P1080: 1920,
    Resolution.P1440: 2560,
    Resolution.P2160: 3840,
}


@dataclass(frozen=True, slots=True)
class TargetSpec:
    label: str
    media_type: str
    extension: str
    accepts: frozenset[MediaClass]
    formats: frozenset[str]
    video_codec: str | None = None
    audio_codec: str | None = None
    timeout_seconds: int = 300


TARGET_SPECS = {
    Target.VIDEO_MP4: TargetSpec(
        label="MP4 (H.264)",
        media_type="video/mp4",
        extension="mp4",
        accepts=frozenset({MediaClass.VIDEO, MediaClass.ANIMATION}),
        formats=frozenset({"3g2", "3gp", "m4a", "mj2", "mov", "mp4"}),
        video_codec="h264",
        timeout_seconds=600,
    ),
    Target.VIDEO_WEBM: TargetSpec(
        label="WebM (VP9)",
        media_type="video/webm",
        extension="webm",
        accepts=frozenset({MediaClass.VIDEO, MediaClass.ANIMATION}),
        formats=frozenset({"matroska", "webm"}),
        video_codec="vp9",
        timeout_seconds=600,
    ),
    Target.IMAGE_JPEG: TargetSpec(
        label="JPEG",
        media_type="image/jpeg",
        extension="jpg",
        accepts=frozenset({MediaClass.IMAGE}),
        formats=frozenset({"image2", "jpeg_pipe"}),
        video_codec="mjpeg",
        timeout_seconds=60,
    ),
    Target.IMAGE_PNG: TargetSpec(
        label="PNG",
        media_type="image/png",
        extension="png",
        accepts=frozenset({MediaClass.IMAGE}),
        formats=frozenset({"image2", "png_pipe"}),
        video_codec="png",
        timeout_seconds=60,
    ),
    Target.IMAGE_WEBP: TargetSpec(
        label="WebP",
        media_type="image/webp",
        extension="webp",
        accepts=frozenset({MediaClass.IMAGE}),
        formats=frozenset({"image2", "webp_pipe"}),
        video_codec="webp",
        timeout_seconds=60,
    ),
    Target.ANIMATION_GIF: TargetSpec(
        label="Animated GIF",
        media_type="image/gif",
        extension="gif",
        accepts=frozenset({MediaClass.VIDEO, MediaClass.IMAGE, MediaClass.ANIMATION}),
        formats=frozenset({"gif"}),
        video_codec="gif",
        timeout_seconds=120,
    ),
    Target.AUDIO_M4A: TargetSpec(
        label="M4A (AAC)",
        media_type="audio/mp4",
        extension="m4a",
        accepts=frozenset({MediaClass.AUDIO, MediaClass.VIDEO}),
        formats=frozenset({"3g2", "3gp", "m4a", "mj2", "mov", "mp4"}),
        audio_codec="aac",
        timeout_seconds=300,
    ),
    Target.AUDIO_MP3: TargetSpec(
        label="MP3",
        media_type="audio/mpeg",
        extension="mp3",
        accepts=frozenset({MediaClass.AUDIO, MediaClass.VIDEO}),
        formats=frozenset({"mp3"}),
        audio_codec="mp3",
        timeout_seconds=300,
    ),
    Target.AUDIO_OPUS: TargetSpec(
        label="Ogg Opus",
        media_type="audio/ogg",
        extension="opus",
        accepts=frozenset({MediaClass.AUDIO, MediaClass.VIDEO}),
        formats=frozenset({"ogg"}),
        audio_codec="opus",
        timeout_seconds=300,
    ),
}


@dataclass(frozen=True, slots=True)
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    frame_rate: float | None = None
    channels: int | None = None
    sample_rate: int | None = None
    attached_picture: bool = False


@dataclass(frozen=True, slots=True)
class ProbeInfo:
    media: MediaMetadata
    streams: tuple[StreamInfo, ...]
    video: StreamInfo | None
    audio: StreamInfo | None
    format_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class ConversionResult:
    input: MediaMetadata
    output_path: Path
    output_bytes: int
    filename: str
    media_type: str
    width: int | None
    height: int | None
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _CommandTimeout(Exception):
    pass


class ConversionCancelled(Exception):
    pass


def target_capabilities() -> list[TargetCapability]:
    return [
        TargetCapability(
            value=target,
            label=spec.label,
            media_type=spec.media_type,
            extension=spec.extension,
            accepts=sorted(spec.accepts, key=lambda media_class: media_class.value),
        )
        for target, spec in TARGET_SPECS.items()
    ]


class MediaProcessor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def verify(self) -> None:
        try:
            version = await self._run_command(
                [self._settings.ffprobe_path, "-version"],
                timeout=10,
                stdout_limit=64 * 1024,
            )
            encoders = await self._run_command(
                [self._settings.ffmpeg_path, "-hide_banner", "-encoders"],
                timeout=10,
                stdout_limit=512 * 1024,
            )
            filters = await self._run_command(
                [self._settings.ffmpeg_path, "-hide_banner", "-filters"],
                timeout=10,
                stdout_limit=512 * 1024,
            )
        except (OSError, _CommandTimeout) as exc:
            raise RuntimeError("Required FFmpeg tools are unavailable") from exc

        if version.returncode != 0 or encoders.returncode != 0 or filters.returncode != 0:
            raise RuntimeError("Required FFmpeg capability checks failed")

        encoder_text = encoders.stdout.decode("utf-8", errors="replace")
        filter_text = filters.stdout.decode("utf-8", errors="replace")
        required_encoders = {
            "aac",
            "gif",
            "libmp3lame",
            "libopus",
            "libvpx-vp9",
            "libwebp",
            "libx264",
            "mjpeg",
            "png",
        }
        required_filters = {"format", "palettegen", "paletteuse", "scale", "split"}
        missing_encoders = sorted(
            name
            for name in required_encoders
            if not re.search(rf"\b{re.escape(name)}\b", encoder_text)
        )
        missing_filters = sorted(
            name
            for name in required_filters
            if not re.search(rf"\b{re.escape(name)}\b", filter_text)
        )
        if missing_encoders or missing_filters:
            missing = ", ".join((*missing_encoders, *missing_filters))
            raise RuntimeError(f"FFmpeg build is missing required capabilities: {missing}")

    async def convert(
        self,
        input_path: Path,
        job_dir: Path,
        options: ConversionOptions,
        cancel_event: asyncio.Event,
    ) -> ConversionResult:
        source = await self._probe(input_path, cancel_event=cancel_event)
        self._validate_input(source, options)

        spec = TARGET_SPECS[options.target]
        partial_path = job_dir / f"result.part.{spec.extension}"
        final_path = job_dir / f"result.{spec.extension}"
        command = self._build_command(input_path, partial_path, source, options)

        try:
            result = await self._run_command(
                command,
                timeout=spec.timeout_seconds,
                cancel_event=cancel_event,
                stdout_limit=64 * 1024,
                stderr_limit=64 * 1024,
            )
        except _CommandTimeout as exc:
            raise ProcessingError(
                "PROCESSING_TIMEOUT", "Conversion exceeded its time limit"
            ) from exc
        except ConversionCancelled:
            raise

        if result.returncode != 0 or not partial_path.is_file():
            raise ProcessingError("CONVERSION_FAILED", "The media could not be converted")

        output_bytes = partial_path.stat().st_size
        if output_bytes <= 0:
            raise ProcessingError("CONVERSION_FAILED", "The converter produced an empty file")
        if output_bytes > self._settings.max_output_bytes:
            raise ProcessingError("OUTPUT_TOO_LARGE", "Converted media exceeds the output limit")

        output_probe = await self._probe(partial_path, cancel_event=cancel_event)
        self._validate_output(output_probe, options.target)
        partial_path.replace(final_path)

        return ConversionResult(
            input=source.media,
            output_path=final_path,
            output_bytes=output_bytes,
            filename=f"converted.{spec.extension}",
            media_type=spec.media_type,
            width=output_probe.media.width,
            height=output_probe.media.height,
            duration_ms=output_probe.media.duration_ms,
        )

    async def _probe(
        self,
        path: Path,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> ProbeInfo:
        command = [
            self._settings.ffprobe_path,
            "-v",
            "error",
            "-probesize",
            "33554432",
            "-analyzeduration",
            "10000000",
            "-max_probe_packets",
            "2500",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            INPUT_FORMAT_WHITELIST,
            "-show_entries",
            (
                "format=format_name,duration:"
                "stream=index,codec_type,codec_name,width,height,duration,avg_frame_rate,"
                "r_frame_rate,channels,sample_rate:stream_disposition=attached_pic"
            ),
            "-of",
            "json",
            os.fspath(path),
        ]
        try:
            result = await self._run_command(
                command,
                timeout=self._settings.probe_timeout_seconds,
                cancel_event=cancel_event,
                stdout_limit=256 * 1024,
                stderr_limit=64 * 1024,
            )
        except _CommandTimeout as exc:
            raise ProcessingError("MEDIA_PROBE_TIMEOUT", "Media inspection timed out") from exc

        if result.returncode != 0:
            raise ProcessingError("UNSUPPORTED_MEDIA", "Media type is unsupported or malformed")

        try:
            payload = json.loads(result.stdout)
            return self._parse_probe(payload, path.stat().st_size)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProcessingError("UNSUPPORTED_MEDIA", "Media metadata is invalid") from exc

    def _parse_probe(self, payload: dict[str, Any], size_bytes: int) -> ProbeInfo:
        raw_streams = payload.get("streams")
        raw_format = payload.get("format")
        if not isinstance(raw_streams, list) or not isinstance(raw_format, dict):
            raise ValueError("missing probe fields")
        if not raw_streams or len(raw_streams) > self._settings.max_streams:
            raise ProcessingError("MEDIA_LIMIT_EXCEEDED", "Media has an invalid stream count")

        streams: list[StreamInfo] = []
        for item in raw_streams:
            if not isinstance(item, dict):
                raise ValueError("invalid stream")
            codec_type = str(item.get("codec_type", ""))
            codec_name = str(item.get("codec_name", ""))
            disposition = item.get("disposition") or {}
            stream = StreamInfo(
                index=int(item["index"]),
                codec_type=codec_type,
                codec_name=codec_name,
                width=self._optional_int(item.get("width")),
                height=self._optional_int(item.get("height")),
                duration=self._optional_float(item.get("duration")),
                frame_rate=max(
                    self._fraction(item.get("avg_frame_rate")),
                    self._fraction(item.get("r_frame_rate")),
                ),
                channels=self._optional_int(item.get("channels")),
                sample_rate=self._optional_int(item.get("sample_rate")),
                attached_picture=bool(disposition.get("attached_pic", 0)),
            )
            streams.append(stream)

        video = next(
            (
                stream
                for stream in streams
                if stream.codec_type == "video" and not stream.attached_picture
            ),
            None,
        )
        audio = next((stream for stream in streams if stream.codec_type == "audio"), None)
        if video is None and audio is None:
            raise ProcessingError("UNSUPPORTED_MEDIA", "Media has no usable audio or visual stream")

        raw_format_names = str(raw_format.get("format_name", ""))
        format_names = frozenset(
            name.strip() for name in raw_format_names.split(",") if name.strip()
        )
        if not format_names:
            raise ValueError("missing format")

        duration = self._optional_float(raw_format.get("duration"))
        if duration is None:
            durations = [stream.duration for stream in streams if stream.duration is not None]
            duration = max(durations, default=None)

        if video is None:
            media_class = MediaClass.AUDIO
        elif format_names & ANIMATION_FORMATS:
            media_class = MediaClass.ANIMATION
        elif format_names & STILL_FORMATS:
            media_class = MediaClass.IMAGE
        else:
            media_class = MediaClass.VIDEO

        duration_ms = round(duration * 1000) if duration is not None and duration >= 0 else None
        media = MediaMetadata(
            bytes=size_bytes,
            media_class=media_class,
            container=raw_format_names,
            duration_ms=duration_ms,
            width=video.width if video else None,
            height=video.height if video else None,
            video_codec=video.codec_name if video else None,
            audio_codec=audio.codec_name if audio else None,
        )
        return ProbeInfo(
            media=media,
            streams=tuple(streams),
            video=video,
            audio=audio,
            format_names=format_names,
        )

    def _validate_input(self, probe: ProbeInfo, options: ConversionOptions) -> None:
        spec = TARGET_SPECS[options.target]
        if probe.media.media_class not in spec.accepts:
            accepted = ", ".join(sorted(media_class.value for media_class in spec.accepts))
            raise ProcessingError(
                "UNSUPPORTED_CONVERSION",
                f"This output accepts only these input classes: {accepted}",
            )

        if spec.video_codec and probe.video is None:
            raise ProcessingError("UNSUPPORTED_MEDIA", "A visual stream is required")
        if spec.audio_codec and probe.audio is None:
            raise ProcessingError("UNSUPPORTED_MEDIA", "An audio stream is required")

        if probe.video:
            width = probe.video.width
            height = probe.video.height
            if not width or not height:
                raise ProcessingError("UNSUPPORTED_MEDIA", "Visual dimensions are unavailable")
            if width > self._settings.max_axis_pixels or height > self._settings.max_axis_pixels:
                raise ProcessingError("MEDIA_LIMIT_EXCEEDED", "Visual dimensions exceed the limit")
            if width * height > self._settings.max_image_pixels:
                raise ProcessingError(
                    "MEDIA_LIMIT_EXCEEDED", "Visual pixel count exceeds the limit"
                )
            if (
                options.target is Target.ANIMATION_GIF
                and width * height > self._settings.max_animation_pixels
            ):
                raise ProcessingError(
                    "MEDIA_LIMIT_EXCEEDED",
                    "GIF input pixel count exceeds the animation limit",
                )
            if probe.video.frame_rate and probe.video.frame_rate > 60.0:
                raise ProcessingError("MEDIA_LIMIT_EXCEEDED", "Frame rate exceeds 60 FPS")

        if probe.audio:
            if probe.audio.channels and probe.audio.channels > 8:
                raise ProcessingError(
                    "MEDIA_LIMIT_EXCEEDED", "Audio channel count exceeds the limit"
                )
            if probe.audio.sample_rate and probe.audio.sample_rate > 192_000:
                raise ProcessingError("MEDIA_LIMIT_EXCEEDED", "Audio sample rate exceeds the limit")

        duration_ms = probe.media.duration_ms
        if duration_ms is not None and duration_ms > self._settings.max_duration_seconds * 1000:
            raise ProcessingError("MEDIA_LIMIT_EXCEEDED", "Media duration exceeds the limit")
        if (
            options.target is Target.ANIMATION_GIF
            and duration_ms is not None
            and duration_ms > 15_000
        ):
            raise ProcessingError("MEDIA_LIMIT_EXCEEDED", "GIF output is limited to 15 seconds")

    def _validate_output(self, probe: ProbeInfo, target: Target) -> None:
        spec = TARGET_SPECS[target]
        if not probe.format_names.intersection(spec.formats):
            raise ProcessingError(
                "CONVERSION_FAILED", "Converted container failed validation"
            )
        if spec.video_codec and (
            probe.video is None or probe.video.codec_name != spec.video_codec
        ):
            raise ProcessingError(
                "CONVERSION_FAILED", "Converted visual stream failed validation"
            )
        if spec.audio_codec and (
            probe.audio is None or probe.audio.codec_name != spec.audio_codec
        ):
            raise ProcessingError(
                "CONVERSION_FAILED", "Converted audio stream failed validation"
            )

        expected_streams = 1
        if target in {Target.VIDEO_MP4, Target.VIDEO_WEBM} and probe.audio is not None:
            expected_streams = 2
        usable_streams = [stream for stream in probe.streams if not stream.attached_picture]
        if len(usable_streams) > expected_streams:
            raise ProcessingError("CONVERSION_FAILED", "Converted media has unexpected streams")

    def _build_command(
        self,
        input_path: Path,
        output_path: Path,
        source: ProbeInfo,
        options: ConversionOptions,
    ) -> list[str]:
        command = [
            self._settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-probesize",
            "33554432",
            "-analyzeduration",
            "10000000",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            INPUT_FORMAT_WHITELIST,
            "-threads",
            str(self._settings.ffmpeg_threads),
            "-i",
            os.fspath(input_path),
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
        ]

        if options.target in {Target.VIDEO_MP4, Target.VIDEO_WEBM}:
            self._append_video_args(command, source, options)
        elif options.target in {Target.IMAGE_JPEG, Target.IMAGE_PNG, Target.IMAGE_WEBP}:
            self._append_image_args(command, source, options)
        elif options.target is Target.ANIMATION_GIF:
            self._append_gif_args(command, source, options)
        else:
            self._append_audio_args(command, source, options)

        command.extend(
            [
                "-fs",
                str(self._settings.max_output_bytes),
                "-threads",
                str(self._settings.ffmpeg_threads),
                "-n",
                os.fspath(output_path),
            ]
        )
        return command

    def _append_video_args(
        self,
        command: list[str],
        source: ProbeInfo,
        options: ConversionOptions,
    ) -> None:
        assert source.video is not None
        command.extend(["-map", f"0:{source.video.index}"])
        if options.audio is AudioMode.KEEP and source.audio is not None:
            command.extend(["-map", f"0:{source.audio.index}"])

        command.extend(["-vf", self._scale_filter(options.resolution, divisible_by_two=True)])
        if options.target is Target.VIDEO_MP4:
            crf = {Quality.ECONOMY: "32", Quality.BALANCED: "26", Quality.HIGH: "20"}[
                options.quality
            ]
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    crf,
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
            if options.audio is AudioMode.KEEP and source.audio is not None:
                bitrate = {
                    Quality.ECONOMY: "96k",
                    Quality.BALANCED: "160k",
                    Quality.HIGH: "256k",
                }[options.quality]
                command.extend(["-c:a", "aac", "-b:a", bitrate])
            command.extend(["-movflags", "+faststart", "-f", "mp4"])
        else:
            crf = {Quality.ECONOMY: "40", Quality.BALANCED: "32", Quality.HIGH: "24"}[
                options.quality
            ]
            command.extend(
                [
                    "-c:v",
                    "libvpx-vp9",
                    "-b:v",
                    "0",
                    "-crf",
                    crf,
                    "-deadline",
                    "good",
                    "-cpu-used",
                    "2",
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
            if options.audio is AudioMode.KEEP and source.audio is not None:
                bitrate = {
                    Quality.ECONOMY: "64k",
                    Quality.BALANCED: "128k",
                    Quality.HIGH: "192k",
                }[options.quality]
                command.extend(["-c:a", "libopus", "-b:a", bitrate])
            command.extend(["-f", "webm"])

    def _append_image_args(
        self,
        command: list[str],
        source: ProbeInfo,
        options: ConversionOptions,
    ) -> None:
        assert source.video is not None
        command.extend(["-map", f"0:{source.video.index}"])
        scale = self._scale_filter(options.resolution, divisible_by_two=False)
        if scale != "null":
            command.extend(["-vf", scale])
        command.extend(["-frames:v", "1"])

        if options.target is Target.IMAGE_JPEG:
            quality = {Quality.ECONOMY: "8", Quality.BALANCED: "5", Quality.HIGH: "2"}[
                options.quality
            ]
            command.extend(
                [
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    quality,
                    "-pix_fmt",
                    "yuvj420p",
                    "-f",
                    "image2",
                ]
            )
        elif options.target is Target.IMAGE_PNG:
            compression = {Quality.ECONOMY: "3", Quality.BALANCED: "6", Quality.HIGH: "9"}[
                options.quality
            ]
            command.extend(["-c:v", "png", "-compression_level", compression, "-f", "image2"])
        else:
            quality = {Quality.ECONOMY: "60", Quality.BALANCED: "78", Quality.HIGH: "90"}[
                options.quality
            ]
            command.extend(
                [
                    "-c:v",
                    "libwebp",
                    "-lossless",
                    "0",
                    "-quality",
                    quality,
                    "-compression_level",
                    "4",
                    "-f",
                    "image2",
                ]
            )

    def _append_gif_args(
        self,
        command: list[str],
        source: ProbeInfo,
        options: ConversionOptions,
    ) -> None:
        assert source.video is not None
        fps, colors = {
            Quality.ECONOMY: (8, 64),
            Quality.BALANCED: (12, 128),
            Quality.HIGH: (15, 256),
        }[options.quality]
        resolution = options.resolution
        if resolution is Resolution.SOURCE:
            scale = "scale=iw:ih"
        else:
            scale = self._scale_filter(resolution, divisible_by_two=False)
        graph = (
            f"[0:{source.video.index}]fps={fps},{scale}:flags=lanczos,split=2[g0][g1];"
            f"[g0]palettegen=max_colors={colors}:stats_mode=diff[p];"
            "[g1][p]paletteuse=dither=sierra2_4a[gif]"
        )
        command.extend(["-filter_complex", graph, "-map", "[gif]", "-loop", "0", "-f", "gif"])

    def _append_audio_args(
        self,
        command: list[str],
        source: ProbeInfo,
        options: ConversionOptions,
    ) -> None:
        assert source.audio is not None
        command.extend(["-map", f"0:{source.audio.index}", "-vn"])
        bitrate = {
            Quality.ECONOMY: "96k",
            Quality.BALANCED: "160k",
            Quality.HIGH: "256k",
        }[options.quality]
        if options.target is Target.AUDIO_M4A:
            command.extend(["-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart", "-f", "mp4"])
        elif options.target is Target.AUDIO_MP3:
            command.extend(["-c:a", "libmp3lame", "-b:a", bitrate, "-f", "mp3"])
        else:
            opus_bitrate = {
                Quality.ECONOMY: "64k",
                Quality.BALANCED: "128k",
                Quality.HIGH: "192k",
            }[options.quality]
            command.extend(["-c:a", "libopus", "-b:a", opus_bitrate, "-f", "ogg"])

    @staticmethod
    def _scale_filter(resolution: Resolution, *, divisible_by_two: bool) -> str:
        if resolution is Resolution.SOURCE:
            if divisible_by_two:
                return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
            return "null"

        edge = RESOLUTION_MAX_EDGE[resolution]
        filter_value = (
            f"scale=w=min(iw\\,{edge}):h=min(ih\\,{edge}):"
            "force_original_aspect_ratio=decrease"
        )
        if divisible_by_two:
            filter_value += ":force_divisible_by=2"
        return filter_value

    async def _run_command(
        self,
        command: list[str],
        *,
        timeout: int,
        cancel_event: asyncio.Event | None = None,
        stdout_limit: int = 64 * 1024,
        stderr_limit: int = 64 * 1024,
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_task = asyncio.create_task(self._capture(process.stdout, stdout_limit))
        stderr_task = asyncio.create_task(self._capture(process.stderr, stderr_limit))
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event else None
        waiters = {wait_task}
        if cancel_task:
            waiters.add(cancel_task)

        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            timed_out = not done
            was_cancelled = bool(
                cancel_task and cancel_task in done and cancel_event and cancel_event.is_set()
            )
            if timed_out or was_cancelled:
                await self._terminate(process, wait_task)
            else:
                await wait_task
        except asyncio.CancelledError:
            await self._terminate(process, wait_task)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            if cancel_task:
                cancel_task.cancel()

        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)

        if was_cancelled:
            raise ConversionCancelled
        if timed_out:
            raise _CommandTimeout
        return CommandResult(returncode=process.returncode or 0, stdout=stdout, stderr=stderr)

    @staticmethod
    async def _capture(stream: asyncio.StreamReader, limit: int) -> bytes:
        captured = bytearray()
        while chunk := await stream.read(64 * 1024):
            remaining = limit - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
        return bytes(captured)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process, wait_task: asyncio.Task[int]) -> None:
        if process.returncode is not None:
            return

        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=2)
            return
        except TimeoutError:
            pass

        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        await wait_task

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value in {None, "", "N/A"}:
            return None
        parsed = int(value)
        return parsed if parsed >= 0 else None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value in {None, "", "N/A"}:
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None

    @staticmethod
    def _fraction(value: Any) -> float:
        if value in {None, "", "N/A", "0/0"}:
            return 0.0
        text = str(value)
        if "/" not in text:
            parsed = float(text)
            return parsed if math.isfinite(parsed) else 0.0
        numerator, denominator = text.split("/", maxsplit=1)
        divisor = float(denominator)
        if divisor == 0:
            return 0.0
        parsed = float(numerator) / divisor
        return parsed if math.isfinite(parsed) else 0.0
