from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from media_manager.config import AuthMode, Settings
from media_manager.estimates import EstimateService
from media_manager.models import (
    CompressionOptions,
    ConversionOptions,
    MediaClass,
    MediaMetadata,
    Resolution,
    Target,
)
from media_manager.processor import (
    CommandResult,
    ConversionCancelled,
    MediaProcessor,
    ProbeInfo,
    StreamInfo,
)


def _probe() -> ProbeInfo:
    media = MediaMetadata(
        bytes=1_000_000,
        media_class=MediaClass.VIDEO,
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_ms=10_000,
        width=1280,
        height=720,
        video_codec="h264",
        audio_codec="aac",
    )
    video = StreamInfo(0, "video", "h264", width=1280, height=720, duration=10)
    return ProbeInfo(
        media=media,
        streams=(video,),
        video=video,
        audio=None,
        format_names=frozenset({"mov", "mp4"}),
    )


class FakeProcessor:
    def __init__(self, work_dir: Path) -> None:
        self._settings = Settings(
            work_dir=work_dir,
            auth_mode=AuthMode.DISABLED,
            public_base_url="http://localhost:8080",
        )
        self.calls: list[list[str]] = []
        self.options_seen: list[ConversionOptions] = []

    async def inspect(
        self, path: Path, cancel_event: asyncio.Event | None = None
    ) -> MediaMetadata:
        return _probe().media

    async def _probe(self, path: Path, *, cancel_event: asyncio.Event | None = None) -> ProbeInfo:
        return _probe()

    def _build_command(
        self,
        input_path: Path,
        output_path: Path,
        source: ProbeInfo,
        options: ConversionOptions,
        *,
        sample_start_seconds: float | None = None,
        sample_duration_seconds: float | None = None,
    ) -> list[str]:
        self.options_seen.append(options)
        command = ["ffmpeg", "-i", str(input_path)]
        if sample_start_seconds is not None:
            command.extend(["-ss", str(sample_start_seconds)])
        if sample_duration_seconds is not None:
            command.extend(["-t", str(sample_duration_seconds)])
        command.append(str(output_path))
        return command

    @staticmethod
    def _advanced_compression_attempts(
        source: ProbeInfo,
        options: CompressionOptions,
        target: Target,
    ) -> tuple[ConversionOptions, ...]:
        return MediaProcessor._advanced_compression_attempts(source, options, target)

    async def _run_command(self, command: list[str], **kwargs: object) -> CommandResult:
        self.calls.append(command)
        Path(command[-1]).write_bytes(b"sample-output" * 20)
        return CommandResult(returncode=0, stdout=b"", stderr=b"")


@pytest.mark.asyncio
async def test_fast_estimate_is_broad_pending_and_keyed_by_options(tmp_path: Path) -> None:
    processor = FakeProcessor(tmp_path)
    service = EstimateService(processor._settings, processor)  # type: ignore[arg-type]
    source = tmp_path / "source.mov"
    source.write_bytes(b"source")

    estimate = await service.fast(
        source,
        ConversionOptions(target=Target.VIDEO_WEBM, resolution=Resolution.P720),
        None,
    )

    assert estimate.status == "pending"
    assert estimate.method == "fast"
    assert estimate.confidence == "low"
    assert estimate.eta_min_seconds is estimate.eta_max_seconds is None
    assert estimate.size_min_bytes < estimate.size_max_bytes
    assert estimate.options_key


@pytest.mark.asyncio
async def test_fast_compression_estimate_accounts_for_target_quality_and_resolution(
    tmp_path: Path,
) -> None:
    processor = FakeProcessor(tmp_path)
    service = EstimateService(processor._settings, processor)  # type: ignore[arg-type]
    source = tmp_path / "source.mov"
    source.write_bytes(b"source")

    defaults = await service.fast(source, None, CompressionOptions())
    constrained = await service.fast(
        source,
        None,
        CompressionOptions(
            target_bytes=5_000_000,
            quality_percent=100,
            resolution=Resolution.P480,
        ),
    )

    assert defaults.size_min_bytes < 20_000_000 < defaults.size_max_bytes
    assert constrained.size_min_bytes < 5_000_000 < constrained.size_max_bytes
    assert constrained.size_max_bytes > 5_000_000  # target is not a hard cap
    assert constrained.size_min_bytes != defaults.size_min_bytes
    assert constrained.options_key != defaults.options_key


