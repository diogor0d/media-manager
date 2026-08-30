from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from media_manager.config import AuthMode, Settings
from media_manager.models import ConversionOptions, Resolution, Target
from media_manager.processor import MediaProcessor

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
        result = await processor.convert(
            source,
            job_dir,
            ConversionOptions(target=target, resolution=resolution),
            asyncio.Event(),
        )

        assert result.output_path.is_file()
        assert result.output_bytes == result.output_path.stat().st_size
        assert result.output_bytes > 0
        assert result.filename.startswith("converted.")
