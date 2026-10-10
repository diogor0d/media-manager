"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = process.env.TEST_WEB_BASELINE
  ? require("node:child_process").execFileSync("git", ["show", "HEAD:src/media_manager/web/app.js"], { encoding: "utf8" })
  : fs.readFileSync(path.join(__dirname, "../src/media_manager/web/app.js"), "utf8");

function harness(fetchImpl, storage = memoryStorage()) {
  const elements = new Map();
  const timers = [];
  const intervals = [];
  const makeElement = () => ({
    hidden: true, disabled: false, value: "", textContent: "", firstChild: { textContent: "" }, lastChild: { textContent: "" },
    children: [],
    style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, removeAttribute() {}, addEventListener() {}, append(...children) { this.children.push(...children); }, replaceChildren(...children) { this.children = children; },
    querySelector() { return null; }, querySelectorAll() { return []; }, matches() { return false; },
    focus() {}, click() {}, scrollIntoView() {}, reportValidity() {},
  });
  const document = {
    addEventListener() {},
    querySelector(selector) { if (!elements.has(selector)) elements.set(selector, makeElement()); return elements.get(selector); },
    querySelectorAll() { return []; },
    createElement() { return makeElement(); },
    hidden: false,
  };
  const window = {
    location: { origin: "https://media.example" }, requestAnimationFrame(callback) { callback(); }, confirm() { return false; },
    setTimeout(callback) { timers.push(callback); return timers.length; }, clearTimeout() {},
    setInterval(callback) { intervals.push(callback); return intervals.length; }, clearInterval() {}, addEventListener() {}, matchMedia() { return { matches: false }; },
  };
  const context = {
    document, window, navigator: { onLine: true, userAgent: "", platform: "", maxTouchPoints: 0 },
    sessionStorage: storage, fetch: fetchImpl, URL, URLSearchParams, DOMException, File: class File {},
    XMLHttpRequest: class {}, console, setTimeout, clearTimeout,
  };
  vm.runInNewContext(`${source}\nglobalThis.__app = { state, historyState, loadHistory, saveRetention, deleteHistoryEntry, discardResult, restoreJob, pollJob, startConversion, continueUpload, showResult, reset, shareResult, updateExpiry, resumeAfterReconnect, requestEstimate, startPreparedJob, startDownload, pollDownload, renderHistory, validateStatusUrl, buildProcessingOptions, updateDownloadWorkflow };`, context);
  return { app: context.__app, elements, timers, intervals, navigator: context.navigator, document, window };
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return { getItem(key) { return values.get(key) ?? null; }, setItem(key, value) { values.set(key, value); }, removeItem(key) { values.delete(key); }, values };
}

test("starting another file preserves a ready result but cancelling an unfinished job deletes it", async () => {
  const deleted = [];
  const { app } = harness(async (url, options = {}) => { if (options.method === "DELETE") deleted.push(url); return { ok: true }; });
  app.state.jobId = "ready-job";
  app.state.result = { filename: "result.webp" };
  await app.reset();
  assert.deepEqual(deleted, []);
  app.state.jobId = "unfinished-job";
  app.state.statusUrl = "/v1/jobs/unfinished-job";
  await app.reset();
  assert.deepEqual(deleted, ["/v1/jobs/unfinished-job"]);
});

const historyEntry = (id = "history-1", kind = "conversion") => ({
  id, kind, completed_at: "2026-10-07T12:00:00Z", source_filename: "source.png", job: job(id, "ready"),
});

test("a stale history response cannot replace a newer refresh", async () => {
  let resolveOld;
  let requests = 0;
  const { app } = harness(() => {
    if (++requests === 1) return new Promise(resolve => { resolveOld = resolve; });
    return Promise.resolve(jsonResponse(200, { items: [historyEntry("newer")], total: 1 }));
  });
  app.historyState.settingsLoaded = true;
  const pending = app.loadHistory();
  await app.loadHistory();
  resolveOld(jsonResponse(200, { items: [historyEntry("older")], total: 1 }));
  await pending;
  assert.equal(app.historyState.items[0].id, "newer");
});

