# Media Manager iOS Shortcuts

This directory contains credential-free generated implementations of two native
iOS Shortcuts: `Convert Media` and `Compress Media`. It deliberately contains no
API hostname, service-token value, Apple account data, iCloud link, or signed
`.shortcut` binary.

The behavior contract remains [`spec.md`](spec.md). The checked-in Cherri source
compiles that contract to unsigned Apple Shortcut property lists. The current
artifacts passed structural checks. Earlier versions passed Apple signing/import
and native macOS tests, but those results do not cover this estimate-first
workflow update. iPhone input and delivery still require the on-device release
gate.

The 2026-10-06 quiet production update removes upload/polling debug details and
compression attempt counts from user prompts. Both blank artifacts passed Apple
signing and were installed using existing local configuration. Eleven structural
tests pass; the earlier 19-case runtime results apply to the previous graphs.
See the dated record for exact new artifact hashes and cleanup status.

For future Shortcut projects, use the reusable
[`testing-guidelines.md`](testing-guidelines.md): test layers, private setup,
native harnesses, dialog coordination, import recovery and cross-machine evidence.
The dated [verification and cleanup record](verification-2026-10-06.md) identifies
the working products, retained evidence and archived obsolete test IDs.

## Package contents

The current artifacts prepare jobs first, show a fast estimate with optional
refinement, and start only after an explicit Start choice. Input selection runs
before configuration actions, so Share Sheet input is retained. This rebuild
contains credential-free unsigned artifacts only; signing, installation, and
native runtime acceptance were not part of this update.

- `README.md`: security model and platform-specific development and release
  workflow.
- `spec.md`: exact behavior and API mapping for both Shortcuts.
- `convert.cherri` and `compress.cherri`: top-level Cherri 2.3.0 sources.
- `setup.cherri` and `job.cherri`: shared native-action source.
- `finalize.py`: fixes known Cherri 2.3.0 import-question and raw-action output
  defects, then validates references and credential-bearing HTTP actions.
- `build.py`: builds and validates both masters with pinned Cherri 2.3.0.
- `test-api.py`: tests synthetic files against an explicitly authorized HTTPS
  origin, downloads results, and confirms deletion of only its own jobs.
- `make-fixtures.py`: generates tiny synthetic PNG, WAV, GIF and MP4 inputs.
- `prepare-native-tests.py` and `test-native.py`: create blank native test copies
  and exercise installed copies through the macOS Shortcuts runtime.
- `Convert Media_unsigned.shortcut` and `Compress Media_unsigned.shortcut`:
  generated credential-free artifacts ready for Apple signing.
- `sign-on-mac.sh`: signs both artifacts locally using Apple's `shortcuts`
  command, without a third-party signing fallback.
- `tests/on-device.md`: release-gating tests that require an Apple device and a
  test API deployment.

Cherri is a third-party compiler and its output format is not an Apple-supported
source format. Version 2.3.0 is pinned for this release. Do not let Cherri sign
these artifacts automatically: if local signing fails, Cherri can fall back to
a remote service. Compile with `--skip-sign`, run `finalize.py`, and invoke
Apple's `shortcuts sign` explicitly as described below.

## Security model

The current API contract requires both of these headers on every request:

```text
CF-Access-Client-Id: <per-device client ID>
CF-Access-Client-Secret: <per-device client secret>
```

Treat the pair as a bearer-equivalent machine credential. Anyone who obtains
both values can act as that service identity until the token expires or is
revoked.

- Create a separate service token for each device or user.
- Scope its Access policy only to this application.
- Use the shortest practical lifetime and establish rotation and revocation
  procedures.
- Never use one long-lived token in a generally distributed Shortcut.
- Never put a token in Git, release notes, screenshots, an iCloud link, or an
  exported master artifact.
- Revoke a device's token when the device is lost, replaced, or no longer
  authorized.
- Use Access logs to identify each per-device token and investigate misuse.

The native design stores the base URL, client ID, and client secret in three
blank `Text` actions populated by import questions. Import-question defaults
must remain blank in the distributable master. After a recipient answers the
questions, the answers are ordinary, editable Shortcut content. They are not a
Keychain-backed secret field and are visible to someone who can edit the
Shortcut. Device encryption does not make a long-lived service token safe to
share.

For higher security at the cost of repeated entry, replace the two credential
`Text` actions with `Ask for Input` actions that run each time. Do not describe
those prompts as secure or masked unless that behavior has been verified on the
target iOS version.

Cloudflare One Agent device-session authentication is preferred where the
deployment can use user and device identity without distributing machine
secrets. It is not a substitute for the concrete contract in this package:
this Shortcut still sends both service-token headers on every API request. A
move to device-session-only authentication requires an explicit API and Access
policy change followed by new on-device tests.

