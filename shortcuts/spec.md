# Native Shortcut Specification

## 1. Purpose and authority

This document is the authoritative action-by-action specification for the
`Media Manager` iOS Shortcut. It defines only behavior supported by the supplied
API contract. Where the contract supplies no schema or behavior, the Shortcut
does not infer one.

The Shortcut accepts one shared media item, collects conversion options,
creates a job with one raw-file upload, polls a bounded number of times, shows
the exact size result before downloading, lets the user cancel, downloads and
saves or shares the result, and requests server-side cleanup.

## 2. Fixed API contract

Every HTTP request in this specification has these two headers, with values
inserted as magic variables from the configuration actions:

```text
CF-Access-Client-Id: Client ID
CF-Access-Client-Secret: Client Secret
```

Do not add an `Authorization` header. Do not put either credential in a URL,
query parameter, request body, notification, error message, or comment.

| Purpose | Method and URL | Request body | Response used by the Shortcut |
| --- | --- | --- | --- |
| Header-bearing preflight | `GET <base>/v1/capabilities` | None | Connectivity check; this release keeps a tested static option graph |
| Create job | `POST <base>/v1/jobs?target=<target>&quality=<quality>&resolution=<resolution>&audio=<keep\|drop>` | Raw File | `id`, `state`, `status_url` |
| Read job | `GET status_url` | None | `state`; ready response fields listed below |
| Download output | `GET output.download_url` | None | File bytes |
| Clean up job | `DELETE status_url` | None | No response fields are assumed |

A ready status response contains these nested fields:

```text
input.bytes
output.bytes
output.filename
output.media_type
output.download_url
```

Interpret both `bytes` fields as JSON numbers, both URL fields as absolute HTTPS
URLs, and all other listed fields as text. These types are required for the
native calculations and requests. The Shortcut does not read an error message,
progress value, capability list, or any other undocumented field.

## 3. Shortcut details

Configure the Shortcut itself as follows:

| Setting | Value |
| --- | --- |
| Name | `Media Manager` |
| Show in Share Sheet | On |
| Accepted input types | Images, Media, and Files |
| If there is no input | Stop and Respond |
| No-input response | `Share exactly one media file with Media Manager.` |

The runtime action graph also counts its input and rejects anything other than
one item. The broad input types allow the API, rather than undocumented local
type inference, to decide whether a source and target combination is valid.

Use native `Comment` actions for the section labels in this document. Comments
must never contain a hostname or credential.

## 4. Configuration actions and import questions

Create these actions at the beginning of the Shortcut. Keep all three `Text`
fields and all three import-question defaults blank in the distributable
master.

### 4.1 API base URL

1. Add a `Text` action with an empty body.
2. Add an import question to that Text parameter.
3. Set the question to `API base URL (HTTPS, no path or trailing slash)`.
4. Leave Default Answer empty.
5. Add `Set Variable` and name it `API Base URL`.

### 4.2 Client ID

1. Add a second `Text` action with an empty body.
2. Add an import question to that Text parameter.
3. Set the question to `Per-device CF-Access client ID`.
4. Leave Default Answer empty.
5. Add `Set Variable` and name it `Client ID`.

### 4.3 Client secret

1. Add a third `Text` action with an empty body.
2. Add an import question to that Text parameter.
3. Set the question to `Per-device CF-Access client secret`.
4. Leave Default Answer empty.
5. Add `Set Variable` and name it `Client Secret`.

### 4.4 Local polling constants

1. Add a `Number` action with `120`; set variable `Maximum Poll Attempts`.
2. Add a `Number` action with `5`; set variable `Poll Interval Seconds`.

These constants produce at most 120 status GET requests. The first request is
immediate and the remaining 119 waits are five seconds each, for a nominal
595-second polling window plus HTTP request time.

### 4.5 Configuration validation

Add these `If` checks before any HTTP action:

1. If `API Base URL` does not have any value, show alert `API base URL is not
   configured.` and use `Stop This Shortcut`.
2. If `API Base URL` does not begin with `https://`, show alert `API base URL
   must use HTTPS.` and stop.
3. If `API Base URL` ends with `/`, show alert `Remove the trailing slash from
   the API base URL.` and stop.
4. If `Client ID` does not have any value, show alert `Client ID is not
   configured.` and stop.
5. If `Client Secret` does not have any value, show alert `Client secret is not
   configured.` and stop.

Do not include either credential's value in an alert.

## 5. HTTP action template

For every `Get Contents of URL` action below, expand the action and add these
header rows manually:

| Header name | Header value |
| --- | --- |
| `CF-Access-Client-Id` | `Client ID` magic variable |
| `CF-Access-Client-Secret` | `Client Secret` magic variable |

Repeat the rows in every preflight, create, status, download, and cleanup
action. Never type a credential literal into one of those rows.

