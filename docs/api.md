# API Contract

Last updated: 2026-09-04

All `/v1/` responses are private and non-cacheable. Production clients must be
authenticated by Cloudflare Access; jobs are visible only to the principal that
created them. Upload bodies are raw media, never multipart.

## Conversion

`POST /v1/jobs?target=<target>` creates a one-request conversion. Optional
parameters are `quality_percent=0..100`, `resolution`, and `audio`. Omitting
them selects 50%, source resolution, and preserved audio. Legacy
`quality=economy|balanced|high` maps to 0, 50, and 100.

Large browser files use the resumable sequence:

```text
POST  /v1/uploads
PATCH /v1/uploads/{id}
POST  /v1/uploads/{id}/complete
```

Creation requires `Upload-Length`; patches require `Upload-Offset` and use
`application/offset+octet-stream`. Chunks are ordered and bounded by the
capability response.

## Compression

`POST /v1/compressions` accepts one raw image, animation, video, or audio file
and rejects all query options. It chooses a canonical output:

| Input | Output |
| --- | --- |
| Video or animation | H.264/AAC MP4 |
| Still image | WebP |
| Audio | AAC in M4A |

The server tries an ordered, bounded ladder of at most five quality/resolution
profiles. It stops at the first valid output at or below the 19,000,000-byte
working aim. If no attempt is below 20,000,000 bytes, it returns the smallest
valid candidate with `compression.met_target=false`. The strict success test is
`output.bytes < 20000000`; this is an attempt, not a guarantee. The complete
search has a 15-minute wall-clock limit in addition to each encoder's limit.

Compression status, content, and deletion use:

```text
GET    /v1/compressions/{id}
GET    /v1/compressions/{id}/content
DELETE /v1/compressions/{id}
```

## Common Behavior

Creation returns `202`; clients poll `status_url` until `ready` or `failed`.
Processing progress contains `queued`, `inspecting`, `converting`, and
`validating` stages. Timeline percentage is omitted when duration is unknown,
never reaches 100 during encoding, and reaches 100 only during validation.

Clients may send a percent-encoded basename in `Upload-Filename`. It is display
metadata only. Result names are `<stem>-converted.<ext>` or
`<stem>-compressed.<ext>` and never influence probing, FFmpeg arguments, or
filesystem paths.

Ready responses report authoritative input/output byte counts and a content
URL. Content is unavailable before readiness. `DELETE` cancels or discards a
job; server expiry remains authoritative when a client disappears.
