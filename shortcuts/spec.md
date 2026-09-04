# iOS Shortcut Specifications

Last updated: 2026-09-04

This document defines two native iOS Shortcuts. Author and test them in Apple's
Shortcuts app; the repository intentionally does not contain a signed
`.shortcut` file or credentials.

## Shared Configuration

Create three empty `Text` actions at the top of each credential-free master and
expose each as an import question:

| Variable | Example format |
| --- | --- |
| `API Base URL` | `https://converter.example.com` |
| `Access Client ID` | Per-device Cloudflare Access service-token ID |
| `Access Client Secret` | Matching per-device secret |

The URL must use HTTPS and have no trailing slash. Every HTTP action sends both
`CF-Access-Client-Id` and `CF-Access-Client-Secret`. Keep import defaults empty.
These values are visible to anyone who can edit the installed Shortcut; they
are not Keychain-backed secret fields.

Before sending credentials to a returned `status_url` or `output.download_url`,
use URL component actions to require the same scheme, host, and effective port
as `API Base URL`. HTTPS alone is insufficient. Reject redirects or returned
URLs to any other origin.

Configure both Shortcuts for the Share Sheet with Images, Media, and Files. If
there is no Shortcut Input, use `Select File`; otherwise require exactly one
input item. Use `Get Details of Files` to obtain its name, then URL-encode that
name as `Upload Filename`.

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

1. Read and validate the three configuration values.
2. Resolve exactly one input file from Shortcut Input or `Select File`.
3. Read and URL-encode the input filename.
4. Use `Choose from Menu` once to set `Target` from the table above.
5. Build `API Base URL/v1/jobs?target=Target` with magic variables.
6. Use `Get Contents of URL` with method `POST`, request body `File`, and the
   original input file. Add the two Access headers and
   `Upload-Filename: Upload Filename`. Do not use Form, JSON, or multipart.
7. Read `id`, `state`, and `status_url`. Require an HTTPS status URL on the exact
   configured API origin.
8. Poll `status_url` immediately, then every three seconds, for at most 200
   requests. Send both Access headers every time. Stop only at exact `ready` or
   `failed`.
9. On `failed`, show the bounded API error message, request `DELETE status_url`,
   and stop.
10. On timeout or malformed data, request `DELETE status_url` when available and
    stop.
11. On `ready`, require `input.bytes`, `output.bytes`, `output.filename`, and an
    same-origin HTTPS `output.download_url`. Show input size, output size, and the measured
    difference before downloading.
12. Ask `Keep this result?`. If no, request `DELETE status_url` and stop.
13. Fetch `output.download_url` with both Access headers, then immediately
    request `DELETE status_url`.
14. Apply `output.filename` with `Set Name` and use `Share`. The native share
    sheet includes Save to Files.

The API validates source/target compatibility. A video cannot become a still
image and an image cannot become audio merely because a menu value was chosen.

## Compress Media

This Shortcut has no conversion menu. The server selects a compact canonical
output and attempts to produce a valid file below 20,000,000 bytes.

### Action Graph

1. Read and validate the shared configuration.
2. Resolve exactly one input file and URL-encode its filename.
3. Build `API Base URL/v1/compressions`.
4. Use `Get Contents of URL` with method `POST`, request body `File`, the two
   Access headers, and `Upload-Filename`. Do not add query parameters.
5. Validate and poll the returned HTTPS `status_url` using the same immediate,
   three-second, 200-request loop as Convert Media.
6. On `ready`, require the normal input/output fields and
   `compression.target_bytes`, `compression.met_target`, and
   `compression.attempts`.
7. Independently compare `output.bytes < compression.target_bytes`; reject a
   response whose comparison disagrees with `compression.met_target`.
8. Show exact input and output sizes. If `met_target` is false, state that the
   server could not produce a valid result below 20 MB and is offering its
   smallest valid candidate. Do not call the operation failed.
9. Ask `Keep this result?`, then download, delete, rename, and share exactly as
   in Convert Media.

The fixed threshold uses decimal MB: success means strictly less than
20,000,000 bytes. The working aim is 19,000,000 bytes. Neither the Shortcut nor
the API guarantees that every source can meet the threshold.

## Failure and Cleanup

Native Shortcuts has no portable `finally` action. A killed process, HTTP action
failure, or system cancellation can stop before `DELETE`; server expiry remains
authoritative. Never send credentials to a returned URL that is not HTTPS, and
test redirect behavior against the actual Cloudflare Access policy before
release.