@pytest.mark.asyncio
async def test_refine_uses_at_most_three_two_second_samples(tmp_path: Path) -> None:
    processor = FakeProcessor(tmp_path)
    service = EstimateService(processor._settings, processor)  # type: ignore[arg-type]
    source = tmp_path / "source.mov"
    source.write_bytes(b"source")

    estimate = await service.refine(
        source,
        ConversionOptions(target=Target.VIDEO_MP4),
        None,
        asyncio.Event(),
    )

    assert estimate.status == "ready"
    assert estimate.method == "sampled"
    assert estimate.confidence == "medium"
    assert estimate.size_min_bytes < estimate.size_max_bytes
    assert estimate.eta_min_seconds is not None
    assert estimate.eta_max_seconds >= estimate.eta_min_seconds
    assert len(processor.calls) == 3
    assert all(
        "-t" in command and float(command[command.index("-t") + 1]) <= 2
        for command in processor.calls
    )


@pytest.mark.asyncio
async def test_compression_refinement_samples_first_real_candidate_and_accounts_for_retries(
    tmp_path: Path,
) -> None:
    processor = FakeProcessor(tmp_path)
    service = EstimateService(processor._settings, processor)  # type: ignore[arg-type]
    source = tmp_path / "source.mov"
    source.write_bytes(b"source")
    options = CompressionOptions(
        target_bytes=250_000,
        quality_percent=80,
        resolution=Resolution.P720,
    )

    estimate = await service.refine(source, None, options, asyncio.Event())

    assert estimate.status == "ready"
    assert estimate.method == "sampled"
    assert processor.options_seen[0].target is Target.VIDEO_MP4
    assert processor.options_seen[0].quality_percent == 80
    assert processor.options_seen[0].resolution is Resolution.P720
    assert len(processor.calls) == 3
    assert estimate.size_min_bytes < options.target_bytes < estimate.size_max_bytes
    assert estimate.size_max_bytes > options.target_bytes  # never claim a hard cap
    assert estimate.eta_max_seconds is not None and estimate.eta_max_seconds >= 5


@pytest.mark.asyncio
async def test_refinements_share_one_sampling_slot(tmp_path: Path) -> None:
    class TrackingProcessor(FakeProcessor):
        def __init__(self, work_dir: Path) -> None:
            super().__init__(work_dir)
            self.active = 0
            self.max_active = 0

        async def _run_command(self, command: list[str], **kwargs: object) -> CommandResult:
            self.calls.append(command)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                Path(command[-1]).write_bytes(b"sample-output" * 20)
                return CommandResult(returncode=0, stdout=b"", stderr=b"")
            finally:
                self.active -= 1

    processor = TrackingProcessor(tmp_path)
    service = EstimateService(processor._settings, processor)  # type: ignore[arg-type]
    first, second = tmp_path / "first.mov", tmp_path / "second.mov"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    results = await asyncio.gather(
        service.refine(first, ConversionOptions(target=Target.VIDEO_MP4), None, asyncio.Event()),
        service.refine(second, ConversionOptions(target=Target.VIDEO_MP4), None, asyncio.Event()),
    )

    assert all(result.status == "ready" for result in results)
    assert processor.max_active == 1


@pytest.mark.asyncio
async def test_refine_falls_back_when_sampling_fails(tmp_path: Path) -> None:
    class FailingProcessor(FakeProcessor):
        async def _run_command(self, command: list[str], **kwargs: object) -> CommandResult:
            self.calls.append(command)
            return CommandResult(returncode=1, stdout=b"", stderr=b"failed")

    processor = FailingProcessor(tmp_path)
    service = EstimateService(processor._settings, processor)  # type: ignore[arg-type]
    source = tmp_path / "source.mov"
    source.write_bytes(b"source")

    estimate = await service.refine(
        source,
        ConversionOptions(target=Target.VIDEO_MP4),
        CompressionOptions(target_bytes=500_000),
        asyncio.Event(),
    )

    assert estimate.status == "fallback"
    assert estimate.method == "fast"
    assert estimate.eta_min_seconds is None


