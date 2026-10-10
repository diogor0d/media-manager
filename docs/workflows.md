# Processing and Integration Workflows

Last updated: 2026-10-09

This page describes code paths and setup requirements. Implementation or
synthetic tests do not establish live Telegram, physical-device, or production
acceptance. Disposable local MeTube validation is recorded separately from
production acceptance.

## Prepare, estimate, start

The web client and API can separate upload from processing so the user can
review options and a size/time range before encoding:

1. Upload the media with `prepare_only=true` on a one-shot job/compression
   request, or reserve and fill a chunked upload and call its `/prepare` route.
2. Inspect metadata with `GET /v1/jobs/{id}/inspect`, then submit the chosen
   `conversion_options` or `compression_options` to the
   matching `/estimate` route. The first response is a fast, low-confidence
   range. The server schedules refinement when its encoding worker is idle;
   fetch the same estimate route with `GET` for the latest state. Sampling uses
   at most three two-second samples with a six-second sample budget and a
   20-second overall deadline. Cancellation and job start stop outstanding
   sampling. If sampling is unavailable or fails, the fast estimate is returned
   with `status: fallback` and no ETA.
3. Start only after review with the matching `/start` route and the same chosen
   options. Poll the job's status URL. The estimate's `options_key` identifies
   the option set; any changed option set needs a new estimate.

Size and ETA values are ranges derived from a bounded sample. They can differ
from final output and runtime; neither a sampled estimate nor a successful
compression guarantees the selected byte ceiling. The encoder's actual output
size and `met_target` field are authoritative. A null ETA means no reliable
time range was available.

Chunk upload offsets and partial files belong to the live process and are not
restored after restart. Once `/prepare` or normal completion accepts the source,
the job record and input are persisted. Accepted review jobs survive restart
until the owner's retention expiry (seven days by default); incomplete uploads
retain the shorter upload-session deadline. Accepted queued/processing jobs are resumed after
restart; interrupted processing may be recovered twice, then fails. Finished
results use the configured retention policy (seven days by default, adjustable
per owner from one to 90 days).

## MeTube download, then optional processing

The optional bridge accepts `POST /v1/downloads` only when
`MEDIA_MANAGER_METUBE_SIDECAR_URL` is configured. It submits download controls
to the sidecar, polls status, copies the completed media to a private local
record, then exposes status/content. A request may select `download`, `convert`,
`compress`, or `convert_compress`; for processing workflows, the local source
is preserved for review and a child media job is created on explicit process
or when `auto_process` is selected. `parent_id` on the child and `child_id` on
the download record link the two identities. They are distinct records with
separate status, deletion, and expiry. Combined conversion and compression use
the selected conversion format and the compression controls for quality,
resolution, audio, and size. Every candidate uses the original downloaded source.

The local download record and child relationship are written under the private
work directory's `.integrations` area. The supplied source URL is sent to the
sidecar but omitted from the local registry and history. Keep the sidecar URL
reachable only on the intended private network; configure its API key in the
protected application environment when required. The bridge sends
`X-Sidecar-Api-Key` and disables redirects.

Before enabling this bridge, the sidecar must support caller-provided UUID
`request_id` on `POST /jobs`, persist it, and return/look up the same job for a
repeated ID. The media-manager writes its local reservation before submission
and polls that ID after an ambiguous POST outcome; it must never blindly retry
the add because the remote server may already have accepted it. Submission
uncertainty has a bounded deadline and is eventually exposed as a safe failure
without resubmission. A directory appearing on disk alone is not proof the
sidecar accepted a job. Verify an authoritative sidecar status response tied to
the request ID. The additive sidecar correlation patch and disposable MeTube video, thumbnail,
and quality checks passed locally; production and installed-device acceptance
remain separate gates.

## Identity and history sharing

Cloudflare Access subjects are represented as `user:<sub>` and service tokens
as `service:<common_name>`. `MEDIA_MANAGER_PRINCIPAL_OWNERS` can map either
source form to a canonical `user:<owner>` identity. Job ownership checks,
history, history settings, and download records resolve through this mapping.
For example, map a dedicated service identity to the same user owner only when
that service is intended to access that owner's private media. Mappings must be
direct source-to-user entries; do not create alias chains or point two user
owners at each other. A mapping grants shared access rather than merely
changing display identity.

The Telegram bot is separately restricted to one configured private chat and
one authorized Telegram user, and uses `MEDIA_MANAGER_TELEGRAM_OWNER_ID` as its
application owner. Set it to the intended canonical `user:<owner>` identity
shared by the corresponding Access aliases. Telegram does not invent a separate
Access principal; confirm the exact owner and any source-to-owner mapping from
authorized configuration rather than inferring it from a display name.

## Telegram workflows