## Credential-free master

Maintain two separate Shortcuts on the authoring Apple device:

- `Convert Media - Master` and `Compress Media - Master`: blank configuration fields, blank import-question
  defaults, and no test credentials. Export only this copy.
- Matching `- Test` copies used with a disposable test token. Never
  export or publish this copy.

Before every release, inspect every `Text`, `Dictionary`, `URL`, and `Get
Contents of URL` action in the master. Search visually for the real hostname,
client ID, and client secret. Then import the release artifact on a clean test
device and confirm all three setup questions are blank.

## Generation

Use Cherri 2.3.0 at `shortcuts/private/tools/cherri`, or pass `--compiler`:

```text
python3 shortcuts/build.py
python3 -m unittest shortcuts.tests.test_origin -v
```

`finalize.py` requires all three setup defaults to be blank, checks setup action
indices, converts the Cherri raw upload placeholder to the native Get Contents
of URL identifier, requires explicit token-string inputs in Match Text's `text`
parameter, and validates file-body, header, URL-variable, cleanup, and
output references. It never signs or adds configuration values.

Do not use Cherri 2.3.0's `--derive-uuids`: it reuses control-flow identifiers
and can replace earlier menu choices with the final menu's choices. The
finalizer rejects reused action IDs, reused groups, unbalanced control flow,
and menus whose displayed choices differ from their branches. Normal builds
use fresh UUIDs, so equivalent rebuilds have different artifact hashes.
The finalizer also removes the compiler's extraneous `VariableName` from native
`ExtensionInput` tokens; named-variable tokens retain their names.
Filename lookup uses native Get Name with an explicit selected-file input.
Cherri's Get File Detail definition emitted `WFFolder`, leaving its File input
empty in the native editor. Direct Get Name passed the native macOS test matrix.

## Automated Mac tests

Store test configuration outside a synced folder, or in ignored
`shortcuts/private/config.json`, with directory mode `0700` and file mode `0600`.
It contains `baseURL`, `clientID`, and `clientSecret`. Never print its contents.
Authenticated tests require authorization for the specified origin and synthetic
uploads; they do not change service configuration or delete unrelated jobs.

Generate inputs with `python3 shortcuts/make-fixtures.py` when local FFmpeg is
available. Alternatively, provide `--docker` and `--docker-image` explicitly
for a cached FFmpeg image; the generator never pulls an image and constrains
the container to synthetic files, no network, one CPU and 256 MiB RAM.
Supply a separate one-second synthetic MP4 via `test-api.py --video` if needed.
The API matrix
uses PNG, WAV, and MP4 sources for nine formats, three compression types,
discard without download, and an unsupported-conversion failure.
Add `test-api.py --gif-inputs` to include still PNG to GIF, GIF to GIF/MP4/WebM,
and GIF compression to MP4. The native matrix includes these five additional
regression cases; `--case` can select a single case.

```text
python3 shortcuts/test-api.py --config shortcuts/private/config.json --origin https://YOUR-HOST
python3 shortcuts/prepare-native-tests.py
shortcuts sign --mode anyone --input "shortcuts/private/native-tests/Media Manager E2E Convert_unsigned.shortcut" --output "shortcuts/private/native-tests/Media Manager E2E Convert.shortcut"
shortcuts sign --mode anyone --input "shortcuts/private/native-tests/Media Manager E2E Compress_unsigned.shortcut" --output "shortcuts/private/native-tests/Media Manager E2E Compress.shortcut"
```

Import these two signed test copies and fill their three blank setup questions
locally. Apple receives blank copies for signing. Keep configured copies out of
exports and shared folders. Then run:

```text
python3 shortcuts/test-native.py --config shortcuts/private/config.json --origin https://YOUR-HOST
```

Use `--case image-jpeg` for the first native probe. Test copies select targets
and Keep/Discard from synthetic filenames and return JSON receipts instead of
opening the share sheet. The generator verifies that all 15 HTTP actions match
the validated master exactly. The runner checks authoritative metadata,
downloaded byte counts, compression fields, expected failure, and HTTP 404 after
cleanup. Receipts and synthetic outputs stay in ignored `shortcuts/private/`.
Upload rejection and validation failures return allowlisted diagnostic receipts
in test copies, avoiding blocking alerts without displaying the raw response.
Cleanup precedes the diagnostic when the master has a valid job identity.
Native permissions and runtime errors can still require interaction. Installed
copies must be replaced with freshly signed copies after rebuilding.
Use `--mode convert` or `--mode compress` to test one updated copy at a time
when sharing access to the Shortcuts app.

