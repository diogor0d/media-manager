# On-Device Release Tests

## 1. Scope

These tests gate a signed-file or iCloud-link release of the `Media Manager`
Shortcut. They require a real Apple device, the target iOS version, an isolated
test API deployment, disposable per-device service credentials, and access to
Cloudflare Access and API logs.

Do not put a hostname, client ID, client secret, private media sample, or iCloud
link in this file or in committed test evidence. Redact credentials from screen
recordings and logs.

## 2. Test record

Create one record per tested release outside credential-bearing systems:

| Field | Value |
| --- | --- |
| Shortcut version | |
| Artifact SHA-256 | |
| Authoring macOS version | |
| Test iOS version | |
| Device model | |
| Calling apps tested | |
| API build/version | |
| Access policy revision | |
| Test date | |
| Tester | |
| Signed file result | Pass / Fail |
| iCloud link result | Pass / Fail |
| Known deviations | |

Never record service-token values.

## 3. Static inspection before export

- [ ] The master Shortcut contains three empty configuration `Text` actions.
- [ ] The base URL, client ID, and client-secret import questions have blank
  defaults.
- [ ] No real hostname appears in any `Text`, `URL`, `Comment`, or HTTP action.
- [ ] No client ID or client secret appears as a literal anywhere.
- [ ] Every HTTP header value is a magic-variable reference to the configuration
  actions.
- [ ] Every `Get Contents of URL` action has both exact header names:
  `CF-Access-Client-Id` and `CF-Access-Client-Secret`.
- [ ] There is no `Authorization` header.
- [ ] The create request body is `File`, not Form or JSON.
- [ ] The shared item is uploaded by exactly one POST action.
- [ ] Polling is configured for 120 attempts and five-second waits after the first
  attempt.
- [ ] The ready preview precedes the download action.
- [ ] Cancel, failed, timeout, malformed-response, and normal paths contain the
  required cleanup action where a valid status URL exists.
- [ ] No signed artifact or populated test copy is stored in this repository.

## 4. Clean installation

### 4.1 Signed file

- [ ] Export the blank master for `Anyone` on a Mac.
- [ ] Transfer the exported file without modifying it.
- [ ] Open it on a clean test device.
- [ ] Confirm iOS requires the user to review and add the Shortcut.
- [ ] Confirm no silent installation occurs.
- [ ] Confirm all three setup questions are shown with blank answers.
- [ ] Enter only disposable per-device test configuration.
- [ ] Confirm the Shortcut appears in the Share Sheet for an image, video,
  audio item, and a media file from Files.

### 4.2 iCloud link

- [ ] Create the link from the blank master on an Apple device.
- [ ] Open it on a clean test device or clean test account.
- [ ] Confirm the preview is the intended version.
- [ ] Confirm the user must tap `Get Shortcut`.
- [ ] Confirm all three setup questions are blank.
- [ ] Confirm no test credential or hostname was copied from the author's test
  Shortcut.

## 5. Configuration and Access

- [ ] Empty API base URL stops before any network request.
- [ ] A base URL not beginning with `https://` stops before any request.
- [ ] A base URL ending in `/` is rejected rather than producing a double slash.
- [ ] Empty client ID stops without displaying the client secret.
- [ ] Empty client secret stops without displaying the client ID value.
- [ ] Invalid credentials produce no successful create job and no origin-side
  media processing. Record whether the capabilities action itself surfaces the
  error or returns a redirect/error body; do not assume the unparsed response
  proves authentication.
- [ ] An expired token is rejected.
- [ ] A revoked token is rejected.
- [ ] A client ID without its matching secret is rejected.
- [ ] A secret without its matching client ID is rejected.
- [ ] Access logs attribute the valid run to the expected per-device service
  token.
- [ ] No notification or custom alert contains either credential.

If Cloudflare One Agent is part of the deployment, separately test its device
session and policy behavior. Do not remove either service-token header from this
Shortcut unless the API contract and this specification are deliberately
revised.

## 6. Share Sheet input

- [ ] Running without Share Sheet input produces the configured no-input
  response.
- [ ] Sharing exactly one image proceeds.
- [ ] Sharing exactly one video proceeds.
- [ ] Sharing exactly one audio item proceeds.
- [ ] Sharing exactly one media file from Files proceeds.
- [ ] Sharing two items shows `Select exactly one media file.` and sends no API
  request.
- [ ] Cancelling any option menu before job creation sends no POST and needs no
  DELETE.
- [ ] The input is not locally transcoded, resized, or archived before upload.

Use synthetic, non-private media for all release tests.

## 7. Choice and query conformance

Exercise every value at least once across the test set. Full Cartesian coverage
is not required unless the backend claims every combination is supported.

### 7.1 Targets

