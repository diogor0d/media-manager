# Project History

## 2026-10-09 - Prepared processing, estimates, and private integrations

- Added prepared conversion/compression flows with a separate estimate and
  explicit start action. Estimates expose bounded size/time ranges and may
  remain low-confidence or omit ETA; they are not exact-output promises.
- Added download workflows that connect a MeTube source record (`parent_id`)
  with an optional conversion/compression child (`child_id`), and joined
  downloads with jobs in per-owner history. Added optional private Telegram
  file/URL workflows, job updates, and result delivery.
- Accepted prepared, queued, and processing jobs are persisted for restart;
  processing recovery is limited to two retries. The default output retention
  remains seven days, with per-owner settings from one to 90 days. Chunked
  uploads are temporary live-process sessions and are not restart-recoverable.
- Source URLs are not written to the download registry or Telegram session
  state. Added backward-compatible MeTube request-ID correlation and validated
  all four pipelines with disposable real MeTube downloads, restart, retained
  originals, deletion, and expiry. Local checks passed: 125 Python, 33 web,
  13 Shortcut, and 20 sidecar tests; Chromium/WebKit responsive and real media
  flows; official local Bot API build and offline checks. See the
  [dated acceptance record](validation-implementation-2026-10-09.md).
- Telegram private configuration/live delivery, physical-device acceptance,
  installed Shortcut replacement, and production activation remain separate
  pending gates. Fully received review sources use owner retention; incomplete
  uploads retain their shorter deadline.
- Older-version rollback against the same work directory is unsafe until the
  older startup cleanup and record compatibility are verified; see
  [the deployment rollback procedure](deployment.md#rollback).

## 2026-09-04T03:15:18+01:00 - Progress, iPhone PWA, and Fast Shortcuts

Recall period: September 2026

- Added real FFmpeg timeline progress with inspect, convert, and validation
  stages; unknown durations remain indeterminate.
- Replaced three web quality presets with a bounded 0-100% technical scale while
  preserving legacy 0/50/100 behavior exactly.
- Preserved sanitized source basenames in `-converted` and `-compressed` result
  names without using filenames for media handling.
- Ran startup capability checks concurrently and coalesced upload writes.
- Refined the existing conversion-bench interface for iPhone safe areas, touch,
  narrow screens, focus, VoiceOver stage semantics, native file sharing,
  retained-job recovery, and non-disruptive PWA updates.
- Added a fixed-purpose compression API that automatically selects MP4, WebP,
  or M4A and makes at most five attempts toward a strict sub-20,000,000-byte
  result without guaranteeing success.
- Split native automation into a one-menu Convert Media Shortcut and a no-menu
  Compress Media Shortcut, with credential-free authoring and on-device gates.
- Verified 34 Python tests, including real FFmpeg conversion/compression, Ruff,
  JavaScript syntax, package build, and a local Docker image build at this stage.
- Production deployment of this revision and Apple-hardware Shortcut/PWA tests
  were initially outside this implementation record. The code revision was
  subsequently deployed on 2026-09-04; host evidence is maintained in the
  private homelab operations record. Apple-hardware validation remains pending.