For GET and DELETE requests, configure no request body. For the create POST,
select request body type `File` and supply the shared item. Do not select Form
or JSON and do not manually set `Content-Type`.

## 6. Validate Share Sheet input

1. Add `Count` configured to count Items in `Shortcut Input`.
2. Add `If` configured as `Count is not 1`.
3. In that branch, show alert `Select exactly one media file.` and stop.
4. After the branch, add `Get Item from List`, choose `First Item`, and use
   `Shortcut Input` as the list.
5. Set the result as variable `Upload File`.

Do not transform, resize, encode, archive, or otherwise read the complete file
before the POST. `Upload File` is the raw request body.

## 7. Authenticated capabilities preflight

1. Add a `URL` action containing the `API Base URL` magic variable immediately
   followed by `/v1/capabilities`.
2. Set the result as `Capabilities URL`.
3. Add `Get Contents of URL` using `Capabilities URL`.
4. Set Method to `GET`.
5. Add both headers from section 5.
6. Add no body and do not parse the response.

The API publishes its option schema, but this native release deliberately uses
the static, release-tested graph below. This request is a header-bearing
connectivity preflight, not a definitive authentication assertion: an Access
redirect or error body might not raise a native action error. Revise the native
menus whenever the API's versioned capabilities change.

## 8. Collect conversion choices

Use `Choose from Menu` so users see readable labels while variables contain only
contract values. Each menu branch contains a `Text` action with the exact value
in the right column followed by `Set Variable` using the variable name shown in
the subsection. Conditional branches below prevent option combinations the API
rejects.

### 8.1 Target

Prompt: `Choose the output format.`

Variable: `Target`

| Menu label | Exact value |
| --- | --- |
| MP4 video | `video-mp4` |
| WebM video | `video-webm` |
| JPEG image | `image-jpeg` |
| PNG image | `image-png` |
| WebP image | `image-webp` |
| GIF animation | `animation-gif` |
| M4A audio | `audio-m4a` |
| MP3 audio | `audio-mp3` |
| Opus audio | `audio-opus` |

### 8.2 Quality

Prompt: `Choose output quality.`

Variable: `Quality`

| Menu label | Exact value |
| --- | --- |
| Economy | `economy` |
| Balanced | `balanced` |
| High | `high` |

### 8.3 Resolution

Variable: `Resolution`

1. If `Target` begins with `audio-`, add `Text` containing `source` and set
   `Resolution`. Do not show a resolution menu.
2. Otherwise, if `Target` is `animation-gif`, show prompt `Choose GIF
   resolution.` with `source`, `480p`, `720p`, and `1080p` only.
3. Otherwise show prompt `Choose output resolution.` with the full table below.

| Menu label | Exact value |
| --- | --- |
| Keep source resolution | `source` |
| 480p | `480p` |
| 720p | `720p` |
| 1080p | `1080p` |
| 1440p | `1440p` |
| 2160p | `2160p` |

### 8.4 Audio

Prompt: `Choose audio handling.`

Variable: `Audio`

Show this menu only if `Target` is `video-mp4` or `video-webm`. For every other
target, add `Text` containing `keep` and set `Audio` without showing a menu.

| Menu label | Exact value |
| --- | --- |
| Keep audio | `keep` |
| Drop audio | `drop` |

Every request still sends all four query keys. The conditional defaults above
match the API contract: audio output requires `resolution=source`; only video
output accepts `audio=drop`; and GIF output is limited to 1080p.

## 9. Create the job with one upload

### 9.1 Build the URL

Add a `URL` action with this exact structure and magic variables in the
placeholder positions:

```text
API Base URL/v1/jobs?target=Target&quality=Quality&resolution=Resolution&audio=Audio
```

In the native action, `API Base URL`, `Target`, `Quality`, `Resolution`, and
`Audio` above are magic-variable tokens, not literal words. Set the resulting
variable to `Create Job URL`.

The option values are closed enumerations containing only URL-safe characters.
Do not add any query key, omit a key, reorder work into a second upload, or add
the filename to the URL.

### 9.2 Send the request

Add `Get Contents of URL` configured as follows:

| Setting | Value |
| --- | --- |
| URL | `Create Job URL` |
| Method | `POST` |
| Headers | Both rows from section 5 |
| Request Body | `File` |
| File | `Upload File` magic variable |

Set the result as `Create Job Response`. This must be the only upload of the
input file.

### 9.3 Parse and validate the create response

1. Add `Get Dictionary from Input` for `Create Job Response`; set `Create Job
   Dictionary`.
2. Add `Get Dictionary Value` for key `id`; set `Job ID`.
3. Add `Get Dictionary Value` for key `state`; set `Create State`.
4. Add `Get Dictionary Value` for key `status_url`; set `Status URL`.
5. If `Status URL` has no value, show alert `Create-job response has no
   status_url.` and stop. Cleanup is impossible without that URL.
