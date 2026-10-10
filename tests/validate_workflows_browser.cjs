/* Review-first workflow acceptance against an isolated loopback Media Manager. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium, webkit } = require("playwright");

const origin = process.env.MEDIA_MANAGER_TEST_URL || "http://127.0.0.1:8765";
assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(origin).hostname), "Use loopback only");
const ffmpegDir = process.env.MEDIA_MANAGER_TEST_FFMPEG_DIR || "/private/tmp/media-manager-glass-tools";
const ffmpeg = path.join(ffmpegDir, "ffmpeg");
const engineName = process.env.MEDIA_MANAGER_TEST_BROWSER || "chromium";
const engine = engineName === "webkit" ? webkit : chromium;
const checks = [];

async function main() {
  const artifacts = process.env.MEDIA_MANAGER_TEST_ARTIFACT_DIR || await fs.mkdtemp(path.join(os.tmpdir(), "media-manager-workflows-"));
  await fs.mkdir(artifacts, { recursive: true });
  const fixtureDir = await fs.mkdtemp(path.join(os.tmpdir(), "media-manager-workflow-fixture-"));
  const fixture = path.join(fixtureDir, `workflow-${Date.now()}.png`);
  const fixtureToken = path.basename(fixture, ".png");
  execFileSync(ffmpeg, ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=1", "-frames:v", "1", "-y", fixture]);
  const executablePath = engineName === "webkit" ? process.env.PLAYWRIGHT_WEBKIT_EXECUTABLE : process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  const browser = await engine.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  const context = await browser.newContext({ viewport: { width: 1366, height: 900 }, acceptDownloads: true, colorScheme: "light" });
  const page = await context.newPage();
  const errors = [];
  const captureWarnings = [];
  const capturing = new Set();
  const deletes = [];
  const external = [];
  const requests = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() !== "error" || message.text().includes("Failed to load resource")) return;
    if (engineName === "webkit" && capturing.has(page) && message.text().startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'")) captureWarnings.push(message.text());
    else errors.push(message.text());
  });
  page.on("request", request => {
    requests.push(request);
    if (process.env.MEDIA_MANAGER_TEST_VERBOSE) console.log("request", request.method(), request.url());
    if (request.method() === "DELETE") deletes.push(request.url());
    if (!request.url().startsWith(origin) && !request.url().startsWith("blob:")) external.push(request.url());
  });
  page.on("requestfailed", request => { if (process.env.MEDIA_MANAGER_TEST_VERBOSE) console.log("request-failed", request.method(), request.url(), request.failure()?.errorText); });
  const $ = id => page.locator(`#${id}`);
  async function screenshot(name, observedPage = page) {
    capturing.add(observedPage);
    try { await observedPage.screenshot({ path: path.join(artifacts, `${name}.png`), fullPage: true }); }
    finally { capturing.delete(observedPage); }
  }
  async function noOverflowOn(observedPage, label) {
    const size = await observedPage.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(size.scroll <= size.width + 1, `${label}: horizontal overflow ${JSON.stringify(size)}`);
  }
  async function noOverflow(label) { return noOverflowOn(page, label); }
  try {
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelector("#limit-copy").textContent.startsWith("Up to"));
    assert.equal(await page.locator('#mode-switch button[data-mode="download"]').isDisabled(), true, "URL mode is disabled without the sidecar capability");
    assert.equal(await page.locator('#mode-switch button[data-mode="convert"]').isDisabled(), false, "Local conversion remains available");
    await screenshot("workflow-empty-desktop");
    for (const size of [{ name: "phone", width: 320, height: 700 }, { name: "phone-large", width: 390, height: 844 }, { name: "tablet", width: 834, height: 1112 }, { name: "desktop", width: 1440, height: 900 }]) {
      await page.setViewportSize({ width: size.width, height: size.height });
      await noOverflow(`empty ${size.name}`);
      if (size.name !== "desktop") await screenshot(`workflow-empty-${size.name}`);
    }
    await page.setViewportSize({ width: 1366, height: 900 });
    await page.locator('#mode-switch button[data-mode="compress"]').click();
    assert.equal(await $("workflow-select").inputValue(), "compress");
    await screenshot("workflow-compress-empty");

    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Choose a media file or drop one here" }).click();
    await (await chooser).setFiles(fixture);
    await $("workspace").waitFor({ state: "visible" });
    await $("workflow-select").selectOption("convert_compress");
    assert.equal(await $("conversion-quality-options").isVisible(), false, "combined workflow uses the advanced compression quality and resolution controls");
    assert.equal(await $("compression-options").isVisible(), true, "combined workflow keeps advanced compression controls available");
    await $("compression-target-bytes").fill("250000");
    await $("compression-resolution").selectOption("480p");
    await $("compression-audio").selectOption("keep");
    await $("compression-quality").fill("76");
    assert.equal(await $("compression-quality-value").innerText(), "76%");
    const estimatePost = page.waitForRequest(request => request.method() === "POST" && request.url().endsWith("/estimate"));
    const createResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().includes("/v1/uploads?"));
    await $("convert-button").click();
    const firstEstimate = await estimatePost;
    const estimateBody = firstEstimate.postDataJSON();
    assert.equal(estimateBody.conversion_options.target, "image-webp");
    assert.equal(estimateBody.conversion_options.quality_percent, 50);
    assert.equal(estimateBody.compression_options.target_bytes, 250000);
    assert.equal(estimateBody.compression_options.target, "image-webp");
    assert.equal(estimateBody.compression_options.resolution, "480p");
    assert.equal(estimateBody.compression_options.audio, "keep");
    assert.equal(estimateBody.compression_options.quality_percent, 76);
    const uploadResponse = await createResponse;
    const prepared = await uploadResponse.json();
    assert.equal(uploadResponse.status(), 201, `upload creation failed: ${JSON.stringify(prepared)}`);
    assert.match(prepared.id, /^[0-9a-f-]{36}$/i, "upload preparation returns its reusable job identity");
    assert.equal(uploadResponse.request().headers()["x-media-source"], "web", "browser uploads declare their source for merged history");
    await $("estimate-review").waitFor({ state: "visible", timeout: 30000 });
    assert.equal(await $("convert-button").isVisible(), false, "the upload CTA is removed while the prepared estimate is awaiting a decision");
    assert.match(await $("estimate-summary").innerText(), /estimate|bytes|KB|MB/i);
    assert.equal(await $("start-job-button").isDisabled(), false);
    assert.ok(!requests.some(request => request.method() === "POST" && request.url().endsWith("/start")), "estimate must not start processing");
    await screenshot("workflow-estimate-desktop");
    for (const size of [{ name: "phone", width: 320, height: 700 }, { name: "tablet", width: 834, height: 1112 }, { name: "desktop", width: 1440, height: 900 }]) {
      await page.setViewportSize({ width: size.width, height: size.height });
      await noOverflow(`estimate ${size.name}`);
      await screenshot(`workflow-estimate-${size.name}`);
    }

    const beforeCloseDeletes = deletes.length;
    await page.close();
    assert.equal(deletes.length, beforeCloseDeletes, "closing the browser page must not discard the prepared job");
    const reopened = await context.newPage();
    reopened.on("pageerror", error => errors.push(error.message));
    reopened.on("console", message => {
      if (message.type() !== "error" || message.text().includes("Failed to load resource")) return;
      if (engineName === "webkit" && capturing.has(reopened) && message.text().startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'")) captureWarnings.push(message.text());
      else errors.push(message.text());
    });
    reopened.on("request", request => { requests.push(request); if (request.method() === "DELETE") deletes.push(request.url()); if (!request.url().startsWith(origin) && !request.url().startsWith("blob:")) external.push(request.url()); });
    await reopened.goto(origin);
    if (await reopened.locator("#history-button").getAttribute("aria-expanded") !== "true") await reopened.locator("#history-button").click();
    const row = reopened.locator("#history-list .history-row").filter({ hasText: fixtureToken });
    await row.waitFor({ state: "visible" });
    await row.getByRole("button", { name: "Review estimate" }).click();
    await reopened.locator("#estimate-review").waitFor({ state: "visible" });
    assert.equal(await reopened.locator("#start-job-button").isVisible(), true, "history reopens the prepared estimate");
    await reopened.setViewportSize({ width: 390, height: 844 });
    await noOverflowOn(reopened, "reopened estimate mobile");
    await reopened.locator("#start-job-button").click();
    await reopened.locator("#result-view").waitFor({ state: "visible", timeout: 90000 });
    const startRequest = requests.find(request => request.method() === "POST" && request.url().endsWith("/start"));
    assert.ok(startRequest, "processing starts only after the review action");
    assert.equal(startRequest.postDataJSON().conversion_options, null);
    assert.equal(startRequest.postDataJSON().compression_options, null, "the prepared settings are reused after history restore");
    assert.equal(deletes.length, beforeCloseDeletes, "neither closing nor starting the restored job deletes it");
    await screenshot("workflow-result-mobile", reopened);
    const downloadEvent = reopened.waitForEvent("download");
    await reopened.locator("#download-button").click();
    const result = await downloadEvent;
    assert.equal(await result.failure(), null);
    await result.saveAs(path.join(artifacts, "workflow-result.webp"));
    if (await reopened.locator("#history-button").getAttribute("aria-expanded") !== "true") await reopened.locator("#history-button").click();
    const completed = reopened.locator("#history-list .history-row").filter({ hasText: fixtureToken });
    await completed.waitFor({ state: "visible" });
    assert.equal(await completed.getByRole("link", { name: /Download/ }).count(), 1, "history exposes the completed output");
    const originalRetention = await reopened.locator("#retention-days").inputValue();
    await reopened.locator("#retention-days").fill(originalRetention === "7" ? "6" : "7");
    await reopened.locator("#retention-save").click();
    await reopened.getByText(/Saved\. New results will be kept/).waitFor();
    await reopened.locator("#retention-days").fill(originalRetention);
    await reopened.locator("#retention-save").click();
    await reopened.getByText(/Saved\. New results will be kept/).waitFor();
    checks.push("Real PNG upload: advanced convert+compress options reach estimate, processing waits for Start, prepared settings survive page close/history restore, result downloads from history, retention setting saves and is restored");

    await reopened.locator("#history-button").click();
    const activeMode = reopened.locator('#mode-switch button[data-mode="download"]');
    assert.equal(await activeMode.isDisabled(), true, "URL form remains disabled without a configured sidecar");
    await reopened.locator('#mode-switch button[data-mode="convert"]').click();
    await reopened.locator("#another-button").click();
    await reopened.locator("#drop-zone").waitFor({ state: "visible" });
    checks.push("Capabilities correctly disable URL downloads while leaving local file conversion usable; no download mock used");
    await screenshot("workflow-final-desktop", reopened);

    const mockContext = await browser.newContext({ viewport: { width: 1366, height: 900 }, serviceWorkers: "block" });
    const mockPage = await mockContext.newPage();
    const mockDownloadId = "82f353b4-33ec-41dd-8466-a1a0271e398f";
    const mockJobId = "c5c993e7-983e-4cd0-b22e-1a44e45b6188";
    const mockProcessRequests = [];
    const mockRequests = [];
    const mockResponses = [];
    const mockPageErrors = [];
    mockPage.on("request", request => mockRequests.push(`${request.method()} ${new URL(request.url()).pathname}`));
    mockPage.on("response", response => mockResponses.push(`${response.status()} ${new URL(response.url()).pathname}`));
    mockPage.on("pageerror", error => mockPageErrors.push(error.message));
    await mockContext.route("**/v1/capabilities", async route => {
      const response = await route.fetch();
      const capabilities = await response.json();
      capabilities.downloads_enabled = true;
      await route.fulfill({ response, json: capabilities });
    });
    await mockContext.route("**/v1/downloads", async route => {
      await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ id: mockDownloadId, state: "queued", status_url: `/v1/downloads/${mockDownloadId}` }) });
    });
    await mockContext.route(`**/v1/downloads/${mockDownloadId}`, async route => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: mockDownloadId, state: "ready", status_url: `/v1/downloads/${mockDownloadId}`, source_filename: "mock-source.mp4", output: { filename: "mock-source.mp4", bytes: 1000000, download_url: `/v1/downloads/${mockDownloadId}/content` } }) });
    });
    await mockContext.route(`**/v1/downloads/${mockDownloadId}/process`, async route => {
      mockProcessRequests.push(route.request().postDataJSON());
      await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ id: mockJobId, state: "awaiting_review", status_url: `/v1/jobs/${mockJobId}` }) });
    });
    await mockContext.route(`**/v1/jobs/${mockJobId}/estimate`, async route => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ size_min_bytes: 400000, size_max_bytes: 600000, eta_min_seconds: 3, eta_max_seconds: 8, confidence: "low", method: "fast", source: { bytes: 1000000 }, options_key: "mock" }) });
    });
    await mockPage.goto(origin);
    await mockPage.waitForFunction(() => document.querySelector("#limit-copy").textContent.startsWith("Up to"));
    await mockPage.locator('#mode-switch button[data-mode="download"]').click();
    await mockPage.locator("#media-url").fill("https://media.example/mock");
    await mockPage.locator("#download-workflow").selectOption("download");
    await mockPage.locator("#download-submit").click();
    await mockPage.waitForFunction(() => document.querySelector("#download-review-detail").textContent.includes("Download ready"), null, { timeout: 8000 }).catch(() => {});
    const mockReadyDetail = await mockPage.locator("#download-review-detail").innerText();
    const mockState = await mockPage.evaluate(() => ({ online: navigator.onLine, detail: document.querySelector("#download-review-detail").textContent, error: document.querySelector("#error-title").textContent }));
    assert.match(mockReadyDetail, /Download ready.*mock-source\.mp4/, `Mock download did not reach READY; state=${JSON.stringify(mockState)}; requests=${mockRequests.join(" | ")}; responses=${mockResponses.join(" | ")}; pageErrors=${mockPageErrors.join(" | ")}`);
    assert.equal(await mockPage.locator("#download-process-button").isVisible(), false, "download-only output has no processing action before a workflow is chosen");
    await mockPage.locator("#download-workflow").selectOption("convert_compress");
    assert.equal(await mockPage.locator("#download-process-button").isVisible(), true, "a ready download can be processed after its original download-only review");
    await mockPage.locator("#download-process-button").click();
    await mockPage.locator("#estimate-review").waitFor({ state: "visible" });
    assert.equal(mockProcessRequests.length, 1);
    assert.equal(mockProcessRequests[0].auto_start, false);
    assert.match(await mockPage.locator("#estimate-summary").innerText(), /40% smaller to 60% smaller.*low confidence/);
    checks.push("Mock-only URL workflow: existing download-only READY output exposes processing after workflow selection and opens estimate review without auto-start");
    await mockContext.close();

    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    const report = { browser: await browser.version(), engine: engineName, passed: checks, artifacts, downloads_enabled: false, browser_close_delete_requests: beforeCloseDeletes, failures: [], captureWarnings: captureWarnings.length ? { count: captureWarnings.length, detail: "Playwright WebKit screenshot helper inline styles were rejected by the strict CSP during capture only." } : null };
    await fs.writeFile(path.join(artifacts, "validation.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
  } finally {
    await context.close();
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
