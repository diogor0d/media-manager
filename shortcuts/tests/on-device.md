# On-Device Release Tests

Last updated: 2026-09-04

Run this gate on each supported iOS version with an isolated deployment,
synthetic media, and a disposable per-device service token. Never retain token
values, private media, or a private iCloud link in test evidence.

## Release Record

| Field | Value |
| --- | --- |
| Convert Media version | |
| Compress Media version | |
| Artifact SHA-256 values | |
| iOS and device | |
| API revision/image digest | |
| Access policy revision | |
| Test date and tester | |
| Signed-file result | Pass / Fail |
| iCloud-link result | Pass / Fail |
| Deviations | |

## Static Inspection

- [ ] Both masters contain three empty configuration Text actions and blank
  import-question defaults.
- [ ] No real hostname, credential, Apple account data, or private media exists
  in either master or artifact.
- [ ] Every HTTP action uses magic-variable references for both Access headers.
- [ ] Both create requests use a raw File body and an encoded `Upload-Filename`.
- [ ] Convert Media sends only `target`; Compress Media sends no query.
- [ ] Polling is bounded to 200 status requests with three-second waits.
- [ ] Every handled failure, rejection, timeout, and discard path requests
  cleanup when a valid status URL exists.

## Installation and Authentication

- [ ] Signed-file and iCloud-link installation each require user review.
- [ ] All three setup questions are blank on a clean device.
- [ ] Missing, malformed, expired, revoked, and mismatched configuration fails
  without exposing the secret.
- [ ] Access logs attribute a valid request to the expected per-device token.
- [ ] Returned non-HTTPS or cross-origin redirect destinations never receive
  credentials.

## Input and Delivery

- [ ] Share Sheet input works for one image, video, audio item, and Files item.
- [ ] Running directly opens Select File.
- [ ] Zero, cancelled, and multiple inputs do not create a job.
- [ ] Ready preview appears before download and shows exact server byte counts.
- [ ] Discard performs no content download.
- [ ] Keep downloads once, deletes the job, applies the API filename, and opens
  the native share sheet.
- [ ] Save to Files and another share destination receive byte-identical output.
- [ ] Backgrounding, locking, cellular use, low storage, and a dismissed share
  sheet have documented observed behavior.

## Convert Media

- [ ] Each of the nine target menu values is sent unchanged in a separate test.
- [ ] No quality, resolution, or audio prompt appears.
- [ ] The API receives default `quality_percent=50`, `resolution=source`, and
  `audio=keep` in its job representation.
- [ ] Compatible source/target pairs complete; incompatible pairs show the
  bounded API error and clean up.
- [ ] Progress handles queued, inspecting, converting, and validating states.

## Compress Media

- [ ] No output or quality menu appears.
- [ ] Video returns MP4, still image returns WebP, and audio returns M4A.
- [ ] A result below 20,000,000 bytes reports `met_target=true`.
- [ ] A controlled result of exactly 20,000,000 bytes reports false.
- [ ] A valid result above the threshold is offered with an explicit warning,
  not treated as a failed job.
- [ ] Displayed `met_target` agrees with a local output-byte comparison.
- [ ] Attempt count remains between one and five.
- [ ] Cancellation during repeated attempts stops processing and cleanup removes
  candidate files.

## Release Decision

- [ ] Both artifacts match the reviewed action graphs in `shortcuts/spec.md`.
- [ ] Artifact hashes match the versions tested on a clean device.
- [ ] Known iOS/runtime limitations accompany the release.
- [ ] The disposable test token is revoked after testing.

Until these tests run on Apple hardware, the Shortcut release is unvalidated.