test("retention cannot submit twice while saving and reports a failed save", async () => {
  let resolveSave;
  let requests = 0;
  const { app, elements } = harness(() => { requests++; return new Promise(resolve => { resolveSave = resolve; }); });
  app.historyState.settingsLoaded = true;
  elements.set("#retention-days", { value: "14" });
  const pending = app.saveRetention({ preventDefault() {} });
  await app.saveRetention({ preventDefault() {} });
  assert.equal(requests, 1);
  resolveSave(jsonResponse(507, { error: { message: "storage unavailable" } }));
  await pending;
  assert.match(elements.get("#history-message").textContent, /could not be saved/);
  assert.equal(app.historyState.saving, false);
});

test("history deletion requires confirmation, uses compression routes and retains failed deletes", async () => {
  const requests = [];
  const { app, window, elements } = harness(async (url, options = {}) => {
    requests.push([url, options.method]);
    return jsonResponse(503, { error: { message: "temporarily unavailable" } });
  });
  const entry = historyEntry("compression-1", "compression");
  app.historyState.items = [entry];
  await app.deleteHistoryEntry(entry, {});
  assert.deepEqual(requests, []);
  window.confirm = () => true;
  await app.deleteHistoryEntry(entry, {});
  assert.deepEqual(requests, [["/v1/compressions/compression-1", "DELETE"]]);
  assert.equal(app.historyState.items.length, 1);
  assert.match(elements.get("#history-message").textContent, /could not be deleted/);
});

test("history is cleared when the authenticated session expires", async () => {
  const { app } = harness(async () => jsonResponse(401, { error: { message: "sign in" } }));
  app.historyState.settingsLoaded = true;
  app.historyState.items = [historyEntry()];
  app.historyState.total = 1;
  await app.loadHistory();
  assert.equal(app.historyState.items.length, 0);
  assert.equal(app.historyState.total, 0);
  assert.equal(app.historyState.settingsLoaded, false);
});

function jsonResponse(status, body) {
  return { status, ok: status >= 200 && status < 300, url: "https://media.example/v1/jobs/id", redirected: false,
    headers: { get: () => "application/json" }, async json() { return body; } };
}

const job = (id = "job-1", state = "queued") => ({
  id, state, target: "image-webp", quality: "balanced", quality_percent: 50, resolution: "source", audio: "keep",
  input: { bytes: 10 }, output: { bytes: 5, filename: "result.webp", media_type: "image/webp" },
  expires_at: "2099-01-01T00:00:00Z", progress: { stage: "queued", percent: null },
});

test("restoreJob retains recovery for network, auth, and transient server failures", async (t) => {
  for (const [name, failure] of [
    ["network", () => Promise.reject(new Error("offline"))],
    ["auth", async () => jsonResponse(403, { error: { message: "sign in" } })],
    ["503", async () => jsonResponse(503, { error: { message: "busy" } })],
  ]) await t.test(name, async () => {
    const storage = memoryStorage({ "media-manager-active-job": "job-1" });
    const { app } = harness(failure, storage);
    if (name === "auth") await assert.rejects(app.restoreJob());
    else assert.equal(await app.restoreJob(), true);
    assert.equal(storage.getItem("media-manager-active-job"), "job-1");
    assert.equal(app.state.jobId, "job-1");
  });
});

test("restoreJob clears recovery only when the server says the job is gone", async (t) => {
  for (const status of [404, 410]) await t.test(String(status), async () => {
    const storage = memoryStorage({ "media-manager-active-job": "job-1" });
    const { app } = harness(async () => jsonResponse(status, { error: { message: "gone" } }), storage);
    assert.equal(await app.restoreJob(), false);
    assert.equal(storage.getItem("media-manager-active-job"), null);
    assert.equal(app.state.jobId, null);
  });
});

