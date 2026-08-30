# Security Model

## Trust boundary

Uploaded media is hostile. A valid Cloudflare Access identity is authorization
to use a constrained converter, not evidence that its files are safe.

The intended request path is:

```text
untrusted media -> Cloudflare Access -> Tunnel -> loopback origin -> API -> FFmpeg
```

Cloudflare limits public reachability and authenticates the client. Origin JWT
validation protects against forged forwarded identity headers and calls from
other Docker or host processes that do not have a valid Access assertion.

## Enforced invariants

- Authentication defaults to Cloudflare mode and fails startup without exact
  issuer, audience, and HTTPS public-origin configuration.
- Access JWTs require RS256, a valid signature, issuer, audience, and expiry.
- Jobs are bound to the JWT `sub` or service-token `common_name` claim.
- Uploads stream to generated `0600` paths and are counted independently of
  `Content-Length`.
- Compressed HTTP bodies, multipart uploads, empty bodies, oversized bodies,
  excessive upload time, and a full queue are rejected and cleaned up.
- MIME type, source filename, extension, and metadata never select a parser or
  command.
- Input demuxers and protocols are allowlisted. Playlists, concat manifests,
  URLs, and arbitrary filesystem paths are not accepted API inputs.
- FFmpeg receives a fixed argv vector through `create_subprocess_exec`; no shell
  or user-provided codec, filter, mapping, bitrate, path, or flag is used.
- Stream count, duration, dimensions, frame rate, audio channels, sample rate,
  probe time, conversion time, and output size are bounded.
- Only validated stream indexes are mapped. Input streams are decoded and
  re-encoded; stream copy is never used.
- Source metadata, chapters, subtitles, data streams, attachments, and original
  filenames are not retained in the output contract.
- Completed output is probed again and its container, codec, stream count, and
  byte size are validated before it becomes downloadable.
- Input and partial files are removed on completion, rejection, cancellation,
  timeout, failure, startup, and expiry.
- Captured FFmpeg output is bounded and never returned to clients. Access
  headers, media content, source filenames, and raw FFmpeg errors are not logged.

## Container controls

The prepared Compose service runs as UID/GID `10001`, with a read-only root
filesystem, all Linux capabilities dropped, `no-new-privileges`, Docker's
default seccomp profile, bounded PIDs/logs/files, two CPUs, 2 GiB RAM with no
additional swap allowance, a small no-exec `/tmp`, and a single worker.

The host publication is IPv4 loopback only. No Docker socket, host namespace,
device, persistent user data, database, broker, or privileged helper is present.
Outbound HTTPS remains available because Access signing keys must be refreshed.

## Residual risk

- FFmpeg is a large native parser/decoder attack surface. A decoder exploit
  could execute as the unprivileged container user. Container controls reduce,
  but do not eliminate, kernel and network attack paths.
- `-protocol_whitelist file` blocks normal network protocols; it is not an
  egress firewall after native-code compromise. Stronger isolation would put
  conversion in a separate no-network worker sandbox or VM.
- The named Docker work volume has no portable hard quota. Application bounds
  cap ordinary files to approximately 768 MiB at four live jobs, but production
  still needs quota/capacity verification and alerting.
- The container installs the current Debian FFmpeg security package when built.
  The Python and uv bases are digest-pinned, but Debian package repository state
  is not immutable. Record the final image digest, package inventory, and build
  date; rebuild promptly for FFmpeg and base-image security updates.
- FFmpeg-only image conversion has unverified fidelity for HEIC orientation,
  Display P3/ICC, CMYK JPEG, alpha-to-JPEG background color, unusual TIFFs, and
  animated WebP/APNG. These formats are not contractual until fixture-tested.
- Cloudflare service tokens are bearer-equivalent machine credentials. Native
  Shortcuts import answers remain visible in the Shortcut editor and are not a
  Keychain-backed secret field.
- A client can terminate before sending `DELETE`; server-side expiry is the
  authoritative cleanup mechanism.
- Exact size preview is available only after conversion. CPU has already been
  spent even if the user declines the download.

## Explicit non-goals

- Durable, resumable, batch, or multi-host jobs.
- URL imports, archives, documents, SVG/PostScript/PDF, camera RAW, subtitles,
  DRM, stream copy, arbitrary transforms, or hardware acceleration.
- Retaining EXIF/GPS metadata or original filenames.
- Guaranteeing that a conversion is smaller or visually lossless.
- Claiming support for every demuxer or decoder in the installed FFmpeg build.

## Security verification

Before deployment, verify at minimum:

1. Missing, invalid, expired, wrong-audience, and wrong-issuer assertions fail.
2. A valid service token succeeds and a no-token request is denied at the edge.
3. Jobs are invisible across principals.
4. Oversized fixed-length and streamed uploads leave no files.
5. HLS, concat, malformed, excessive-dimension, excessive-duration, and
   excessive-stream fixtures fail within limits and cause no outbound request.
6. Cancellation and timeout terminate every FFmpeg process and remove partials.
7. Outputs contain only the expected streams/codecs and no source metadata.
8. The container has no public/LAN/IPv6 listener, capabilities, writable root,
   Docker socket, device, or unexpected egress requirement.
9. Image and dependency scanning findings are reviewed against the actual image
   digest; a clean scan is not treated as proof of safety.
