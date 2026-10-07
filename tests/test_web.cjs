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
    style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, removeAttribute() {}, addEventListener() {}, append() {}, replaceChildren() {},
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
  vm.runInNewContext(`${source}\nglobalThis.__app = { state, historyState, loadHistory, saveRetention, deleteHistoryEntry, discardResult, restoreJob, pollJob, startConversion, continueUpload, showResult, reset, shareResult, updateExpiry, resumeAfterReconnect };`, context);
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

test("a lost completion reply or UPLOAD_ALREADY_COMPLETED is recovered by checking the accepted job", async (t) => {
  for (const mode of ["network", "already-completed"]) await t.test(mode, async () => {
    const calls = [];
    const { app } = harness(async (url, options = {}) => {
      calls.push([url, options.method || "GET"]);
      if (url.endsWith("/complete")) {
        if (mode === "network") throw new Error("reply lost");
        return jsonResponse(409, { error: { code: "UPLOAD_ALREADY_COMPLETED", message: "already complete" } });
      }
      if (url === "/v1/jobs/job-1") return jsonResponse(200, job("job-1", "queued"));
      throw new Error(`unexpected request ${url}`);
    });
    app.state.jobId = "job-1";
    app.state.upload = { id: "job-1", offset: 10, length: 10, chunk_size: 50 };
    app.state.file = { name: "sample.png", size: 10, slice() { return {}; } };
    app.state.capabilities = { targets: [{ value: "image-webp", extension: "webp" }] };
    await app.continueUpload({ value: "image-webp", extension: "webp" });
    assert.equal(calls.filter(([url, method]) => url.endsWith("/complete") && method === "POST").length, 1);
    assert.deepEqual(calls.slice(0, 2).map(([url, method]) => [url, method]), [
      ["/v1/uploads/job-1/complete", "POST"], ["/v1/jobs/job-1", "GET"],
    ]);
  });
});
