/* History acceptance against an isolated loopback server; Playwright and FFmpeg required. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium, webkit, devices } = require("playwright");

async function main() {
  const origin = process.env.MEDIA_MANAGER_TEST_URL || "http://127.0.0.1:8766";
  assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(origin).hostname), "Use an isolated loopback server");
  const tools = process.env.MEDIA_MANAGER_TEST_FFMPEG_DIR;
  const ffmpeg = tools ? path.join(tools, "ffmpeg") : "ffmpeg";
  const ffprobe = tools ? path.join(tools, "ffprobe") : "ffprobe";
  const artifacts = process.env.MEDIA_MANAGER_TEST_ARTIFACT_DIR || await fs.mkdtemp(path.join(os.tmpdir(), "media-manager-history-"));
  await fs.mkdir(artifacts, { recursive: true });
  const fixture = path.join(artifacts, "source.png");
  execFileSync(ffmpeg, ["-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90", "-frames:v", "1", "-y", fixture]);
  const source = await fs.readFile(fixture);
  const engine = process.env.MEDIA_MANAGER_TEST_BROWSER === "webkit" ? webkit : chromium;
  const browser = await engine.launch({ executablePath: engine === chromium ? process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined : undefined });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  const ids = [];
  const checks = [];
  const previousRetention = (await (await context.request.get(`${origin}/v1/history/settings`)).json()).retention_days;
  page.on("pageerror", error => errors.push(error.message));
  async function history() {
    const response = page.waitForResponse(value => new URL(value.url()).pathname === "/v1/history");
    await page.locator("#history-refresh").click();
    const body = await (await response).json();
    await page.waitForFunction(total => document.querySelector("#history-message").textContent === `${total} ${total === 1 ? "result" : "results"}`, body.total);
  }
  async function layout(observedPage, label) {
    const overflow = await observedPage.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    assert.equal(overflow, false, `${label}: horizontal overflow`);
    const small = await observedPage.locator("#history-panel button:visible, #history-panel input:visible, #history-panel a:visible").evaluateAll(elements => elements.filter(element => element.getBoundingClientRect().height < 43.5).map(element => element.textContent));
    assert.deepEqual(small, [], `${label}: touch target shorter than44px`);
  }
  try {
    assert.equal((await context.request.put(`${origin}/v1/history/settings`, { data: { retention_days: 7 } })).status(), 200);
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelectorAll("#format-guide button").length === 9);
    await page.locator("#history-button").click();
    await page.getByText("No saved results yet. Completed conversions appear here.", { exact: true }).waitFor();
    assert.equal(await page.locator("#retention-days").inputValue(), "7");
    await page.locator("#retention-days").fill("9");
    await page.locator("#retention-save").click();
    await page.getByText("Saved. New results will be kept for 9 days.", { exact: true }).waitFor();
    checks.push("Empty history and persisted in-app retention setting");
    for (const filename of ["a-long-source-filename-".repeat(12) + ".png", "second.png"]) {
      await page.locator("#file-input").setInputFiles({ name: filename, mimeType: "image/png", buffer: source });
      await page.locator("#workspace").waitFor({ state: "visible" });
      await page.locator("#convert-button").click();
      await page.locator("#result-view").waitFor({ state: "visible", timeout: 30000 });
      const href = await page.locator("#download-button").getAttribute("href");
      ids.push({ id: href.split("/")[3], kind: "jobs" });
      await page.locator("#another-button").click();
      await page.locator("#drop-zone").waitFor({ state: "visible" });
    }
    const compression = await context.request.post(`${origin}/v1/compressions`, { data: source, headers: { "Content-Type": "image/png", "Upload-Filename": "compressed.png" } });
    assert.equal(compression.status(), 202);
    const compressedId = (await compression.json()).id;
    ids.push({ id: compressedId, kind: "compressions" });
    const deadline = Date.now() + 30000;
    while (true) {
      const status = await (await context.request.get(`${origin}/v1/compressions/${compressedId}`)).json();
      assert.notEqual(status.state, "failed", status.error?.message);
      if (status.state === "ready") break;
      assert.ok(Date.now() < deadline, "Compression did not finish");
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    await history();
    await page.waitForFunction(() => document.querySelectorAll("#history-list li").length === 3);
    assert.equal(await page.locator("#history-list li").count(), 3);
    const entryResponse = await context.request.get(`${origin}/v1/jobs/${ids[0].id}`);
    const first = await entryResponse.json();
    assert.ok(new Date(first.expires_at).getTime() - Date.now() > 8.99 * 86400000);
    const link = page.locator(`a[href="/v1/jobs/${ids[0].id}/content"]`);
    const pending = page.waitForEvent("download");
    await link.click();
    const download = await pending;
    assert.equal(await download.failure(), null);
    const downloaded = path.join(artifacts, "history-download.webp");
    await download.saveAs(downloaded);
    const streams = JSON.parse(execFileSync(ffprobe, ["-v", "error", "-show_streams", "-of", "json", downloaded], { encoding: "utf8" })).streams;
    assert.equal(streams[0].codec_name, "webp");
    checks.push("Multiple real conversions and compression retained; history download verified with FFprobe");
    await page.locator("#retention-days").fill("2");
    await page.locator("#retention-save").click();
    await page.getByText("Saved. New results will be kept for 2 days.", { exact: true }).waitFor();
    assert.equal((await (await context.request.get(`${origin}/v1/jobs/${ids[0].id}`)).json()).expires_at, first.expires_at);
    await page.reload();
    await page.locator("#history-button").click();
    await page.waitForFunction(() => document.querySelector("#retention-days").value === "2");
    assert.equal(await page.locator("#history-list li").count(), 3);
    for (const scheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme: scheme });
      for (const width of [320, 375, 430, 768, 834, 1194, 1440, 1920]) {
        await page.setViewportSize({ width, height: 900 });
        await layout(page, `${scheme} ${width}`);
        if ([375, 834, 1440].includes(width)) await page.screenshot({ path: path.join(artifacts, `history-${scheme}-${width}.png`), fullPage: true });
      }
    }
    for (const device of ["iPhone 13", "iPad Pro 11"]) {
      const touchContext = await browser.newContext(devices[device]);
      const touch = await touchContext.newPage();
      await touch.goto(origin);
      await touch.locator("#history-button").tap();
      await touch.waitForFunction(() => document.querySelectorAll("#history-list li").length === 3);
      for (const orientation of ["portrait", "landscape"]) {
        if (orientation === "landscape") await touch.setViewportSize({ width: devices[device].viewport.height, height: devices[device].viewport.width });
        await layout(touch, `${device} ${orientation}`);
      }
      await touchContext.close();
    }
    checks.push("History layout, long filenames and44px controls:8 widths, light/dark, iPhone/iPad touch orientations");
    await context.setOffline(true);
    await page.waitForFunction(() => document.querySelector("#retention-save").disabled);
    assert.equal(await page.locator(".history-delete").first().isDisabled(), true);
    assert.equal(await page.locator(".history-download").first().getAttribute("aria-disabled"), "true");
    await context.setOffline(false);
    await page.waitForFunction(() => !document.querySelector("#history-refresh").disabled);
    const compressionRow = page.locator("#history-list li").filter({ has: page.locator(`a[href="/v1/compressions/${compressedId}/content"]`) });
    page.once("dialog", dialog => dialog.dismiss());
    await compressionRow.getByRole("button").click();
    assert.equal(await page.locator("#history-list li").count(), 3);
    page.once("dialog", dialog => dialog.accept());
    await compressionRow.getByRole("button").click();
    await page.waitForFunction(() => document.querySelectorAll("#history-list li").length === 2);
    assert.equal((await context.request.get(`${origin}/v1/compressions/${compressedId}`)).status(), 404);
    checks.push("Offline controls and reconnect; rejected confirmation preserves result; confirmed deletion removes it");
    const cachePaths = await page.evaluate(async () => { const result = []; for (const name of await caches.keys()) for (const request of await (await caches.open(name)).keys()) result.push(new URL(request.url).pathname); return result; });
    assert.equal(cachePaths.some(value => value.startsWith("/v1/")), false);
    assert.deepEqual(errors, []);
    const report = { passed: checks, browser: await browser.version(), artifacts, limitations: ["Physical Apple devices and production validation remain separate."] };
    await fs.writeFile(path.join(artifacts, "validation.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
  } catch (error) {
    console.error(await page.evaluate(() => ({ message: document.querySelector("#history-message")?.textContent, rows: document.querySelector("#history-list")?.innerText })));
    throw error;
  } finally {
    for (const job of ids) await context.request.delete(`${origin}/v1/${job.kind}/${job.id}`).catch(() => {});
    await context.request.put(`${origin}/v1/history/settings`, { data: { retention_days: previousRetention } }).catch(() => {});
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