These tests verify the native transport and conversion graph. The original
format menu, size preview, pickers, Share Sheet, Save to Files, and iPhone
background/lock behavior still require [`tests/on-device.md`](tests/on-device.md).
Coordinate access to the Shortcuts app when another chat is testing. If a run
pauses on a permission or alert dialog, inspect that specific Shortcuts helper
dialog before rerunning; do not send global keyboard shortcuts while the user
is using other applications.

In the observed macOS runtime, the format and Keep/Discard choosers require
selecting a row and then clicking Done. The helper window can be visible on the
desktop while absent from the main Shortcuts accessibility tree. Keep the
existing run open for the user's selection instead of starting another run.

### Current verification (2026-10-06)

- The live API completed all 18 synthetic cases with each job deleted and
  confirmed absent, including the four PNG/GIF conversion regressions.
  A subsequent animated GIF compression case also passed, returning a
  1,591-byte MP4; deletion and HTTP 404 were verified. This verifies the server
  contract, not native execution.
- Earlier blank native test copies passed Apple signing and registered in the
  native library after a delay despite recurring setup panels. The first native
  upload reached an API error response. Current copies use direct Get Name and
  return safe upload-error receipts. Both current copies passed Apple signing.
  Corrected Convert is installed and passed all 15 native conversion cases:
  nine formats, discard, unsupported conversion, and four PNG/GIF regressions.
  Upload, polling, downloaded byte counts, expected failure and deletion were
  verified; every test job returned HTTP 404 afterward. Corrected Compress is
  installed and passed PNG-to-WebP, WAV-to-M4A and MP4 compression, including
  threshold metadata, attempt counts, downloaded bytes and HTTP 404 after
  deletion. An earlier PNG run timed out; after replacing it with the current
  diagnostic copy, the PNG case and full compression matrix passed. The cause
  of the earlier timeout remains unproven.
  The recurring setup panel concealed a duplicate-name Replace alert; cancelling
  that panel revealed the alert, and Replace completed the import. Complete
  iOS release gates remain pending. The
  automated copies replace menus and sharing with deterministic selections and
  receipts; their success does not establish those delivery gates.
- The installed automated Compress copy also passed GIF-to-MP4 compression.
  Its 1,591-byte output matched the independently tested API bytes, and the
  runner verified canonical MP4 metadata, threshold fields and HTTP 404 after
  cleanup. The complete native matrix now contains 19 cases: 15 conversion and
  four compression cases. Both public masters were rebuilt privately and
  matched their source after normalizing fresh UUIDs and dictionary ordering.
- An unchanged Convert action graph, imported under a separate UI test name,
  completed PNG-to-JPEG through the original format menu, size preview and Keep
  choice. The CLI returned a valid 290-byte JPEG with the same SHA-256 as the
  independent native matrix result. The user confirmed the sharing menu opened
  and reported a new image in Photos. The server work directory was empty after
  the run; a job-specific HTTP 404 was not captured for this interactive case.
  This is macOS evidence, not an iPhone Share Sheet or Save to Files test.
- An unchanged Compress action graph completed the size preview and Keep path
  on macOS. Its valid 152-byte WebP matched the independent native matrix
  result, the user confirmed the sharing menu opened, and the server work
  directory was empty afterward. The tested UI copies are now named
  `Convert Media` and `Compress Media`; the older configured Debug copy was
  preserved. Runtime helper selections were completed by the user, not by the
  automation controls. iPhone input and delivery remain unverified.
- Additional live probes reproduced GIF-to-GIF rejection (`MEDIA_LIMIT_EXCEEDED`)
  and still PNG-to-GIF failure (`CONVERSION_FAILED`). Local processor fixes now
  accept GIF's measured frame rate and preserve the single frame when producing
  a GIF from a still image. Real FFmpeg regression tests passed for still-image
  to GIF and short GIF to GIF/MP4/WebM; unknown GIF timing and excessive frame
  rates remain rejected. Commit `3aaa25abca62031144abdcf829113f348d39de86`
  was deployed to `diogoserver2` on 2026-10-05. Readiness, authentication,
  container security limits and all 18 authenticated API cases passed.
  The previous image and a private environment backup are retained for rollback.

Windows still cannot perform these Apple-specific steps:

- Sign a Shortcut using Apple's `shortcuts` command.
- Validate Share Sheet, upload, polling, save, or share behavior on iOS.
- Silently install a Shortcut on a recipient's device.
- Create an iCloud Shortcut link without using the Shortcuts app on an Apple
  device.

Do not use a remote third-party signing service for a Shortcut that contains a
hostname or credentials. The checked-in unsigned artifacts contain neither.

## Mac signing

Transfer this directory to the Mac, open Terminal, and run:

```text
bash "/path/to/media-manager/shortcuts/sign-on-mac.sh"
```

