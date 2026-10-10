# Workflow navigation release — 2026-10-11

Occurrence, verification, and documentation date: 2026-10-11 (Europe/Lisbon).
Recall period: October 2026. The user authorized commit, push, and redeployment.

## Change

Download, Convert, and Compress now sit in a compact navigation row below the
page title and outside the media panel. The active workflow has a blue underline;
there is no enclosing capsule or filled selection. Mobile uses equal-width
controls with 48-pixel height. The obsolete Converter brand badge was removed.

The HTML asset versions, service-worker cache, and existing validation references
advance together to `workflows3`. Processing code, dependencies, persistence
formats, authentication configuration, and the sidecar bridge are unchanged.

## Local verification

- 33 web logic tests and the workflow contract check passed.
- The API web/security-header test passed; Ruff and diff checks passed.
- Chromium and WebKit each passed 30 navigation combinations: all three modes
  at 320, 390, 834, 1100, and 1366 pixels in light/dark themes, with keyboard
  activation, no horizontal overflow, and at least 44-pixel control dimensions.
- Both engines passed real PNG upload, combined compression controls, estimate
  review and explicit start, close/history recovery, and retained-result retrieval.
  A separate mocked URL-flow check covered the existing processing controls.
  Local URL availability remained correctly disabled without a sidecar.
- Screenshots were inspected on desktop and phone. WebKit's 11 screenshot-helper
  CSP warnings occurred during capture only; workflow checks had no failures.

## Deployment

Only Media Manager's `api` service was recreated on `diogoserver2`.

- Source: `85fc9948c0a711fac52c59b64f222c3bb66f1a0b`.
- Image: `sha256:e85b28b1ce1a3b7d71ba88f07fcd0aa4af60c05f50dd698056955a9e21f44a56`.
- Healthy, restart count zero, UID/GID 10001, read-only root, loopback-only
  `127.0.0.1:18081`; anonymous origin access remained denied with 401.
- All four retained metadata records and three output files matched their
  pre-deployment SHA-256 checksums. No history cleanup was performed.
- MeTube and its sidecar retained the exact same containers and images.
- Authenticated live Chromium and WebKit each passed the same 30 navigation
  combinations against the deployed site, including the `workflows3` service-worker
  revision, with no JavaScript errors. Download remained enabled through the real
  sidecar capability. These live checks created no downloads or processing jobs.

## Rollback and separate gates

The previous image (`88078c4`, image
`sha256:bcbcb68835d61680e190659a8335647dfa64c17f0dfa72937b76fc65c3584ce2`)
and protected runtime definitions are retained. Runtime snapshots and private
checksums live under the owner-only `media-manager-navigation-2026-10-11` directory
on the deployment host; no private configuration is committed.

This release changes only web assets and validation references. Comparing the
previous/new processing sources, dependency lock, Dockerfile, and Compose files
showed no differences, so restoring the immediately previous image and its
protected runtime definition uses the same persistence/recovery implementation.
Recreate only `api` and repeat health, authentication, and retained-file checks.
Earlier releases predating shared accepted jobs still require the
[persistence compatibility procedure](deployment.md#rollback).

Physical iPhone acceptance, Telegram setup, and replacement Shortcut installation
remain separate gates described in the [integration release](release-2026-10-10.md).
