# iOS Shortcut Specifications

Last updated: 2026-10-09

This document defines two native iOS Shortcuts. Author and test them in Apple's
Shortcuts app; the repository intentionally does not contain a signed
`.shortcut` file or credentials.

## Shared Configuration

Resolve Shortcut Input before configuration actions, then create three empty
`Text` actions in each credential-free master and expose each as an import
question:

| Variable | Example format |
| --- | --- |
| `API Base URL` | `https://converter.example.com` |
| `Access Client ID` | Per-device Cloudflare Access service-token ID |
| `Access Client Secret` | Matching per-device secret |

The URL must use HTTPS and have no trailing slash. Every HTTP action sends both
`CF-Access-Client-Id` and `CF-Access-Client-Secret`. Keep import defaults empty.
These values are visible to anyone who can edit the installed Shortcut; they
are not Keychain-backed secret fields.

Construct request destinations from the validated `API Base URL` and a
validated job UUID. Require returned `status_url` and `output.download_url`
values to equal those canonical destinations exactly. Never request a URL
supplied by the response directly. The native HTTP action's redirect behavior
still requires device testing; these checks do not establish that headers are
withheld from an HTTP redirect. A prepared job remains server-owned until it
expires or the user chooses an explicit Discard action.

Configure both Shortcuts for the Share Sheet with Images, Media, and Files. If
there is no Shortcut Input, show a source menu with `Photos` first and `Files`
second, then use the corresponding native single-item picker. Otherwise require
exactly one input item. Use native `Get Name` with the selected file as its
explicit input, then URL-encode that name as `Upload Filename`. Cherri 2.3.0's
file-details action leaves its native file input empty; direct Get Name avoids
that compiler defect.

## Convert Media

The fast path asks one question only: output format. Quality defaults to 50%,
resolution defaults to source, and sound is kept when the target supports it.

### Output Menu

| Menu label | `Target` value |
| --- | --- |
| MP4 | `video-mp4` |
| WebM | `video-webm` |
| JPEG | `image-jpeg` |
| PNG | `image-png` |
| WebP | `image-webp` |
| GIF | `animation-gif` |
| M4A | `audio-m4a` |
| MP3 | `audio-mp3` |
| Opus | `audio-opus` |

### Action Graph

1. Resolve exactly one input file from Shortcut Input or the Photos/Files source
   menu.
2. Read and validate the three configuration values.
3. Read and URL-encode the input filename.
4. Use `Choose from Menu` once to set `Target` from the table above.
5. Build `API Base URL/v1/jobs?target=Target&prepare_only=true` with magic variables.
6. Use `Get Contents of URL` with method `POST`, request body `File`, and the
   original input file. Add the two Access headers and
   `Upload-Filename: Upload Filename`. Do not use Form, JSON, or multipart.
7. Read `id`, `state`, and `status_url`; require `awaiting_review` and an HTTPS
   status URL on the exact configured API origin.
8. POST `{}` to the locally constructed `API Base URL/v1/jobs/id/estimate`.
   Show the fast
   size and time ranges, then let the user Start, Refine estimate, or Discard.
   Refinement is an optional GET of the same `/estimate` route; show the
   returned ranges and ask Start or Discard again.
9. For Start, POST `{}` to the locally constructed `API Base URL/v1/jobs/id/start`. Require
   the same job ID and status URL and the `queued` state.
10. Poll `status_url` immediately, then every three seconds, for at most 200
    requests. Send both Access headers every time. Stop only at exact `ready` or
    `failed`.
11. On `failed`, timeout, or malformed data, show the bounded error and stop.
    Do not issue an automatic DELETE.
12. On `ready`, require `input.bytes`, `output.bytes`, `output.filename`, and a
    same-origin HTTPS `output.download_url`. Show decimal MB input/output sizes
    and the signed size change in MB and percent before downloading.
13. Ask `Keep this result?`. If no, request `DELETE status_url` and stop.
14. After a short handoff delay, fetch `output.download_url` with both Access
    headers and apply `output.filename` with `Set Name` to materialize the file.
15. Use `Share`. The native share sheet includes Save to Files. Keeping and
    sharing a result does not send DELETE; expiry remains authoritative.

The API validates source/target compatibility. A video cannot become a still
image and an image cannot become audio merely because a menu value was chosen.

## Compress Media

This Shortcut has no conversion menu. The server selects a compact canonical
output and attempts to produce a valid file below 20,000,000 bytes.

### Action Graph

1. Resolve exactly one input file before configuration actions.
2. Read and validate the shared configuration, then URL-encode the input filename.
3. Build `API Base URL/v1/compressions?prepare_only=true`.
4. Use `Get Contents of URL` with method `POST`, request body `File`, the two
   Access headers, and `Upload-Filename`.
5. Validate `awaiting_review` and the returned HTTPS `status_url`. POST `{}` to
   `API Base URL/v1/jobs/id/estimate`, show the fast ranges, and offer
   Start, optional Refine estimate, or explicit Discard as in Convert Media.
6. For Start, POST `{}` to `API Base URL/v1/jobs/id/start`, require `queued`,
   then poll the same HTTPS status URL using the immediate, three-second,
   200-request loop.
7. On `ready`, require the normal input/output fields and
   `compression.target_bytes`, `compression.met_target`, and
   `compression.attempts`.
8. Independently compare `output.bytes < compression.target_bytes`; reject a
   response whose comparison disagrees with `compression.met_target`.
9. Show exact input and output sizes. If `met_target` is false, state that the
   server could not produce a valid result below 20 MB and is offering its
   smallest valid candidate. Do not call the operation failed.
10. Ask `Keep this result?`, then download, rename, and share exactly as in
    Convert Media. DELETE is sent only when the user explicitly chooses Discard.

The canonical output is MP4 for video and animated GIF, WebP for still images,
and M4A for audio. GIF compression therefore preserves animation as video;
it does not return a smaller GIF.

The fixed threshold uses decimal MB: success means strictly less than
20,000,000 bytes. The working aim is 19,000,000 bytes. Neither the Shortcut nor
the API guarantees that every source can meet the threshold.

## Failure and Cleanup

Native Shortcuts has no portable `finally` action. A killed process, HTTP action
failure, or system cancellation can stop before `DELETE`; server expiry remains
authoritative. Never send credentials to a returned URL that is not HTTPS, and
test redirect behavior against the actual Cloudflare Access policy before
release.
