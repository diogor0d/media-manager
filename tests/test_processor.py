from __future__ import annotations

import asyncio
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest

from media_manager.config import AuthMode, Settings
from media_manager.models import (
    ConversionOptions,
    JobProgressStage,
    MediaClass,
    MediaMetadata,
    Quality,
    Resolution,
    Target,
)
from media_manager.processor import (
    COMPRESSION_TARGET_BYTES,
    ConversionResult,
    MediaProcessor,
    ProbeInfo,
    StreamInfo,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is not installed")
    return path


def _generate_fixtures(directory: Path, ffmpeg: str) -> dict[str, Path]:
    fixtures = {
        "video": directory / "source.mp4",
        "image": directory / "source.png",
        "audio": directory / "source.wav",
    }
    commands = [
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(fixtures["video"]),
        ],
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x48",
            "-frames:v",
            "1",
            "-y",
            str(fixtures["image"]),
        ],
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1",
            "-y",
            str(fixtures["audio"]),
        ],
    ]
    for command in commands:
        subprocess.run(command, check=True, capture_output=True)
    return fixtures


async def test_every_advertised_target_converts_with_the_runtime_ffmpeg(tmp_path: Path) -> None:
    ffmpeg = _tool("ffmpeg")
    ffprobe = _tool("ffprobe")
    fixtures = _generate_fixtures(tmp_path, ffmpeg)
    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        probe_timeout_seconds=10,
    )
    processor = MediaProcessor(settings)
    await processor.verify()

    sources = {
        Target.VIDEO_MP4: fixtures["video"],
        Target.VIDEO_WEBM: fixtures["video"],
        Target.IMAGE_JPEG: fixtures["image"],
        Target.IMAGE_PNG: fixtures["image"],
        Target.IMAGE_WEBP: fixtures["image"],
        Target.ANIMATION_GIF: fixtures["video"],
        Target.AUDIO_M4A: fixtures["audio"],
        Target.AUDIO_MP3: fixtures["audio"],
        Target.AUDIO_OPUS: fixtures["audio"],
    }

    for target, source in sources.items():
        job_dir = tmp_path / target.value
        job_dir.mkdir()
        resolution = (
            Resolution.P480 if target is Target.ANIMATION_GIF else Resolution.SOURCE
        )
        progress: list[tuple[JobProgressStage, int | None]] = []

        async def report(
            stage: JobProgressStage,
            percent: int | None,
            target: list[tuple[JobProgressStage, int | None]] = progress,
        ) -> None:
            target.append((stage, percent))

        result = await processor.convert(
            source,
            job_dir,
            ConversionOptions(target=target, resolution=resolution),
            asyncio.Event(),
            report,
        )

        assert result.output_path.is_file()
        assert result.output_bytes == result.output_path.stat().st_size
        assert result.output_bytes > 0
        assert result.filename.startswith("converted.")
        assert progress[0] == (JobProgressStage.INSPECTING, None)
        assert any(stage is JobProgressStage.CONVERTING for stage, _ in progress)
        assert progress[-1] == (JobProgressStage.VALIDATING, 100)


async def test_quality_percentage_preserves_legacy_anchors_and_is_monotonic() -> None:
    anchors = [
        (32, 26, 20, False),
        (40, 32, 24, False),
        (8, 5, 2, False),
        (3, 6, 9, True),
        (60, 78, 90, True),
        (8, 12, 15, True),
        (64, 128, 256, True),
        (96, 160, 256, True),
    ]
    for economy, balanced, high, increasing in anchors:
        values = [
            MediaProcessor._quality_value(
                ConversionOptions(target=Target.VIDEO_MP4, quality_percent=percent),
                economy,
                balanced,
                high,
            )
            for percent in range(101)
        ]
        assert (values[0], values[50], values[100]) == (economy, balanced, high)
        assert all(
            current <= following if increasing else current >= following
            for current, following in pairwise(values)
        )

    for quality, expected in (
        (Quality.ECONOMY, 32),
        (Quality.BALANCED, 26),
        (Quality.HIGH, 20),
    ):
        assert (
            MediaProcessor._quality_value(
                ConversionOptions(target=Target.VIDEO_MP4, quality=quality),
                32,
                26,
                20,
            )
            == expected
        )


async def test_ffmpeg_progress_parser_is_monotonic_and_never_reports_100() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"out_time_us=2500000\nout_time_us=2000000\nout_time_us=12000000\nprogress=end\n"
    )
    reader.feed_eof()
    observed: list[tuple[JobProgressStage, int | None]] = []

    async def report(stage: JobProgressStage, percent: int | None) -> None:
        observed.append((stage, percent))

    captured = await MediaProcessor._capture_progress(reader, 1024, 10_000, report)

    assert b"progress=end" in captured
    assert observed == [
        (JobProgressStage.CONVERTING, 25),
        (JobProgressStage.CONVERTING, 99),
    ]