test("a delayed poll after reset cannot render a result or schedule another poll", async () => {
  let resolveFetch;
  const { app, elements, timers } = harness(() => new Promise((resolve) => { resolveFetch = resolve; }));
  app.state.jobId = "job-1";
  const pending = app.pollJob();
  await app.reset(false);
  resolveFetch(jsonResponse(200, job("job-1", "ready")));
  await pending;
  assert.equal(elements.get("#result-view")?.hidden, true);
  assert.equal(timers.length, 0);
  assert.equal(app.state.jobId, null);
});

test("a cancelled upload creation response deletes only its own job", async () => {
  let resolveOldCreate;
  const requests = [];
  const { app, elements } = harness((url, options = {}) => {
    requests.push([url, options]);
    if (url.startsWith("/v1/uploads?") && !resolveOldCreate) return new Promise((resolve) => { resolveOldCreate = resolve; });
    if (url.startsWith("/v1/uploads?")) return Promise.resolve(jsonResponse(201, { id: "new-job", offset: 1, length: 1, chunk_size: 50 }));
    if (url.endsWith("/complete")) return Promise.resolve(jsonResponse(202, job("new-job", "queued")));
    if (url === "/v1/jobs/new-job") return Promise.resolve(jsonResponse(200, job("new-job", "queued")));
    return Promise.resolve({ ok: true });
  });
  app.state.file = { name: "old.png", size: 10 };
  app.state.capabilities = { targets: [{ value: "image-webp", extension: "webp" }] };
  elements.set('input[name="target"]:checked', { value: "image-webp" });
  elements.set("#quality-slider", { value: "50" });
  elements.set("#resolution-select", { value: "source" });
  const pendingOld = app.startConversion({ preventDefault() {} });
  await app.reset(false);
  app.state.file = { name: "new.png", size: 20 };
  const pendingNew = app.startConversion({ preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(app.state.jobId, "new-job");
  resolveOldCreate(jsonResponse(201, { id: "old-job", offset: 0, length: 10, chunk_size: 50 }));
  await Promise.all([pendingOld, pendingNew]);
  assert.equal(app.state.jobId, "new-job");
  assert.equal(app.state.file.name, "new.png");
  assert.deepEqual(requests.filter(([, options]) => options.method === "DELETE").map(([url]) => url), ["/v1/jobs/old-job"]);
});

test("storage failures do not break showing a result or resetting", async () => {
  const storage = { getItem() { return null; }, setItem() { throw new Error("storage disabled"); }, removeItem() { throw new Error("storage disabled"); } };
  const { app } = harness(async () => jsonResponse(200, job("job-1", "ready")), storage);
  app.state.capabilities = { targets: [{ value: "image-webp", extension: "webp" }] };
  assert.doesNotThrow(() => app.showResult(job("job-1", "ready")));
  await assert.doesNotReject(app.reset(false));
});

test("discarding while a share fetch is pending keeps the reset view", async () => {
  let resolveFetch;
  let shareCalls = 0;
  const { app, elements, navigator } = harness(() => new Promise((resolve) => { resolveFetch = resolve; }));
  navigator.canShare = () => true;
  navigator.share = async () => { shareCalls += 1; };
  app.state.jobId = "job-1";
  app.state.result = { filename: "result.webp", media_type: "image/webp" };
  const pending = app.shareResult();
  await app.reset(false);
  app.state.jobId = "new-job";
  app.state.result = { filename: "new-result.webp", media_type: "image/webp" };
  resolveFetch({ ok: true, async blob() { return Buffer.from("image"); } });
  await pending;
  assert.equal(app.state.jobId, "new-job");
  assert.equal(app.state.result.filename, "new-result.webp");
  assert.equal(elements.get("#drop-zone").hidden, false);
  assert.equal(elements.get("#result-view").hidden, true);
  assert.equal(shareCalls, 0);
});

test("expired results stay disabled after reconnect and a fresh result becomes available", () => {
  const storage = memoryStorage({ "media-manager-active-job": "job-1" });
  const { app, elements, intervals } = harness(async () => {}, storage);
  app.state.capabilities = { targets: [{ value: "image-webp", extension: "webp" }] };
  app.state.jobId = "job-1";
  app.state.result = { filename: "result.webp" };
  app.updateExpiry("2000-01-01T00:00:00Z");
  assert.equal(intervals.length, 1);
  intervals[0]();
  assert.equal(storage.getItem("media-manager-active-job"), null);
  assert.equal(app.state.result, null);
  app.resumeAfterReconnect();
  assert.equal(elements.get("#share-button").disabled, true);
  assert.equal(elements.get("#download-button").tabIndex, -1);
  app.showResult(job("new-job", "ready"));
  assert.equal(elements.get("#share-button").disabled, false);
  assert.equal(elements.get("#download-button").tabIndex, 0);
  assert.equal(elements.get("#download-button").firstChild.textContent, "Open ");
});

test("reconnect keeps a pending share disabled and prevents duplicate downloads", async () => {
  let resolveFetch;
  let downloads = 0;
  const { app, elements, navigator } = harness(() => {
    downloads += 1;
    return new Promise((resolve) => { resolveFetch = resolve; });
  });
  navigator.canShare = () => true;
  navigator.share = async () => {};
  app.state.capabilities = { targets: [{ value: "image-webp", extension: "webp" }] };
  app.state.jobId = "job-1";
  app.state.result = { filename: "result.webp", media_type: "image/webp" };
  const pending = app.shareResult();
  app.resumeAfterReconnect();
  assert.equal(elements.get("#share-button").disabled, true);
  await app.shareResult();
  assert.equal(downloads, 1);
  resolveFetch({ ok: true, async blob() { return Buffer.from("image"); } });
  await pending;
  assert.equal(elements.get("#share-button").disabled, false);
});

test("reconnect resumes a retained job status poll", async () => {
  const requests = [];
  const { app, navigator } = harness(async (url) => {
    requests.push(url);
    return jsonResponse(200, job("job-1", "ready"));
  });
  app.state.jobId = "job-1";
  app.state.capabilities = { targets: [{ value: "image-webp", extension: "webp" }] };
  app.state.resumeAction = "poll";
  navigator.onLine = true;
  app.resumeAfterReconnect();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests, ["/v1/jobs/job-1"]);
  assert.equal(app.state.result.filename, "result.webp");
});

test("a lost prepare reply or UPLOAD_ALREADY_COMPLETED is recovered before estimate review", async (t) => {
  const id = "82f353b4-33ec-41dd-8466-a1a0271e398f";
  const statusUrl = `/v1/jobs/${id}`;
  const prepared = { ...job(id, "awaiting_review"), status_url: statusUrl };
  for (const mode of ["network", "already-completed"]) await t.test(mode, async () => {
    const calls = [];
    const { app } = harness(async (url, options = {}) => {
      calls.push([url, options.method || "GET"]);
      if (url.endsWith("/prepare")) {
        if (mode === "network") throw new Error("reply lost");
        return jsonResponse(409, { error: { code: "UPLOAD_ALREADY_COMPLETED", message: "already complete" } });
      }
      if (url === statusUrl) return jsonResponse(200, prepared);
      if (url === `/v1/jobs/${id}/estimate`) return jsonResponse(200, { size_min_bytes: 1, size_max_bytes: 2, eta_min_seconds: 1, eta_max_seconds: 2, method: "fast", confidence: "low", options_key: "test" });
      throw new Error(`unexpected request ${url}`);
    });
    app.state.jobId = id;
    app.state.operation = 1;
    app.state.upload = { id, offset: 10, length: 10, chunk_size: 50 };
    app.state.file = { name: "sample.png", size: 10, slice() { return {}; } };
    app.state.capabilities = { targets: [{ value: "image-webp", extension: "webp" }] };
    await app.continueUpload({ value: "image-webp", extension: "webp" });
    assert.equal(calls.filter(([url, method]) => url.endsWith("/prepare") && method === "POST").length, 1);
    assert.deepEqual(calls.slice(0, 3).map(([url, method]) => [url, method]), [
      [`/v1/uploads/${id}/prepare`, "POST"], [statusUrl, "GET"], [`/v1/jobs/${id}/estimate`, "POST"],
    ]);
    assert.equal(app.state.statusUrl, statusUrl);
  });
});

test("prepared jobs show an estimate before the explicit start request", async () => {
  const id = "82f353b4-33ec-41dd-8466-a1a0271e398f";
  const statusUrl = `/v1/jobs/${id}`;
  const requests = [];
  const ready = { ...job(id, "ready"), status_url: statusUrl };
  const { app, elements } = harness(async (url, options = {}) => {
    requests.push([url, options.method || "GET", options.body]);
    if (url === `/v1/jobs/${id}/estimate`) return jsonResponse(200, { size_min_bytes: 100, size_max_bytes: 200, eta_min_seconds: 2, eta_max_seconds: 4, method: "fast", confidence: "low", options_key: "key-1" });
    if (url === `/v1/jobs/${id}/start`) return jsonResponse(202, { ...job(id, "queued"), status_url: statusUrl });
    if (url === statusUrl) return jsonResponse(200, ready);
    throw new Error(`unexpected request ${url}`);
  });
  app.state.jobId = id; app.state.statusUrl = statusUrl; app.state.operation = 1;
  app.state.capabilities = { targets: [{ value: "image-webp", extension: "webp" }] };
  app.state.processingOptions = { conversion_options: { target: "image-webp", quality_percent: 50, resolution: "source", audio: "keep" }, compression_options: null };
  await app.requestEstimate({ id }, false);
  assert.equal(elements.get("#estimate-review").hidden, false);
  assert.deepEqual(requests.map(([url, method]) => [url, method]), [[`/v1/jobs/${id}/estimate`, "POST"]]);
  await app.startPreparedJob();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests.slice(1).map(([url, method]) => [url, method]), [[`/v1/jobs/${id}/start`, "POST"], [statusUrl, "GET"]]);
  assert.equal(JSON.parse(requests[0][2]).conversion_options.target, "image-webp");
});