@pytest.mark.asyncio
async def test_sampling_deadline_returns_fast_fallback(tmp_path, monkeypatch) -> None:
    from media_manager import estimates as estimates_module

    class SlowProcessor(FakeProcessor):
        async def _run_command(self, command, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(estimates_module, "ESTIMATE_DEADLINE_SECONDS", 0.05)
    processor = SlowProcessor(tmp_path)
    service = EstimateService(processor._settings, processor)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    estimate = await asyncio.wait_for(
        service.refine(source, ConversionOptions(target=Target.VIDEO_MP4), None, asyncio.Event()),
        1,
    )
    assert estimate.status == "fallback"
    assert estimate.method == "fast"
    assert not list(tmp_path.glob("media-estimate-*"))


@pytest.mark.asyncio
async def test_refine_falls_back_without_duration_for_temporal_media(tmp_path: Path) -> None:
    class UnknownDurationProcessor(FakeProcessor):
        async def inspect(
            self, path: Path, cancel_event: asyncio.Event | None = None
        ) -> MediaMetadata:
            return _probe().media.model_copy(update={"duration_ms": None})

        async def _probe(
            self, path: Path, *, cancel_event: asyncio.Event | None = None
        ) -> ProbeInfo:
            probe = _probe()
            return ProbeInfo(
                media=probe.media.model_copy(update={"duration_ms": None}),
                streams=probe.streams,
                video=probe.video,
                audio=probe.audio,
                format_names=probe.format_names,
            )

    processor = UnknownDurationProcessor(tmp_path)
    service = EstimateService(processor._settings, processor)  # type: ignore[arg-type]
    source = tmp_path / "unknown-duration.mov"
    source.write_bytes(b"source")

    estimate = await service.refine(
        source, ConversionOptions(target=Target.VIDEO_MP4), None, asyncio.Event()
    )

    assert estimate.status == "fallback"
    assert estimate.method == "fast"
    assert estimate.eta_min_seconds is None
    assert processor.calls == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_ffmpeg_conversion_and_estimates_cover_video_audio_and_image(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg and ffprobe are required")

    fixtures = {
        "video": tmp_path / "source.mp4",
        "audio": tmp_path / "source.wav",
        "image": tmp_path / "source.png",
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
            "testsrc2=size=640x360:rate=12",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
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
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "4",
            "-y",
            str(fixtures["audio"]),
        ],
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=1",
            "-frames:v",
            "1",
            "-y",
            str(fixtures["image"]),
        ],
    ]
    for command in commands:
        subprocess.run(command, check=True, capture_output=True)

    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        probe_timeout_seconds=10,
    )
    settings.work_dir.mkdir()
    processor = MediaProcessor(settings)
    estimates = EstimateService(settings, processor)
    cases = (
        (fixtures["video"], Target.VIDEO_MP4),
        (fixtures["audio"], Target.AUDIO_M4A),
        (fixtures["image"], Target.IMAGE_WEBP),
    )
    for index, (source, target) in enumerate(cases):
        options = ConversionOptions(target=target)
        job_dir = tmp_path / f"converted-{index}"
        job_dir.mkdir()
        converted = await processor.convert(source, job_dir, options, asyncio.Event())
        assert converted.output_path.is_file()
        assert converted.output_bytes == converted.output_path.stat().st_size > 0

        estimate = await estimates.refine(
            source, options, None, asyncio.Event()
        )
        assert estimate.status == "ready"
        assert estimate.method == "sampled"
        assert estimate.size_min_bytes < estimate.size_max_bytes
        assert estimate.size_min_bytes <= converted.output_bytes <= estimate.size_max_bytes
        assert estimate.eta_min_seconds is not None
        assert estimate.eta_max_seconds >= estimate.eta_min_seconds


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_ffmpeg_estimate_cancellation_cleans_sample_files(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg and ffprobe are required")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30",
            "-t",
            "8",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        probe_timeout_seconds=10,
    )
    settings.work_dir.mkdir()
    processor = MediaProcessor(settings)
    estimates = EstimateService(settings, processor)
    cancel_event = asyncio.Event()
    run_command = processor._run_command

    async def cancel_during_sample(command: list[str], **kwargs: object) -> CommandResult:
        if command[0] == ffmpeg and "-progress" in command:
            asyncio.get_running_loop().call_later(0.01, cancel_event.set)
        return await run_command(command, **kwargs)

    processor._run_command = cancel_during_sample  # type: ignore[method-assign]
    with pytest.raises(ConversionCancelled):
        await estimates.refine(
            source,
            ConversionOptions(target=Target.VIDEO_MP4),
            None,
            cancel_event,
        )
    assert not list(settings.work_dir.glob("media-estimate-*"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_compression_estimate_does_not_claim_an_impossible_target(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg and ffprobe are required")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=12",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        probe_timeout_seconds=10,
    )
    settings.work_dir.mkdir()
    processor = MediaProcessor(settings)
    estimates = EstimateService(settings, processor)
    options = CompressionOptions(target_bytes=1, quality_percent=50)

    estimate = await estimates.refine(source, None, options, asyncio.Event())
    job_dir = tmp_path / "compression"
    job_dir.mkdir()
    actual = await processor.compress(
        source, job_dir, asyncio.Event(), options=options
    )

    assert estimate.status == "ready"
    assert estimate.method == "sampled"
    assert estimate.size_max_bytes > options.target_bytes
    assert actual.output_bytes > options.target_bytes
    assert not actual.compression.met_target
    assert estimate.size_min_bytes <= actual.output_bytes <= estimate.size_max_bytes
