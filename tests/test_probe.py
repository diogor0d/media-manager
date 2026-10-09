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


def _phone_mov_probe_payload(stream_count: int = 9) -> dict:
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "hevc",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "60000/1001",
            "r_frame_rate": "60000/1001",
            "duration": "16.815",
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "sample_rate": "48000",
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "unknown",
            "channels": 4,
            "sample_rate": "48000",
        },
    ]
    streams.extend(
        {"index": index, "codec_type": "data", "codec_name": "bin_data"}
        for index in range(3, stream_count)
    )
    return {
        "streams": streams,
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "16.815"},
    }


def test_phone_mov_with_nine_streams_maps_only_video_and_first_audio(tmp_path: Path) -> None:
    processor = MediaProcessor(Settings(work_dir=tmp_path, auth_mode=AuthMode.DISABLED))
    probe = processor._parse_probe(_phone_mov_probe_payload(), 1_000_000)
    options = ConversionOptions(target=Target.VIDEO_MP4)

    processor._validate_input(probe, options)
    command = processor._build_command(Path("input.mov"), Path("output.mp4"), probe, options)

    assert probe.video is not None and probe.video.index == 0
    assert probe.audio is not None and probe.audio.index == 1
    assert [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-map"] == [
        "0:0",
        "0:1",
    ]
    assert "-sn" in command
    assert "-dn" in command


def test_probe_accepts_exact_stream_limit_and_rejects_one_over(tmp_path: Path) -> None:
    processor = MediaProcessor(Settings(work_dir=tmp_path, auth_mode=AuthMode.DISABLED))

    processor._parse_probe(_phone_mov_probe_payload(stream_count=64), 1_000_000)
    with pytest.raises(ProcessingError) as error:
        processor._parse_probe(_phone_mov_probe_payload(stream_count=65), 1_000_000)

    assert error.value.code == "MEDIA_LIMIT_EXCEEDED"
    assert error.value.message == "Media has 65 streams; the limit is 64"


def test_configured_stream_limit_remains_enforced(tmp_path: Path) -> None:
    processor = MediaProcessor(
        Settings(work_dir=tmp_path, auth_mode=AuthMode.DISABLED, max_streams=8)
    )

    with pytest.raises(ProcessingError) as error:
        processor._parse_probe(_phone_mov_probe_payload(), 1_000_000)

    assert error.value.code == "MEDIA_LIMIT_EXCEEDED"
    assert error.value.message == "Media has 9 streams; the limit is 8"


def test_empty_stream_list_is_unsupported_media(tmp_path: Path) -> None:
    processor = MediaProcessor(Settings(work_dir=tmp_path, auth_mode=AuthMode.DISABLED))

    with pytest.raises(ProcessingError) as error:
        processor._parse_probe(
            {"streams": [], "format": {"format_name": "mov"}}, 1_000
        )

    assert error.value.code == "UNSUPPORTED_MEDIA"
    assert error.value.message == "Media has no audio or visual streams"


def test_stream_limit_environment_default_is_64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_MANAGER_AUTH_MODE", "disabled")
    monkeypatch.delenv("MEDIA_MANAGER_MAX_STREAMS", raising=False)

    assert Settings.from_env().max_streams == 64


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