test("remote download submissions carry the requested kind and workflow options", async () => {
  const id = "82f353b4-33ec-41dd-8466-a1a0271e398f";
  const statusUrl = `/v1/downloads/${id}`;
  const requests = [];
  const { app, elements } = harness(async (url, options = {}) => {
    requests.push([url, options.method || "GET", options.body]);
    if (url === "/v1/downloads") return jsonResponse(202, { id, state: "queued", status_url: statusUrl });
    if (url === statusUrl) return jsonResponse(200, { id, state: "awaiting_review", status_url: statusUrl, source_filename: "clip.mp4", output: { filename: "clip.mp4", bytes: 100 } });
    throw new Error(`unexpected request ${url}`);
  });
  app.state.capabilities = { downloads_enabled: true };
  const set = (selector, value, extra = {}) => elements.set(selector, { hidden: false, disabled: false, value, checked: false, textContent: "", dataset: {}, classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, ...extra });
  set("#media-url", "https://video.example/watch?v=1");
  set("#download-type", "audio");
  set("#download-quality", "720");
  set("#download-format", "mp3");
  set("#download-codec", "opus");
  set("#download-workflow", "compress");
  set("#download-auto-process", "", { checked: true });
  set("#download-target", "audio-mp3");
  set("#download-quality-percent", "65");
  set("#download-resolution", "source");
  set("#download-audio", "keep");
  set("#download-target-bytes", "20000000");
  set("#download-submit", "");
  set("#download-review", "");
  set("#download-review-detail", "");
  set("#download-source-button", "", { href: "" });
  set("#download-process-button", "");
  set("#download-discard-button", "");
  await app.startDownload({ preventDefault() {} });
  const payload = JSON.parse(requests[0][2]);
  assert.equal(payload.download_type, "audio");
  assert.equal(payload.quality, "720");
  assert.equal(payload.format, "mp3");
  assert.equal(payload.codec, "opus");
  assert.equal(payload.workflow, "compress");
  assert.equal(payload.auto_process, true);
  assert.equal(payload.compression_options.target_bytes, 20000000);
  assert.equal(requests[1][0], statusUrl);
  assert.match(elements.get("#download-review-detail").textContent, /Ready to review/);
});

