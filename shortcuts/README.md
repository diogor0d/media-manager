# Media Manager iOS Shortcut

This directory defines a credential-free development and distribution package
for a native iOS Shortcut that submits shared media to the Media Manager API.
It deliberately contains no API hostname, service-token value, Apple account
data, iCloud link, or signed `.shortcut` binary.

The authoritative implementation is the action-by-action native specification
in [`spec.md`](spec.md). It is intended to be authored once in Apple's
Shortcuts app and then exported for users. Recipients do not need to recreate
the actions.

## Package contents

- `README.md`: security model and platform-specific development and release
  workflow.
- `spec.md`: exact native Shortcut behavior and API mapping.
- `tests/on-device.md`: release-gating tests that require an Apple device and a
  test API deployment.

No declarative source is included. A third-party generator can produce an
unsigned Shortcut representation on Windows, but that representation and its
compatibility with the current Shortcuts app could not be validated here.
Apple's supported signing tooling is only available on macOS and is documented
for previously exported shortcuts. The native specification is therefore more
truthful and supportable than unverified generated source.

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

- `Media Manager - Master`: blank configuration fields, blank import-question
  defaults, and no test credentials. Export only this copy.
- `Media Manager - Test`: a duplicate used with a disposable test token. Never
  export or publish this copy.

Before every release, inspect every `Text`, `Dictionary`, `URL`, and `Get
Contents of URL` action in the master. Search visually for the real hostname,
client ID, and client secret. Then import the release artifact on a clean test
device and confirm all three setup questions are blank.

## Windows-automatable work

Windows can be used for the following repository and release preparation:

- Maintain and review this specification and test plan.
- Compare the native Shortcut against the numbered action sequence during a
  screen-sharing or review session.
- Record a release version, tested OS versions, and the checksum of an artifact
  that was exported on a Mac.
- Publish an already Apple-exported artifact or an already-created iCloud link
  through the chosen release channel.

Windows cannot perform these official Apple steps:

- Create the native action graph through an Apple-supported command-line tool.
- Sign a Shortcut using Apple's `shortcuts` command.
- Validate Share Sheet, upload, polling, save, or share behavior on iOS.
- Silently install a Shortcut on a recipient's device.
- Create an iCloud Shortcut link without using the Shortcuts app on an Apple
  device.

Do not use a remote third-party signing service for a Shortcut that contains a
hostname or credentials. This package does not require one.

## Native authoring workflow

1. On a current iPhone, iPad, or Mac, create `Media Manager - Master` in the
   Shortcuts app.
2. Implement [`spec.md`](spec.md) exactly, including all blank setup questions,
   header repetitions, bounded polling, confirmation, and cleanup branches.
3. Keep all three configuration `Text` actions blank in the master.
4. Duplicate the master as `Media Manager - Test`.
5. Populate only the test copy with a disposable per-device token and test API
   base URL.
6. Complete every applicable case in [`tests/on-device.md`](tests/on-device.md).
7. Apply tested fixes to the blank master without copying configuration values
   from the test copy.

The Shortcut uses native actions only and requires no companion scripting app.

## Mac export and signing

The preferred file release is an export performed by the Shortcuts app on a
Mac:

1. Open the credential-free master.
2. Confirm its setup questions have blank defaults.
3. Choose `File > Export`.
4. Select `Anyone` for a generally distributable file.
5. Save the exported `.shortcut` file outside this repository until its clean
   import has been verified.
6. Import it on a clean Apple device, answer with disposable test values, and
   run the release tests.

Apple receives a copy for validation when exporting for `Anyone`. That process
protects the distributed file against modification; it is not an audit of the
Shortcut's behavior or credential handling.

If a previously exported file must be signed again, macOS provides:

```text
shortcuts sign --mode anyone --input "Media Manager.exported.shortcut" --output "Media Manager.shortcut"
```

What it does: asks Apple's macOS Shortcuts tooling to validate and sign a
previously exported Shortcut for import by anyone.

Do not claim that the command is available on Windows or that it officially
accepts arbitrary files produced by external generators.

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

- The master contains no hostname, credential, Apple account data, or private
  media.
- All three import-question defaults are blank.
- The action graph matches [`spec.md`](spec.md).
- Every HTTP action carries both exact Cloudflare Access header names.
- Upload uses a raw `File` body, not Form, multipart, or JSON.
- Polling performs no more than 120 status requests.
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
- The nominal polling window is about 595 seconds plus request time. Share Sheet
  execution and large uploads can be constrained by the target iOS version,
  device memory, and the calling app.
- The capabilities request carries authentication headers, but this release uses
  a tested static option graph rather than dynamically constructing native menus.
  If the HTTP action follows an Access redirect or returns an error body without
  raising an action error, this request alone cannot prove authentication.
- Failed jobs expose only the API's bounded public `error.code` and
  `error.message`. Raw FFmpeg output is never available to the Shortcut.
- The contract requires credentials to be sent to the absolute `status_url` and
  `output.download_url` returned by the API. Both URLs are required to use
  HTTPS. Deployments should keep them on trusted Access-protected origins and
  test redirects carefully.
- The explicit `Cancel` menu branch requests cleanup. Dismissing a native menu
  through the system's own cancellation control may terminate the Shortcut
  before that branch runs. This must be tested and treated like any other
  unexpected termination.
