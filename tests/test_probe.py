from pathlib import Path

import pytest

from media_manager.config import AuthMode, Settings
from media_manager.errors import ProcessingError
from media_manager.models import ConversionOptions, MediaClass, Target
from media_manager.processor import MediaProcessor


def _probe(processor, *, codec="gif", container="gif", average="10/1", nominal="100/1"):
    return processor._parse_probe(
        {"streams": [{"index": 0, "codec_type": "video", "codec_name": codec,
                      "width": 32, "height": 24, "avg_frame_rate": average,
                      "r_frame_rate": nominal, "duration": "0.200000"}],
         "format": {"format_name": container, "duration": "0.200000"}},
        943,
    )


def test_gif_timing_base_does_not_reject_a_valid_animation(tmp_path: Path) -> None:
    processor = MediaProcessor(Settings(work_dir=tmp_path, auth_mode=AuthMode.DISABLED))
    # Values observed with FFmpeg 6.1 on the two-frame, 100 ms synthetic GIF.
    probe = _probe(processor)
    assert probe.media.media_class == MediaClass.ANIMATION
    assert probe.video.frame_rate == 10
    for target in (Target.ANIMATION_GIF, Target.VIDEO_MP4, Target.VIDEO_WEBM):
        processor._validate_input(probe, ConversionOptions(target=target))


@pytest.mark.parametrize(
    "parameters",
    [
        {"average": "61/1"},
        {"average": "0/0"},
        {"average": "N/A"},
        {"codec": "h264", "container": "mov,mp4,m4a,3gp,3g2,mj2"},
        {"codec": "apng", "container": "apng"},
        {"codec": "gif", "container": "avi"},
    ],
)
def test_frame_rate_limits_remain_enforced(tmp_path: Path, parameters: dict) -> None:
    processor = MediaProcessor(Settings(work_dir=tmp_path, auth_mode=AuthMode.DISABLED))
    with pytest.raises(ProcessingError, match="Frame rate exceeds 60 FPS") as error:
        processor._validate_input(_probe(processor, **parameters),
                                  ConversionOptions(target=Target.ANIMATION_GIF))
    assert error.value.code == "MEDIA_LIMIT_EXCEEDED"


def test_still_png_probe_keeps_its_existing_rate(tmp_path: Path) -> None:
    processor = MediaProcessor(Settings(work_dir=tmp_path, auth_mode=AuthMode.DISABLED))
    probe = _probe(processor, codec="png", container="png_pipe", average="0/0", nominal="25/1")
    assert probe.media.media_class == MediaClass.IMAGE
    assert probe.video.frame_rate == 25
    processor._validate_input(probe, ConversionOptions(target=Target.IMAGE_JPEG))