test("ready or reviewable downloads can be processed after choosing a workflow", () => {
  const { app, elements } = harness(async () => jsonResponse(200, {}));
  app.state.downloadState = "ready";
  elements.set("#download-workflow", { value: "download" });
  elements.set(".download-process-options", { hidden: false });
  elements.set("#download-auto-process", { disabled: false, checked: true });
  elements.set("#download-process-button", { hidden: false });
  app.updateDownloadWorkflow();
  assert.equal(elements.get("#download-process-button").hidden, true);
  elements.get("#download-workflow").value = "compress";
  app.updateDownloadWorkflow();
  assert.equal(elements.get("#download-process-button").hidden, false);
  app.state.downloadState = "awaiting_review";
  app.updateDownloadWorkflow();
  assert.equal(elements.get("#download-process-button").hidden, false);
});

test("estimate review includes a signed size-change range and confidence", async () => {
  const id = "82f353b4-33ec-41dd-8466-a1a0271e398f";
  const { app, elements } = harness(async () => jsonResponse(200, {
    size_min_bytes: 600, size_max_bytes: 1200, source: { bytes: 1000 }, confidence: "medium", method: "sampled",
  }));
  app.state.jobId = id; app.state.statusUrl = `/v1/jobs/${id}`; app.state.operation = 1;
  app.state.processingOptions = {};
  await app.requestEstimate({ id }, false);
  assert.match(elements.get("#estimate-summary").textContent, /20% larger to 40% smaller/);
  assert.match(elements.get("#estimate-summary").textContent, /medium confidence/);
});