- [ ] `video-mp4`
- [ ] `video-webm`
- [ ] `image-jpeg`
- [ ] `image-png`
- [ ] `image-webp`
- [ ] `animation-gif`
- [ ] `audio-m4a`
- [ ] `audio-mp3`
- [ ] `audio-opus`

### 7.2 Qualities

- [ ] `economy`
- [ ] `balanced`
- [ ] `high`

### 7.3 Resolutions

- [ ] `source`
- [ ] `480p`
- [ ] `720p`
- [ ] `1080p`
- [ ] `1440p`
- [ ] `2160p`

The two largest resolutions must be exercised only with non-GIF visual output.
Audio output must always send `source`.

### 7.4 Audio handling

- [ ] `keep`
- [ ] `drop`

For each run, verify the API receives exactly these four query keys once:

```text
target=<selected target>
quality=<selected quality>
resolution=<selected resolution>
audio=<keep|drop>
```

Do not treat an API rejection of a source/target combination as proof that the
Shortcut changed a value. Compare the received query against the selected menu
branches.

- [ ] Audio targets send `resolution=source` and do not show a resolution menu.
- [ ] Non-video targets send `audio=keep` and do not show an audio menu.
- [ ] GIF offers no `1440p` or `2160p` branch.
- [ ] Video targets offer both audio choices and all six resolutions.

## 8. Endpoint and authentication ledger

Use Access/API logs or a purpose-built non-production request recorder to
verify every row. Recorder output must redact header values before retention.

| Sequence | Expected request | Required headers | Body |
| --- | --- | --- | --- |
| 1 | `GET <base>/v1/capabilities` | Both CF-Access headers | None |
| 2 | `POST <base>/v1/jobs?target=<target>&quality=<quality>&resolution=<resolution>&audio=<keep\|drop>` | Both CF-Access headers | Raw shared file |
| 3 | `GET status_url` | Both CF-Access headers | None |
| 4 | Additional `GET status_url` while nonterminal, maximum 120 total | Both CF-Access headers | None |
| 5a | `GET output.download_url` after user chooses Save or Share | Both CF-Access headers | None |
| 5b | No download GET after user chooses Cancel | N/A | N/A |
| 6 | `DELETE status_url` on every handled terminal path | Both CF-Access headers | None |

- [ ] No endpoint other than the five contract endpoint forms is called.
- [ ] The capabilities response is not parsed for undocumented fields.
- [ ] The capabilities request is not treated as proof of authentication unless
  the native action raises an observed error for the tested Access response.
- [ ] `status_url` is used as returned and is not joined to the base URL.
- [ ] `output.download_url` is used as returned and is not joined to the base
  URL.
- [ ] Non-HTTPS returned URLs are rejected before credentials are sent to them.
- [ ] Redirect tests confirm credentials are not exposed to an unintended host.

## 9. Create-response validation

Use controlled test responses or a test-only API mode. Do not add response
fields to the production contract just for the Shortcut.

- [ ] A normal create response reads `id`, `state`, and `status_url`.
- [ ] Missing `status_url` stops without attempting an impossible cleanup.
- [ ] Non-HTTPS `status_url` stops without sending credentials to it.
- [ ] Missing `id` invokes DELETE when a valid status URL is available, then
  stops.
- [ ] Missing `state` invokes DELETE when a valid status URL is available, then
  stops.
- [ ] The initial state is validated, but the first status GET is still made to
  obtain a complete terminal payload.

## 10. Polling behavior

### 10.1 Immediate ready

- [ ] The first status GET happens without an initial five-second wait.
- [ ] `ready` prevents all subsequent Repeat iterations from issuing requests.
- [ ] The ready response becomes `Last Status Dictionary`.

### 10.2 Delayed ready

- [ ] A nonterminal state triggers a five-second wait before the next GET.
- [ ] Unknown nonterminal state text is polled again rather than interpreted as
  ready or failed.
- [ ] A later `ready` state stops further requests.
- [ ] Total status GET count never exceeds 120.

### 10.3 Failed

- [ ] Exact lowercase `failed` stops further status requests.
- [ ] DELETE is requested once.
- [ ] The alert contains the job ID and no invented error field.
- [ ] No download request occurs.

### 10.4 Timeout

- [ ] Keep the job nonterminal for all 120 status responses.
- [ ] Confirm exactly 120 status GETs.
- [ ] Confirm there are 119 waits of approximately five seconds.
- [ ] Confirm DELETE follows the final nonterminal response.
- [ ] Confirm no download request occurs.

### 10.5 Malformed status

- [ ] A status response without `state` requests cleanup and stops.
- [ ] A network or HTTP failure demonstrates the documented limitation that
  native action termination may skip later cleanup.
- [ ] The server's independent expiration/cleanup behavior is verified outside
  this client test because client DELETE is not guaranteed.

## 11. Ready-field validation

For a valid ready response, confirm the Shortcut reads exactly:

- [ ] `input.bytes`
- [ ] `output.bytes`
- [ ] `output.filename`
- [ ] `output.media_type`
- [ ] `output.download_url`

Run one negative test for each missing path:

- [ ] Missing `input`
- [ ] Missing `input.bytes`
- [ ] Missing `output`
- [ ] Missing `output.bytes`
- [ ] Missing `output.filename`
- [ ] Missing `output.media_type`
- [ ] Missing `output.download_url`

Each test must request DELETE and must not download. Also verify:

- [ ] A nonnumeric `input.bytes` is rejected and cleaned up.
- [ ] A nonnumeric `output.bytes` is rejected and cleaned up.
- [ ] Native `Get Type` returns exact text `Number` for each valid JSON numeric
  bytes value on the tested iOS version.
- [ ] A non-HTTPS `output.download_url` is rejected before credentials are sent
  to it, and the job is cleaned up through the valid status URL.

## 12. Exact preview and cancellation

Use known fixture sizes and compare exact numeric values. Locale-specific
grouping and decimal separators may change presentation without changing the
value.

- [ ] Input bytes exactly equal API `input.bytes`.
- [ ] Output bytes exactly equal API `output.bytes`.
- [ ] Savings bytes exactly equal `input.bytes - output.bytes`.
- [ ] Savings percentage equals `(savings bytes / input bytes) * 100`, rounded
  only for display to two decimal places.
- [ ] An output larger than its input displays negative savings without
  clamping.
- [ ] A zero-byte input displays exact byte values and `n/a` instead of dividing
  by zero.
- [ ] Filename exactly equals `output.filename`.
- [ ] Media type exactly equals `output.media_type`.
- [ ] The preview explicitly says the output has not been downloaded.
- [ ] Access/API logs confirm no download GET occurred before the menu choice.
- [ ] Choosing Cancel issues DELETE.
- [ ] Choosing Cancel issues no GET to `output.download_url`.
- [ ] If the native menu has a separate system dismissal control, record whether
  using it skips cleanup and list that behavior as a release limitation.

## 13. Download, save, share, and cleanup

### 13.1 Save to Files

- [ ] Choosing Save performs one authenticated GET of `output.download_url`.
- [ ] The complete response is held as a file.
- [ ] `Set Name` applies `output.filename` exactly.
- [ ] `Set Name` uses the extension in `output.filename` rather than preserving
  or appending the download response's previous extension.
- [ ] DELETE occurs after download completes and before the Files picker opens.
- [ ] The Files picker asks for a location.
- [ ] An existing file is not silently overwritten.
- [ ] Saved bytes match the download response.
- [ ] Saved size matches `output.bytes` for a conforming fixture.

### 13.2 Share

- [ ] Choosing Share performs one authenticated GET of `output.download_url`.
- [ ] The shared item uses `output.filename`.
- [ ] DELETE occurs after download completes and before the system share sheet
  opens.
- [ ] The receiving test app gets byte-identical content.
- [ ] Dismissing the system share sheet does not cause another download or
  DELETE.

### 13.3 Delivery failures

- [ ] A download network failure demonstrates and records whether cleanup was
  skipped by native action termination.
- [ ] Cancelling the Files picker after successful download still leaves the
  earlier cleanup request in the logs.
- [ ] Dismissing the system share sheet after successful download still leaves
  the earlier cleanup request in the logs.

## 14. Media and resource coverage

- [ ] Small image over Wi-Fi.
- [ ] Small video over Wi-Fi.
- [ ] Audio input over Wi-Fi.
- [ ] Animated input over Wi-Fi.
- [ ] Supported input over cellular, if cellular use is in scope.
- [ ] Large image near the supported server limit.
- [ ] Large video near the supported server limit.
- [ ] Device locked or app backgrounded during polling.
- [ ] Calling app dismissed during execution.
- [ ] Low-storage behavior during download and Save to Files.
- [ ] The iOS `Allow Sharing Large Amounts of Data` setting is tested both as
  required by the chosen fixtures and documented for users if necessary.

Record observed Share Sheet time, memory, and background limitations. Do not
claim a supported maximum file size from these tests unless the backend and
Apple runtime both define and enforce it.

## 15. Final release decision

- [ ] Every required case passed on each supported iOS version.
- [ ] Failures and skipped cases are documented in release notes.
- [ ] The final artifact checksum matches the tested artifact.
- [ ] The final iCloud link resolves to the tested blank master.
- [ ] A last clean import shows blank configuration questions.
- [ ] The disposable release-test service token is revoked after testing.
- [ ] No credential-bearing evidence is retained in the repository.

If any signing, import, Share Sheet, raw-file upload, polling, authenticated
download, or cleanup behavior has not been exercised on Apple hardware, the
release remains unvalidated and must be described that way.