async def test_explicit_fifty_percent_matches_every_balanced_ffmpeg_command(
    tmp_path: Path,
) -> None:
    processor = MediaProcessor(Settings(auth_mode=AuthMode.DISABLED))
    video = StreamInfo(
        index=0,
        codec_type="video",
        codec_name="h264",
        width=1920,
        height=1080,
        duration=10,
        frame_rate=30,
    )
    audio = StreamInfo(
        index=1,
        codec_type="audio",
        codec_name="aac",
        duration=10,
        channels=2,
        sample_rate=48_000,
    )
    source = ProbeInfo(
        media=MediaMetadata(
            bytes=1024,
            media_class=MediaClass.VIDEO,
            container="mov,mp4",
            duration_ms=10_000,
            width=1920,
            height=1080,
            video_codec="h264",
            audio_codec="aac",
        ),
        streams=(video, audio),
        video=video,
        audio=audio,
        format_names=frozenset({"mov", "mp4"}),
    )
    input_path = tmp_path / "input"
    output_path = tmp_path / "output"

    for target in Target:
        resolution = Resolution.P480 if target is Target.ANIMATION_GIF else Resolution.SOURCE
        legacy = processor._build_command(
            input_path,
            output_path,
            source,
            ConversionOptions(target=target, quality=Quality.BALANCED, resolution=resolution),
        )
        percentage = processor._build_command(
            input_path,
            output_path,
            source,
            ConversionOptions(target=target, quality_percent=50, resolution=resolution),
        )
        assert percentage == legacy


async def test_compression_automatically_selects_compact_canonical_outputs(
    tmp_path: Path,
) -> None:
    ffmpeg = _tool("ffmpeg")
    ffprobe = _tool("ffprobe")
    fixtures = _generate_fixtures(tmp_path, ffmpeg)
    processor = MediaProcessor(
        Settings(
            work_dir=tmp_path / "work",
            auth_mode=AuthMode.DISABLED,
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            probe_timeout_seconds=10,
        )
    )

    for media_class, expected_target in (
        ("video", Target.VIDEO_MP4),
        ("image", Target.IMAGE_WEBP),
        ("audio", Target.AUDIO_M4A),
    ):
        job_dir = tmp_path / f"compress-{media_class}"
        job_dir.mkdir()
        result = await processor.compress(
            fixtures[media_class],
            job_dir,
            asyncio.Event(),
        )

        assert result.output_path.is_file()
        assert result.output_bytes == result.output_path.stat().st_size
        assert result.compression.selected_target is expected_target
        assert result.compression.target_bytes == COMPRESSION_TARGET_BYTES
        assert result.compression.met_target is (result.output_bytes < COMPRESSION_TARGET_BYTES)
        assert result.compression.attempts == 1


async def test_compression_retry_prefers_the_working_aim_and_uses_strict_boundary(
    tmp_path: Path,
) -> None:
    audio = StreamInfo(index=0, codec_type="audio", codec_name="aac", duration=60)
    source = ProbeInfo(
        media=MediaMetadata(
            bytes=30_000_000,
            media_class=MediaClass.AUDIO,
            container="mov,mp4,m4a",
            duration_ms=60_000,
            audio_codec="aac",
        ),
        streams=(audio,),
        video=None,
        audio=audio,
        format_names=frozenset({"mov", "mp4", "m4a"}),
    )

    class SizedProcessor(MediaProcessor):
        def __init__(self, sizes: list[int]) -> None:
            super().__init__(Settings(auth_mode=AuthMode.DISABLED))
            self.sizes = iter(sizes)
            self.peak_attempt_directories = 0

        async def _probe(self, *_args, **_kwargs) -> ProbeInfo:
            return source

        async def convert(
            self,
            _input_path: Path,
            job_dir: Path,
            _options: ConversionOptions,
            _cancel_event: asyncio.Event,
            _progress_callback=None,
        ) -> ConversionResult:
            self.peak_attempt_directories = max(
                self.peak_attempt_directories,
                len(list(job_dir.parent.glob("compression-*"))),
            )
            output_path = job_dir / "result.m4a"
            output_path.write_bytes(b"candidate")
            size = next(self.sizes)
            return ConversionResult(
                input=source.media,
                output_path=output_path,
                output_bytes=size,
                filename="converted.m4a",
                media_type="audio/mp4",
                width=None,
                height=None,
                duration_ms=60_000,
            )

    aimed_dir = tmp_path / "aimed"
    aimed_dir.mkdir()
    aimed = SizedProcessor([19_500_000, 18_000_000])
    aimed_result = await aimed.compress(
        tmp_path / "input",
        aimed_dir,
        asyncio.Event(),
    )
    assert aimed_result.output_bytes == 18_000_000
    assert aimed_result.compression.met_target is True
    assert aimed_result.compression.attempts == 2
    assert aimed.peak_attempt_directories == 2
    assert not list(aimed_dir.glob("compression-*"))

    boundary_dir = tmp_path / "boundary"
    boundary_dir.mkdir()
    boundary = SizedProcessor([20_000_000, 21_000_000, 22_000_000])
    boundary_result = await boundary.compress(
        tmp_path / "input",
        boundary_dir,
        asyncio.Event(),
    )
    assert boundary_result.output_bytes == 20_000_000
    assert boundary_result.compression.met_target is False
    assert boundary_result.compression.attempts == 3
    assert boundary.peak_attempt_directories == 2