test("unknown source submission is reported as confirmation, not active downloading", async () => {
  const id = "82f353b4-33ec-41dd-8466-a1a0271e398f";
  const { app, elements, timers } = harness(async () => jsonResponse(200, {
    id, state: "queued", submission_unknown: true, status_url: `/v1/downloads/${id}`,
  }));
  app.state.downloadStatusUrl = `/v1/downloads/${id}`;
  await app.pollDownload();
  assert.match(elements.get("#download-review-detail").textContent, /Confirming whether the source accepted/);
  assert.doesNotMatch(elements.get("#download-review-detail").textContent, /Downloading from the source/);
  assert.equal(timers.length, 1);
});

test("history marks unknown source acceptance without showing a download ETA", () => {
  const { app, elements } = harness(async () => jsonResponse(200, {}));
  app.historyState.items = [{
    id: "82f353b4-33ec-41dd-8466-a1a0271e398f", kind: "download", completed_at: null,
    source_filename: "clip.mp4", job: {
      id: "82f353b4-33ec-41dd-8466-a1a0271e398f", state: "queued", submission_unknown: true,
      status_url: "/v1/downloads/82f353b4-33ec-41dd-8466-a1a0271e398f", processing: { eta_seconds: 30 },
    },
  }];
  elements.set("#history-list", { children: [], replaceChildren() { this.children = []; }, append(...children) { this.children.push(...children); } });
  app.renderHistory();
  const row = elements.get("#history-list").children[0];
  const metadata = row.children[0].children[1].textContent;
  assert.match(metadata, /Confirming source acceptance/);
  assert.doesNotMatch(metadata, /about .* left/);
  assert.equal(row.children[1].children[0].textContent, "View status");
});

