from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from media_manager.config import AuthMode, Settings
from media_manager.models import (
    AudioMode,
    CompressionMetadata,
    CompressionOptions,
    ConversionOptions,
    MediaClass,
    MediaMetadata,
    Resolution,
    Target,
)
from media_manager.processor import (
    ConversionResult,
    MediaProcessor,
    ProbeInfo,
    StreamInfo,
)


def _source() -> ProbeInfo:
    media = MediaMetadata(
        bytes=10_000_000,
        media_class=MediaClass.VIDEO,
        container="mp4",
        duration_ms=30_000,
        width=3840,
        height=2160,
        video_codec="h264",
        audio_codec="aac",
    )
    video = StreamInfo(0, "video", "h264", width=3840, height=2160, duration=30)
    return ProbeInfo(media, (video,), video, None, frozenset({"mp4"}))


def test_advanced_attempts_respect_resolution_cap_audio_and_selected_target(tmp_path: Path) -> None:
    processor = MediaProcessor(
        Settings(
            work_dir=tmp_path,
            auth_mode=AuthMode.DISABLED,
            public_base_url="http://localhost:8080",
        )
    )
    compression = CompressionOptions(
        target_bytes=4_000_000,
        target=Target.VIDEO_WEBM,
        resolution=Resolution.P1080,
        audio=AudioMode.DROP,
        quality_percent=75,
    )

    attempts = processor._advanced_compression_attempts(
        _source(), compression, compression.target
    )

    assert len(attempts) == 5
    assert [attempt.quality_percent for attempt in attempts] == [75, 25, 0, 75, 25]
    assert [attempt.resolution for attempt in attempts] == [
        Resolution.P1080,
        Resolution.P1080,
        Resolution.P1080,
        Resolution.P720,
        Resolution.P720,
    ]
    assert all(attempt.target is Target.VIDEO_WEBM for attempt in attempts)
    assert all(attempt.audio is AudioMode.DROP for attempt in attempts)


def test_sample_command_keeps_paths_as_arguments_and_applies_bounded_seek(tmp_path: Path) -> None:
    processor = MediaProcessor(
        Settings(
            work_dir=tmp_path,
            auth_mode=AuthMode.DISABLED,
            public_base_url="http://localhost:8080",
        )
    )
    source_path = tmp_path / "source with spaces.mp4"
    output_path = tmp_path / "sample with spaces.mp4"
    command = processor._build_command(
        source_path,
        output_path,
        _source(),
        ConversionOptions(target=Target.VIDEO_MP4),
        sample_start_seconds=12.5,
        sample_duration_seconds=2,
    )

    assert command[command.index("-ss") + 1] == "12.500"
    assert command[command.index("-t") + 1] == "2.000"
    assert str(source_path) in command
    assert str(output_path) in command


@pytest.mark.asyncio
async def test_advanced_compression_uses_dynamic_target_and_returns_smallest_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor = MediaProcessor(
        Settings(
            work_dir=tmp_path,
            auth_mode=AuthMode.DISABLED,
            public_base_url="http://localhost:8080",
        )
    )
    source = ProbeInfo(
        media=MediaMetadata(
            bytes=2_000_000,
            media_class=MediaClass.IMAGE,
            container="image2",
            width=1600,
            height=1200,
        ),
        streams=(),
        video=StreamInfo(0, "video", "png", width=1600, height=1200),
        audio=None,
        format_names=frozenset({"image2"}),
    )

    async def probe(*args: object, **kwargs: object) -> ProbeInfo:
        return source

    async def convert(
        input_path: Path,
        job_dir: Path,
        options: ConversionOptions,
        cancel_event: asyncio.Event,
        progress_callback: object = None,
    ) -> ConversionResult:
        output = job_dir / "result.webp"
        output_bytes = 1_200_000 if options.quality_percent == 50 else 900_000
        output.write_bytes(b"x" * output_bytes)
        return ConversionResult(
            input=source.media,
            output_path=output,
            output_bytes=output_bytes,
            filename="converted.webp",
            media_type="image/webp",
            width=1600,
            height=1200,
            duration_ms=None,
        )

    monkeypatch.setattr(processor, "_probe", probe)
    monkeypatch.setattr(processor, "convert", convert)
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    result = await processor.compress(
        tmp_path / "source.png",
        job_dir,
        asyncio.Event(),
        options=CompressionOptions(target_bytes=1_000_000, target=Target.IMAGE_WEBP),
    )

    assert result.output_path.is_file()
    assert result.output_bytes == 900_000
    assert result.compression == CompressionMetadata(
        target_bytes=1_000_000,
        aim_bytes=950_000,
        met_target=True,
        attempts=2,
        selected_target=Target.IMAGE_WEBP,
    )
