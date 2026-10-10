# API Contract

Last updated: 2026-10-09

All `/v1/` responses are private and non-cacheable. In the deployed
configuration, Cloudflare Access authenticates requests and each job belongs to
the authenticated principal. Configured principal aliases can map a service
identity to a user owner; aliases are described in [workflows.md](workflows.md).
Upload bodies are raw media, never multipart.

## Capabilities and conversion

`GET /v1/capabilities` describes supported targets/options, upload and chunk
limits, the default result TTL, compression target, and whether the MeTube
download bridge is enabled.

`POST /v1/jobs?target=<target>` accepts one raw media body and creates a
conversion. Optional query parameters are `quality_percent=0..100`,
`resolution=source|480p|720p|1080p|1440p|2160p`, and `audio=keep|drop`.
Defaults are 50%, source resolution, and preserved audio. Legacy
`quality=economy|balanced|high` maps to 0, 50, and 100. Target-specific
constraints are validated by the server.

For large files, reserve a resumable session with `POST /v1/uploads` and
`Upload-Length`, then send ordered `PATCH /v1/uploads/{id}` requests with
`Upload-Offset` and `application/offset+octet-stream`. `GET /v1/uploads/{id}`
returns the authoritative offset. Each chunk is bounded by
`max_chunk_bytes` from capabilities. `POST /v1/uploads/{id}/complete` accepts
the file and queues it immediately. These upload sessions are held in memory
and temporary files while this server process is running; a process restart
does not recover an incomplete upload. The upload-session expiry is separate
from result retention.

Clients that need a review step can use `?prepare_only=true` on the one-shot
`POST /v1/jobs` endpoint, or call `POST /v1/uploads/{id}/prepare` after all
chunks are present. The response is an `awaiting_review` job. The accepted
source is then persisted and survives restart subject to the owner's retention
period, seven days by default. Partial uploads still use the shorter upload-session
expiry. Inspect content metadata with `GET /v1/jobs/{id}/inspect` (also available
under `/v1/compressions/{id}/inspect`). Create or refresh an estimate with
`POST /v1/jobs/{id}/estimate`; fetch
the latest estimate with `GET /v1/jobs/{id}/estimate`; start with
`POST /v1/jobs/{id}/start`. Estimate request bodies use the options shape in
the compression section below. Starting is a separate explicit action.

## Compression

`POST /v1/compressions` accepts a raw image, animation, video, or audio file.
It rejects query options except `prepare_only=true`. Without that flag the
server uses default compression settings and queues immediately. For review,
create a prepared compression using `?prepare_only=true`, then send
`POST /v1/compressions/{id}/estimate` and `POST /v1/compressions/{id}/start`.
Estimate and start requests accept a JSON `ProcessingRequest`:

```json
{
  "compression_options": {
    "target_bytes": 20000000,
    "target": null,
    "resolution": "source",
    "audio": "keep",
    "quality_percent": 50
  }
}
```

`target_bytes` must be positive and cannot exceed the configured output
capacity. `target` selects a supported output format or remains `null` for
automatic selection. `resolution` caps video/image size without upscaling;
`audio` keeps or drops audio where applicable; `quality_percent` ranges from 0
to 100. Clients may also send `conversion_options` when they want to select a
conversion target and use the same request for the estimate/start flow. The
server validates option/target compatibility. With both option sets supplied,
the conversion options select the output format; compression options control
quality, resolution, audio, and target size. This is one compression operation
using the original source for every candidate, without an intermediate encode.

Automatic output selection uses H.264/AAC MP4 for video and animation, WebP for
still images, and AAC in M4A for audio. The processor searches a bounded ladder
of at most five candidates and returns the smallest valid candidate if none
meets the selected target. The default target is 20,000,000 bytes; for this
default the internal working aim is below 19,000,000 bytes. A size target is an
attempt, not a guarantee. The complete search has a 15-minute wall-clock limit
in addition to encoder limits.

Estimate responses contain `method` (`fast` or `sampled`), minimum/maximum
size and optional ETA, `confidence` (`low` or `medium`), `status` (`pending`,
`ready`, or `fallback`), inspected `source`, and `options_key`. Estimates are
ranges, not promises. Refinement is bounded; a fast fallback remains available
if sampling cannot finish. A null ETA means time could not be estimated
reliably. Call the estimate endpoint again to obtain the latest result; changing
options produces a different options key.

Compression status, content, and deletion use:

```text
GET    /v1/compressions/{id}
GET    /v1/compressions/{id}/content
DELETE /v1/compressions/{id}
```

`compression.met_target` reports whether the actual output met the requested
byte ceiling; output byte counts are authoritative.

## MeTube downloads and processing

When `downloads_enabled` is true, `POST /v1/downloads` accepts a JSON
`DownloadRequest` with a source URL and optional `quality`, `format`,
`download_type`, `codec`, `workflow`, `auto_process`, `conversion_options`,
and `compression_options`. Workflow values are `download`, `convert`,
`compress`, and `convert_compress`. The route returns `202` and a download ID.
Poll `GET /v1/downloads/{id}`. A download-only request becomes `ready`; a
requested processing workflow becomes `awaiting_review` unless
`auto_process=true`. Use `POST /v1/downloads/{id}/process` with a
`ProcessingRequest` to prepare a child job. Fetch the source via
`GET /v1/downloads/{id}/content` or delete it with `DELETE
/v1/downloads/{id}`. A processed job records the download as its `parent_id`;
the download response exposes its `child_id` when one is prepared. Download and
child results have separate status/content/retention records.

The source URL is not written to the local download registry or Telegram
session state. The companion sidecar patch adds optional UUID `request_id`
correlation while preserving existing clients. An ambiguous submission is polled
by that ID and is never blindly resubmitted. Install a sidecar version with this
contract before enabling the bridge. See [workflows.md](workflows.md) for setup
and current validation status.

## History and common behavior

`GET /v1/history?limit=50&offset=0` merges the authenticated owner's downloads,
conversions, and compressions, newest first. `limit` accepts 1–100; `offset`
must be nonnegative. Each entry identifies its kind and contains the existing
job/download view. `GET /v1/history/settings` returns the owner's retention
period, seven days by default. `PUT /v1/history/settings` accepts
`{"retention_days": 1..90}`. Preferences survive restart and affect future
completion expiry dates; they do not rewrite existing expiry dates. The
`MEDIA_MANAGER_RESULT_TTL_SECONDS` setting controls the default when no owner
preference is saved.

Accepted prepared, queued, or processing jobs survive restart. Interrupted
processing jobs are requeued at most twice; after that the job fails with a
recovery-limit error. Incomplete uploads and unaccepted sessions are not
recovered. Completed results, safe metadata, and history settings are retained
until expiry or explicit deletion. The default is seven days. Sources are
removed after processing. Retained records do not count against the active
queue limit.

Creation normally returns `202`; prepared jobs return `awaiting_review` until
started. Poll the returned `status_url` until `ready` or `failed`. Progress
stages are `queued`, `inspecting`, `converting`, and `validating`. Percentage
is omitted when duration is unknown, remains below 100 during encoding, and
reaches 100 only after validation.

Clients may send a percent-encoded basename in `Upload-Filename`. It is display
metadata only and never influences probing, FFmpeg arguments, or filesystem
paths. Result names are `<stem>-converted.<ext>` or `<stem>-compressed.<ext>`.
Ready responses provide authoritative input/output byte counts and a content
URL. Content is unavailable before readiness. `DELETE` cancels or discards a
job; server expiry remains authoritative when a client disappears.