for (const sourceState of ["ready", "processing", "awaiting_review"]) test(`history groups original ${sourceState} and processed child with recoverable outputs`, () => {
  const { app, elements } = harness(async () => jsonResponse(200, {}));
  const originalId = "82f353b4-33ec-41dd-8466-a1a0271e398f";
  const childId = "c5c993e7-983e-4cd0-b22e-1a44e45b6188";
  app.historyState.items = [
    { id: originalId, kind: "download", child_id: childId, completed_at: "2026-10-09T10:00:00Z", job: { id: originalId, state: sourceState, output: { filename: "source.mp4", bytes: 100 }, expires_at: "2026-10-16T10:00:00Z" } },
    { id: childId, kind: "conversion", parent_id: originalId, completed_at: "2026-10-09T10:01:00Z", job: { id: childId, parent_id: originalId, state: "ready", output: { filename: "result.webp", bytes: 50 }, expires_at: "2026-10-16T10:01:00Z" } },
  ];
  elements.set("#history-list", { children: [], replaceChildren() { this.children = []; }, append(...children) { this.children.push(...children); } });
  app.renderHistory();
  const rows = elements.get("#history-list").children;
  assert.equal(rows.length, 1);
  const stages = rows[0].children[0].children.find(child => child.className === "history-lineage-stages");
  assert.deepEqual(stages.children.map(stage => stage.textContent), ["Original · source.mp4", "Processed · result.webp · ready"]);
  assert.ok(rows[0].children[1].children.some(action => action.textContent === "Download processed"));
  assert.ok(rows[0].children[1].children.some(action => action.textContent === "Download source"));
});

test("processing failure keeps a recoverable original download available", async () => {
  const id = "82f353b4-33ec-41dd-8466-a1a0271e398f";
  const { app, elements } = harness(async () => jsonResponse(200, {
    id, state: "awaiting_review", error: { code: "PROCESSING_FAILED", message: "Conversion failed" },
    source_filename: "clip.mp4", status_url: `/v1/downloads/${id}`,
    output: { filename: "clip.mp4", bytes: 100, download_url: `/v1/downloads/${id}/content` },
  }));
  app.state.downloadStatusUrl = `/v1/downloads/${id}`;
  elements.set("#download-workflow", { value: "convert" });
  elements.set("#download-process-button", { hidden: true });
  elements.set("#download-source-button", { hidden: false, setAttribute() {} });
  await app.pollDownload();
  assert.equal(app.state.downloadState, "awaiting_review");
  assert.match(elements.get("#download-review-detail").textContent, /Original download preserved/);
  assert.match(elements.get("#download-review-detail").textContent, /Conversion failed/);
  assert.equal(elements.get("#download-process-button").hidden, false);
  assert.equal(elements.get("#download-source-button").href, "/v1/downloads/82f353b4-33ec-41dd-8466-a1a0271e398f/content");
});

test("status URLs reject cross-origin, mismatched, and malformed destinations", () => {
  const { app } = harness(async () => jsonResponse(200, {}));
  const id = "82f353b4-33ec-41dd-8466-a1a0271e398f";
  assert.equal(app.validateStatusUrl(`/v1/compressions/${id}`, ["jobs", "compressions"], id), `/v1/compressions/${id}`);
  assert.throws(() => app.validateStatusUrl(`https://evil.example/v1/jobs/${id}`, "jobs", id), /unsafe status URL/);
  assert.throws(() => app.validateStatusUrl(`/v1/jobs/${id}/content`, "jobs", id), /unexpected status URL/);
  assert.throws(() => app.validateStatusUrl("/v1/jobs/job-1", "jobs", "job-1"), /invalid job identity/);
});
