# Deployment Guide

## State

An earlier revision (`8f2e2b9be87b98b7fc6efdeac8f7338c1d872e5e`) is deployed
behind Cloudflare Access. The current worktree's compression endpoint, progress,
quality scale, filename handling, PWA changes, and Shortcut contracts are local
prepared state only. No production mutation is authorized by this document;
repeat the validation matrix for the exact new image before deployment.

## Intended architecture

```text
approved browser or iOS device
  -> https://<dedicated-media-hostname>
  -> Cloudflare Access (interactive identity or one service token per device)
  -> Cloudflare Tunnel
  -> http://127.0.0.1:<host-port>   (loopback-only origin)
  -> media-manager container TCP 8080
  -> disposable host work directory
```

| Item | Intended value |
| --- | --- |
| Compose project/service | `media-manager` / `api` |
| Source | This repository at a clean recorded commit |
| Image | Built from that commit, tagged with it, then recorded by immutable digest |
| Host publication | IPv4 loopback TCP `${MEDIA_MANAGER_PORT}` to container TCP `8080` |
| Public ingress | Dedicated HTTPS hostname through an existing host-managed Tunnel connector |
| Edge policy | Cloudflare Access interactive identity for browsers and `Service Auth` for iOS automation |
| Origin auth | Access JWT signature, issuer, audience, expiry, and principal validation |
| Persistence | Disposable host work directory only; no user data or job state survives restart |
| Backup | Work directory excluded by design; Git and recorded image digest are desired state |
| Resources | Initial ceiling: 2 CPUs, 2 GiB RAM/no extra swap, 128 PIDs |
| Logs | Docker local driver, 10 MiB x 3; no access log or media/credential content |
| Health | Local unauthenticated `/health/ready`; external synthetic check remains Access-authenticated |

Placement requirements for any candidate host:

- Docker Engine + Compose with cgroup v2 and a host-managed `cloudflared`
  connector already trusted by your Cloudflare zone;
- sufficient CPU/RAM headroom for a 2-CPU / 2 GiB conversion workload measured
  against existing services;
- a pre-created host work directory owned by UID/GID `10001`, mode `0700`, with
  at least 10 GiB plus operational headroom; a quota-backed path is strongly
  preferred; and
- no conflicting listener on the chosen loopback port.

## Secrets and configuration

The server receives only non-secret Access metadata:

- `MEDIA_MANAGER_PUBLIC_BASE_URL`;
- `MEDIA_MANAGER_WORK_DIR_HOST`;
- `MEDIA_MANAGER_CF_ISSUER`;
- `MEDIA_MANAGER_CF_AUDIENCE`.

The Cloudflare service-token client ID and secret belong on the authorized iOS
device only. They must never be placed in server `.env`, Compose, Git, an image,
logs, screenshots, or the credential-free master Shortcut. Store the real
server `.env` outside Git with owner-only permissions even though its intended
values are non-secret, and review it before sharing diagnostics.

## Pre-deployment gates

Do not deploy until all of these are resolved:

1. Obtain explicit authorization for read-only host inspection and the exact
   later production mutation.
2. Reconcile the server checkout path, Git state, current Compose projects, and
   unexpected local changes.
3. Verify current CPU, RAM, swap, Docker data-root capacity/inodes, and whether a
   quota-backed work path is available. Benchmark before raising limits.
4. Verify the chosen loopback port is unused. If changed, update your exposure
   documentation and Tunnel origin together.
5. Build from a clean commit, run tests, record the image identifier, FFmpeg and
   Python package versions, and scan results. Keep the previous known-good image.
6. Confirm the Cloudflare zone upload limit exceeds the configured 50 MiB chunk
   size and that upload, polling, and download paths do not use cross-origin
   redirects. Cloudflare rejects single requests above the plan limit, so 5 GiB
   browser uploads must use `/v1/uploads`; do not raise the chunk size casually.
   See Cloudflare's current [request-size limits](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/).
7. Create a dedicated hostname, Tunnel route, Access application AUD, an
   interactive browser policy, and a `Service Auth` policy. Cover `/`,
   `/assets/*`, and `/v1/*`; do not add a bypass or direct public origin route.
8. Create one short-lived, application-scoped service token per device and
   record owner/expiry/revocation metadata without values.
9. Add work-volume/disk, container health/restart, CPU/memory, stuck-job, Tunnel,
   and authenticated synthetic monitoring. Test alert delivery.
10. Prepare the credential-free Shortcut and complete the on-device release
    matrix over Wi-Fi and cellular with disposable media and credentials.

## Local artifact validation

Use placeholder values to validate syntax without rendering or exposing a real
environment:

```bash
docker compose --env-file .env.example config --quiet
docker compose --env-file .env.example config --services
docker compose --env-file .env.example config --images
```

What it does: validates Compose and lists the reviewed service/image names
without printing a secret-interpolated production configuration.

Build and test the exact clean revision intended for deployment:

```bash
revision="$(git rev-parse HEAD)"
docker build --build-arg SOURCE_REVISION="$revision" --tag "media-manager:$revision" .
docker image inspect "media-manager:$revision" \
  --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
```

What it does: builds a source-labelled image and prints its content-addressable
local image ID with the recorded source revision. The checkout must be clean;
record a registry `RepoDigest` as well if the artifact is pushed. Inspect output
narrowly because broad image or container inspection can expose configuration.

## Authorized deployment outline

These steps are a runbook, not standing authorization:

1. Preserve an independent administrative path and capture the current
   service-scoped rollback state.
2. Place a clean checkout at the approved server path and check out the reviewed
   commit without overwriting server-local changes.
3. Pre-create `MEDIA_MANAGER_WORK_DIR_HOST` as UID/GID `10001`, mode `0700`,
   and verify its capacity; do not let Compose create it as root.
4. Create the real `.env` from the variable names in `.env.example`, set its
   owner-only mode, and use a commit-tagged `MEDIA_MANAGER_IMAGE`.
5. Run quiet Compose validation and build the image from the recorded commit.
6. Start only the `media-manager` project and wait for `/health/ready`.
7. Configure the dedicated Access application with interactive and `Service
   Auth` policies, then add the host Tunnel route to the loopback origin.
8. Run the full validation matrix before distributing a production Shortcut.
9. Record actual path, commit, image digest, package versions, listener, Tunnel
   route, Access policy type, resource baseline, and remaining gaps wherever you
   track infrastructure state.

Expected application downtime is only the container replacement interval.
In-flight and retained jobs are disposable and will be lost on every restart.

## Validation matrix

| Area | Required result |
| --- | --- |
| Configuration | Quiet Compose validation succeeds; only `api`, its project network, and the reviewed work-directory bind are present |
| Runtime | Container stays healthy without restart loops; FFmpeg capability startup check passes |
| Artifact | Running image digest and source revision match the reviewed records |
| Local origin | Host request to the loopback `/health/ready` returns `200` |
| Edge denial | External request without Access credentials is denied before media reaches the origin |
| Edge allow | Dedicated test token can read capabilities, convert a synthetic fixture, poll, preview, download, and delete |
| Browser flow | Interactive Access login loads the authenticated UI and assets; iPhone safe areas, file selection, options, resumable upload, recovered polling, native sharing, update deferral, and discard work without cross-origin requests |
| Origin JWT | Missing/invalid/wrong-audience assertions fail when testing the origin through an approved isolated path |
| Isolation | A second principal receives `404` for the first principal's job |
| Exposure | No wildcard, LAN, VPN, router, or IPv6 listener exists for the published loopback port |
| Limits | Oversized, malformed, playlist, excessive-dimension/duration, timeout, cancellation, and queue-full cases stay bounded and clean up |
| Resources | Representative image/video/GIF conversions remain inside measured CPU, memory, PID, disk, and time budgets |
| Compression | Video, image, and audio select the documented canonical outputs; sub-20 MB and over-target results report measured status correctly; cancellation removes every attempt directory |
| Logs | No token, assertion, header, media content, source filename, metadata, or raw FFmpeg error is present |
| Cleanup | Explicit delete, expiry, restart, failed conversion, and client abandonment leave no stale files |
| iOS | Signed-file/iCloud import, Wi-Fi/cellular upload, polling, exact size preview, cancel-before-download, save/share, expiry, and bad-token cases pass |
| Monitoring | Local health, authenticated external check, restart, resource, and disk alerts reach the owner |

## Abort conditions

Abort or roll back if the checkout is dirty unexpectedly; image provenance is
unknown; the intended port is occupied; capacity/quota is inadequate; the
container is unhealthy or restarts; Access allow or denial fails; direct origin
access is possible; files survive required cleanup; resources threaten host
stability; logs expose sensitive data; or rollback cannot be performed.

## Rollback

The service has no migration or authoritative runtime data. Rollback is:

1. Restore the previous reviewed Compose/source revision and image digest.
2. Recreate only the `media-manager` service.
3. Repeat local health, Access allow/deny, origin exposure, resource, and cleanup
   checks.
4. If the new public route or policy caused the failure, restore its recorded
   previous state or remove the new route through an authorized Cloudflare
   change.

Do not delete the host work directory during rollback. Its contents are
disposable, but deletion should be a separate explicit retention/decommission
decision.

## Operations and decommissioning

Update by building a new clean revision and digest, retaining the previous image
through the rollback window, recreating only this service, and rerunning the
validation matrix. Rebuild promptly for FFmpeg, Python, Alpine, FastAPI, PyJWT,
and cryptography security updates.

For decommissioning, first stop distributing the Shortcut, remove the Access
application/Tunnel route/DNS in the approved order, revoke every per-device
token, stop the Compose project while retaining the work directory until deletion
is approved, then remove disposable files and reconcile your infrastructure
inventory, exposure records, monitoring, and activity documentation.