When a bot token is configured with private chat, user, and owner settings, the
bot can accept a URL or media file in that private chat. URL actions support
download presets, conversion, compression, and advanced source/output
settings; downloaded sources can be reviewed or configured for automatic
processing. File uploads can use conversion or compression options, receive an
estimate, then start as an accepted job. Progress and completed media can be
sent back through Telegram. `/status` reports the recent-completion count.

Telegram update offset, session/job IDs, safe options, and notification outbox
are stored in private state. Source URLs are removed before that state is
written. Telegram file IDs are used to retrieve incoming media and are not
treated as filesystem paths. If local Bot API file access is deliberately
used, `MEDIA_MANAGER_TELEGRAM_FILE_ROOT` limits reads to the configured shared
mount. Keep the bot token secret and do not expose the bot outside the intended
private chat/user authorization.

Bot token/chat/user/owner configuration and live Bot API acceptance remain
pending. Before enabling, test with a disposable bot and private account:
authorization denial, one URL and one file for each intended workflow,
estimate/start behavior, duplicate Telegram update handling, restart recovery,
result delivery, expiry, and secret-free logs. Physical device acceptance and
production deployment are separate gates and remain pending.

## Optional private services

`compose.yaml` remains the base application. For downloads without Telegram,
add `compose.downloads.yaml`, set the existing sidecar network name through
`MEDIA_MANAGER_METUBE_NETWORK`, and provide the private runtime env file through
`MEDIA_MANAGER_INTEGRATIONS_ENV_FILE`. Configure the same private bridge key as
`SIDECAR_BRIDGE_API_KEY` on the sidecar and
`MEDIA_MANAGER_METUBE_SIDECAR_API_KEY` on Media Manager. The additive authenticated
`/internal/jobs` routes leave existing sidecar clients unchanged.

For downloads with Telegram, add `compose.integrations.yaml`
for the MeTube bridge and the `telegram` profile for the official local Bot API.
Use [deploy/compose.env.example](../deploy/compose.env.example) only as a template;
the integration and application env examples contain empty credentials and do
not enable Telegram. Put real runtime files outside this checkout, mode 0600.

- Set the fixed sidecar origin to its private Docker service name on the
  existing MeTube network (the supplied sidecar listens on port 8080). Give the
  Media Manager service access to that network; preserve the sidecar's read-only
  download mount. The bridge never sends upstream credentials to clients.
- Set `MEDIA_MANAGER_PRINCIPAL_OWNERS` to exact verified Access identities.
  Find the browser subject and service common-name from trusted authentication
  configuration; do not copy a display name or expose tokens in diagnostic logs.
- Create the bot with BotFather and obtain a separate application API ID/hash
  from `my.telegram.org`. Configure token, positive private chat/user IDs, and
  the canonical owner in the protected integrations env file. Application ID/hash
  belong in the protected Telegram application env file.
- The official Bot API source is pinned to
  `e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1`; its TDLib submodule is pinned by that
  tree. Both build/runtime bases use the same immutable Python/Alpine digest.
  The service has no published host port, uses a private Docker bridge, and
  still needs outbound Internet access to Telegram. It has bounded CPU, memory,
  PIDs, logs, health checks, and persistent storage. Media Manager receives a
  read-only mount at the same `/telegram` path to securely copy local `getFile`
  results without allowing paths outside that root.
- Cache cleanup runs before the Bot API starts and every hour. It removes only
  expired media caches inside validated bot directories, preserving session and
  database files, and inspects at most 10,000 files per cleanup pass. Keep a
  filesystem quota and monitor disk usage. Cache retention should exceed expected
  transfer duration. Existing Media Manager input limits apply independently of
  Telegram's cache.

Validate placeholders locally without starting services:

```bash
docker compose --env-file .env.example --env-file deploy/compose.env.example \
  -f compose.yaml -f compose.integrations.yaml --profile telegram config --quiet
```

What it does: validates the optional deployment using blank private-runtime
placeholders; it neither starts containers nor reveals real credentials.

During an authorized activation, deregister an existing bot from the hosted
Bot API using `logOut` before moving it to the local server; do not poll the
same bot concurrently on both servers. See the
[official migration instructions](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server).
Files still pass through Telegram. Its local API accepts documents up to
2,000 MB; larger results use an authenticated website link. See the
[official local Bot API limits](https://core.telegram.org/bots/api#using-a-local-bot-api-server).

Notifications report an output becoming ready and each download request,
including filename, bytes, expiry, and a recovery link. Preview fetches are
excluded. They do not prove a phone saved the file. A durable outbox delivers in
its own worker with five bounded attempts; failures cannot stall conversion or
HTTP downloads. A crash after Telegram accepted a message/document but before
local acknowledgement can cause a duplicate delivery because Telegram provides
no send-message idempotency key. Update-to-job correlation prevents that from
creating duplicate processing/download jobs.