6. If `Status URL` does not begin with `https://`, show alert `The returned
   status URL is not HTTPS. Credentials were not sent to it.` and stop. Do not
   issue DELETE to an untrusted non-HTTPS URL.
7. If `Job ID` has no value, run the cleanup block in section 11, show alert
   `Create-job response has no id.`, and stop.
8. If `Create State` has no value, run the cleanup block, show alert
   `Create-job response has no state.`, and stop.

The initial `state` is validated because the contract requires it. The client
still performs an immediate GET of `status_url` so the final ready payload
always comes from the status endpoint.

## 10. Bounded polling

### 10.1 Initialize state

1. Add `Number` with value `0`; set variable `Terminal State Reached`.
2. Add `Text` containing `Create State`; set variable `Current State`.

Use `0` for false and `1` for true. Do not use an unverified early-break action.
The first Repeat iteration always assigns `Last Status Dictionary` before any
ready-response parsing occurs.

### 10.2 Repeat at most 120 status requests

Add `Repeat` configured for `Maximum Poll Attempts` times. Its body is:

1. Add `If Terminal State Reached is 0` around the rest of this body.
2. Inside it, add `If Repeat Index is greater than 1`.
3. In that nested branch, add `Wait` for `Poll Interval Seconds` seconds.
4. After the wait branch, add `Get Contents of URL` using `Status URL`.
5. Set Method to `GET`, include both headers, and add no body.
6. Convert the response with `Get Dictionary from Input`; set `Last Status
   Dictionary`.
7. Read key `state`; set `Polled State`.
8. If `Polled State` has no value, run the cleanup block, show alert `Status
   response has no state.`, and stop.
9. Set `Current State` to `Polled State`.
10. If `Current State is ready`, add `Number` with `1` and set `Terminal State
    Reached` to it.
11. Otherwise, if `Current State is failed`, add `Number` with `1` and set
    `Terminal State Reached` to it.

Close all conditions and the Repeat. Any state other than exact lowercase
`ready` or `failed` is treated as nonterminal and consumes another attempt.
After a terminal state, later Repeat iterations make no requests and do not
wait.

### 10.3 Handle failed and timed-out jobs

After Repeat:

1. If `Current State is failed`, read key `error` from `Last Status Dictionary`
   into `Error Dictionary`. Read `code` and `message` from it into `Error Code`
   and `Error Message`. Run the cleanup block. If both values are present, show
   alert `Conversion failed: Error Code - Error Message`; otherwise show
   `Conversion failed for job Job ID.`. Stop. Never invent or display another
   server field.
2. If `Current State is not ready`, run the cleanup block, show alert
   `Conversion did not finish after 120 status checks.`, and stop.
3. Continue only when `Current State` is exactly `ready`.

`Job ID` in the alert is a magic variable. Do not display credentials or an
undocumented server error.

## 11. Cleanup block

Where this specification says `run the cleanup block`, duplicate this native
action at that location:

| Action | Setting | Value |
| --- | --- | --- |
| `Get Contents of URL` | URL | `Status URL` |
| `Get Contents of URL` | Method | `DELETE` |
| `Get Contents of URL` | Headers | Both rows from section 5 |
| `Get Contents of URL` | Request body | None |

Do not assume a response schema and do not parse the response. Cleanup is
possible only after a nonempty HTTPS `Status URL` has been obtained.

Native Shortcuts does not provide a portable `finally` block. If an HTTP action
or the process itself terminates unexpectedly, later cleanup actions may not
run. This limitation must remain in release notes and on-device tests.

## 12. Parse the ready response

Use `Last Status Dictionary`, not `Create Job Dictionary`.

1. Read key `input`; set variable `Input Dictionary`.
2. Read key `bytes` from `Input Dictionary`; set `Input Bytes`.
3. Read key `output`; set variable `Output Dictionary`.
4. Read key `bytes` from `Output Dictionary`; set `Output Bytes`.
5. Read key `filename` from `Output Dictionary`; set `Output Filename`.
6. Read key `media_type` from `Output Dictionary`; set `Output Media Type`.
7. Read key `download_url` from `Output Dictionary`; set `Download URL`.

For each required value, add an `If` using `does not have any value`. Inside
each failure branch, run the cleanup block, show alert `Ready response is
missing <field>.`, and stop. Replace `<field>` with exactly one of:

```text
input
input.bytes
output
output.bytes
output.filename
output.media_type
output.download_url
```

After presence validation, add these checks:

1. Add `Get Type` for `Input Bytes`; set `Input Bytes Type`. If that text is not
   exactly `Number`, clean up, show alert `input.bytes is not a number.`, and
   stop.
