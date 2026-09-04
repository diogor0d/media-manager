# Project History

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
