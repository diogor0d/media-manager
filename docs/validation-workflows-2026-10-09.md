# Real workflow validation — 2026-10-09

This record covers a local, disposable acceptance run of the four MeTube-backed
Media Manager workflows. It exercises the real MeTube extractor and downloader,
the sidecar, and the Media Manager processor against generated synthetic media.
It does not use source-site URLs, production accounts, real media, or production
services.

## Isolation and cleanup

Run the complete check with:

```sh
python3 scripts/validate_workflows.py
```

What it does: builds and runs a disposable synthetic Docker stack, validates
the workflows, and removes its exact project, volumes, and validation images.

The runner uses Docker Desktop's `desktop-linux` context and creates a unique
Compose project for every run. Its network is internal, no service publishes a
host port, and generated media lives in project-scoped Docker volumes. The
MeTube image is digest-pinned. The main image is built from this checkout with
the frozen `uv.lock`; the sidecar gets a unique image tag. The checker makes
HTTP requests from inside the Compose network. On completion or failure, the
runner removes that exact Compose project and its volumes, then removes the
validation image tags.

The synthetic source is an eight-second 1280×720 H.264/AAC clip, a PCM audio
fixture, and a JPEG thumbnail, generated inside the fixture container. The local
HTML page exposes the video and thumbnail to MeTube. The fixture service must be
healthy before checks start. MeTube receives only the capabilities needed by its
image entrypoint to drop privileges and initialize its downloads directory; all
services drop other capabilities and run without published ports.

## Acceptance cases

The runner checks these behaviors:

- Download-only completes through MeTube, returns the original MP4, probes its
  streams and duration with `ffprobe`, and appears in history.
- A conversion request pauses at review. The API is restarted before conversion;
  the same parent ID and source checksum must survive. The checker prepares a
  WebM conversion, waits for an estimate, explicitly starts the child job, and
  verifies its history entry and probed output.
- Automatic compression returns a ready child output with matching byte-count
  metadata and `ffprobe` dimensions.
- Automatic convert-and-compress produces WebM, honors the requested 480p cap
  and dropped audio stream, and reports dimensions and byte count matching the
  final file.
- A thumbnail-to-video conversion failure retains a downloadable source. Deleting
  Media Manager's private copy must leave MeTube's original file present with the same hash.
- A separate short-TTL check changes retention only in the final disposable API
  container, then verifies the expired API result is inaccessible while MeTube's source
  remains unchanged.

## Results

The complete detached run exited with status 0. All six checks below passed,
including the TTL-only final container restart; the runner's exact-project
cleanup completed.

- Download-only returned an 8-second, 1280×720 H.264/AAC MP4 (2,854,822 bytes),
  reached `ready`, and appeared in history.
- Review paused at `awaiting_review`. After an API restart, the same download ID
  and source checksum were restored. The sampled estimate reached `ready`; an
  explicit start produced a 1280×720 VP9/Opus WebM (2,883,542 bytes), with the
  parent and child present in history.
- Automatic compression produced a ready 854×480 H.264/AAC MP4 (405,619 bytes).
- Automatic convert-and-compress produced a ready VP9 WebM (478,982 bytes),
  854×480, with no audio stream. The response byte count and dimensions matched
  the final file reported by `ffprobe`.
- Converting a downloaded JPEG thumbnail to video failed with
  `PROCESSING_FAILED` while retaining the 320×200, 613-byte JPEG. Deleting the
  API's private copy left MeTube's original file present with an unchanged
  SHA-256 hash.
- The last API container used a five-second result TTL. After expiry, both the
  download metadata and content endpoints returned 404, the item disappeared
  from history, and MeTube's original remained present with an unchanged hash.

The main image built from the current checkout using frozen `uv.lock`. The
runner removed the project volumes, network, containers, the fixed validation
image tag, and the unique sidecar image tag. It left no published ports or
synthetic test stack running.

Production deployment, public-source compatibility, Telegram delivery, and
physical iPhone/Shortcut behavior are outside this local validation.