The script validates both unsigned plists, sends each credential-free artifact
to Apple for `anyone` signing, and opens
`~/Downloads/media-manager-shortcuts`. It overwrites only same-named signed
outputs in that dedicated directory. It does not invoke Cherri or any
third-party signing service.

Double-click each signed file and answer its three setup questions. Use the
actual API origin and a disposable per-device service token only in the installed
test copy, then complete [`tests/on-device.md`](tests/on-device.md).

The installed Shortcuts use native actions only and require no companion app.

## Manual signing

The script is equivalent to:

```text
shortcuts sign --mode anyone --input "Convert Media_unsigned.shortcut" --output "Convert Media.shortcut"
shortcuts sign --mode anyone --input "Compress Media_unsigned.shortcut" --output "Compress Media.shortcut"
```

Apple receives each credential-free artifact for validation. Signing protects
the distributed file against later modification; it is not an audit of the
workflow's behavior or credential handling.

## iCloud-link distribution

An iCloud link is the lowest-friction supported installation path:

1. Open the credential-free master in Shortcuts on an Apple device.
2. Use Share and choose `Copy iCloud Link`.
3. Open the link yourself and confirm the displayed Shortcut is the intended
   credential-free version.
4. Give recipients the link through the release channel.
5. Each recipient reviews the Shortcut, taps `Get Shortcut`, and answers the
   three blank setup questions with their own base URL and per-device token.

Installation remains user-mediated. Neither an iCloud link nor a signed file
silently installs the Shortcut.

Stopping iCloud sharing does not revoke credentials already entered into
installed copies. Revoke the relevant service token separately.

## Per-device setup

Give each recipient these values out of band:

- API base URL in the form `https://<host>`, with no path, query, fragment, or
  trailing slash.
- A client ID created for that recipient or device.
- The matching client secret, which must not be reused on another device.

The recipient should:

1. Import the Shortcut from the signed file or iCloud link.
2. Enter the three values when prompted. Defaults must be blank.
3. Review the Shortcut and confirm the secret appears only in its configuration
   `Text` action and is referenced through magic variables elsewhere.
4. Run a small test conversion from the Share Sheet.
5. Report the device identity to the token administrator so token ownership and
   expiration can be recorded.

If a credential changes, edit the corresponding configuration `Text` action or
re-import a clean copy and answer the setup questions again. Do not edit each
HTTP header separately; every header value must reference the configuration
variables.

## Release gate

A release is acceptable only when all of the following are true:

- Both masters contain no hostname, credential, Apple account data, or private
  media.
- All three import-question defaults are blank.
- The action graph matches [`spec.md`](spec.md).
- Every HTTP action carries both exact Cloudflare Access header names.
- Upload uses a raw `File` body, not Form, multipart, or JSON.
- Polling performs no more than 200 status requests.
- A user can choose Discard after seeing the size preview and before download.
- `DELETE status_url` is sent only for an explicit Discard choice.
- Failure, timeout, closing the Shortcut, and keeping/sharing a result do not
  trigger deletion; server expiry remains the cleanup fallback.
- The output is downloaded before it is saved or shared.
- The signed-file and iCloud-link installation paths have each been tested from
  a clean device where they will be offered.
- Known limitations in the next section are included in release notes.

## Known limitations

- Native Shortcuts has no documented general-purpose secret vault for these
  header values. Import-question answers remain editable.
- `Get Contents of URL` has no portable `try/finally` behavior. A process kill,
  network failure, HTTP error, or action error can stop execution before the
  cleanup request. The server must not depend exclusively on client-side
  `DELETE` for eventual cleanup.
- The nominal polling window is about 597 seconds plus request time. Share Sheet
  execution and large uploads can be constrained by the target iOS version,
  device memory, and the calling app.
- Failed jobs expose only the API's bounded public `error.code` and
  `error.message`. Raw FFmpeg output is never available to the Shortcut.
- Requests use canonical URLs built from the configured HTTPS origin and a
  validated job UUID. Returned status and download URLs must match exactly.
  Native HTTP redirect behavior is not controlled by these equality checks
  and remains a device release gate.
- The explicit `Discard` menu branch requests cleanup. Dismissing a native menu
  or closing the Shortcut can leave a prepared or completed item until server
  expiry.
- Compress returns MP4 for video and animated GIF, WebP for still images, and
  M4A for audio; it does not preserve the input format. GIF output in Convert
  is limited to 15 seconds and the server's animation pixel limit.
- Native iPhone Photos/Share Sheet coercion has not been tested with HEIC or
  camera RAW. The server's input demuxer allowlist does not include HEIF; use
  supported JPEG/PNG/GIF files for the verified paths until device testing
  establishes what those pickers upload.