2. Add `Get Type` for `Output Bytes`; set `Output Bytes Type`. If that text is
   not exactly `Number`, clean up, show alert `output.bytes is not a number.`,
   and stop.
3. If `Download URL` does not begin with `https://`, clean up, show alert `The
   returned download URL is not HTTPS. Credentials were not sent to it.`, and
   stop.

Verify on the target iOS version that JSON numeric values produce the `Get Type`
text `Number`. If Apple changes that label, stop the release and revise this
specification based on observed behavior rather than silently omitting the type
checks.

## 13. Calculate and display the exact result

### 13.1 Savings calculations

1. Add `Calculate` with `Input Bytes - Output Bytes`; set `Savings Bytes`.
2. If `Input Bytes is greater than 0`, calculate `Savings Bytes / Input Bytes`,
   then multiply the result by `100`, then use `Format Number` with two decimal
   places. Set `Savings Percent Display` to that formatted result.
3. Otherwise, add `Text` with `n/a (zero-byte input)` and set `Savings Percent
   Display` to it.

Do not clamp a negative result. If output is larger than input, exact savings
bytes and the percentage are negative. The byte values and byte difference are
the exact preview; only the displayed percentage is explicitly rounded to two
decimal places.

### 13.2 Build the preview

Add a `Text` action containing:

```text
Conversion is ready.

File: Output Filename
Media type: Output Media Type
Input: Input Bytes bytes
Output: Output Bytes bytes
Savings: Savings Bytes bytes (Savings Percent Display%)

The output has not been downloaded yet.
```

Every capitalized field above is a magic variable. For the zero-byte case,
omit the final literal `%` by using a small `If` to build either the numeric
percentage line or `Savings: Savings Bytes bytes (n/a for zero-byte input)`.
Set the final text as `Ready Preview`.

### 13.3 Choose delivery or cancel

Add `Choose from Menu` with `Ready Preview` as its prompt and these branches:

| Menu branch | Actions |
| --- | --- |
| Save to Files | `Text` containing `save`, then `Set Variable Delivery Mode` |
| Share | `Text` containing `share`, then `Set Variable Delivery Mode` |
| Cancel | Run cleanup block; show notification `Conversion cancelled and cleanup requested.`; `Stop This Shortcut` |

The Cancel branch must contain no GET of `Download URL`. This is the required
cancel-before-download point. A user must choose the explicit branch to obtain
handled cleanup. If the system offers a separate control that dismisses the
native menu, that dismissal may terminate the Shortcut before cleanup and must
be recorded as an on-device limitation.

## 14. Download, clean up, and deliver

After the menu, only the `save` and `share` branches reach this section.

1. Add `Get Contents of URL` using `Download URL`.
2. Set Method to `GET`, add both headers, and add no body.
3. Set the result as `Downloaded File`.
4. Add `Set Name` for `Downloaded File` using `Output Filename`; replace the
   `Downloaded File` variable with the renamed result. Configure the action so
   the supplied name includes its extension; do not preserve or append the
   download response's previous extension.
5. Run the cleanup block immediately after the complete file is in memory.
6. If `Delivery Mode is save`, add `Save File` with `Downloaded File`, turn on
   `Ask Where to Save`, and do not silently overwrite an existing file.
7. Otherwise, if `Delivery Mode is share`, add `Share` with `Downloaded File`.
8. Show notification `Output downloaded and cleanup requested.`.

`Output Media Type` is displayed but is not used to invent a file extension or
locally transcode the response. `Output Filename` is authoritative for the
saved/shared name. The download response remains authoritative for its actual
bytes and content type.

Cleanup occurs after download but before `Save File` or `Share`. This prevents
a cancelled system save/share sheet from leaving a normal completed job solely
because the final user interface was dismissed. If the download itself fails,
native action termination may still prevent cleanup.

## 15. Complete action-flow summary

The final top-level flow, in order, is:

1. Load blank-import-question configuration and polling constants.
2. Validate configuration.
3. Require exactly one Share Sheet item.
4. Send authenticated `GET /v1/capabilities` without parsing fields.
5. Collect exact target, quality, resolution, and audio values.
6. Upload the input once with a raw-file authenticated POST.
7. Validate `id`, `state`, and HTTPS `status_url`.
8. Perform at most 120 authenticated status GET requests.
9. Delete and stop on failed, timed-out, or malformed jobs where cleanup is
   possible.
10. Parse the five contracted ready leaf values and their two parent
    dictionaries.
11. Show exact input bytes, output bytes, and savings bytes before download.
12. Delete and stop if the user cancels.
13. Otherwise perform one authenticated download GET.
14. Rename the in-memory file, DELETE the job, then save or share the file.

No other API endpoint, request body, response field, authentication mechanism,
or local media behavior is part of this specification.
