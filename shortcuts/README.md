# Media Manager iOS Shortcuts

This directory contains credential-free generated implementations of two native
iOS Shortcuts: `Convert Media` and `Compress Media`. It deliberately contains no
API hostname, service-token value, Apple account data, iCloud link, or signed
`.shortcut` binary.

The behavior contract remains [`spec.md`](spec.md). The checked-in Cherri source
compiles that contract to unsigned Apple Shortcut property lists. The generated
artifacts have passed structural checks and Apple signing/import was proven with
a minimal setup-question probe, but the complete workflows still require the
on-device release gate.

## Package contents

The current artifacts are diagnostic builds: after uploading they show a local
Quick Look preview of the actual POST response before parsing it, and polling
timeouts use the `Media Manager: Polling Diagnostic v2` title. Do not publish
the raw preview or screenshots that could contain sensitive response details.
Close the preview to continue. Remove these diagnostics after polling succeeds;
these are not validated release artifacts.

- `README.md`: security model and platform-specific development and release
  workflow.
- `spec.md`: exact behavior and API mapping for both Shortcuts.
- `convert.cherri` and `compress.cherri`: top-level Cherri 2.3.0 sources.
- `setup.cherri` and `job.cherri`: shared native-action source.
- `finalize.py`: fixes known Cherri 2.3.0 import-question and raw-action output
  defects, then validates references and credential-bearing HTTP actions.
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

The current artifacts were compiled with Cherri 2.3.0 using:

```text
cherri convert.cherri --skip-sign
cherri compress.cherri --skip-sign
python3 finalize.py "Convert Media_unsigned.shortcut" "Compress Media_unsigned.shortcut"
```

`finalize.py` requires all three setup defaults to be blank, checks setup action
indices, converts the Cherri raw upload placeholder to the native Get Contents
of URL identifier, requires explicit token-string inputs in Match Text's `text`
parameter, and validates file-body, header, URL-variable, cleanup, and
output references. It never signs or adds configuration values.

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
- A user can cancel after seeing exact byte counts and before any download.
- Success, explicit user cancellation, a reported `failed` state, timeout, and
  malformed ready data invoke `DELETE status_url` when a valid status URL is
  available.
- The output is downloaded before cleanup and is then saved or shared.
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
- The contract requires credentials to be sent to the absolute `status_url` and
  `output.download_url` returned by the API. Both URLs are required to use
  HTTPS on the exact configured API origin. Reject every other origin and test
  redirects carefully.
- The explicit `Cancel` menu branch requests cleanup. Dismissing a native menu
  through the system's own cancellation control may terminate the Shortcut
  before that branch runs. This must be tested and treated like any other
  unexpected termination.
