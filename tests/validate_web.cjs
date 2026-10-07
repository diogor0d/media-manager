/* Real-browser validation against an isolated local server. Requires Playwright and FFmpeg. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium, webkit, devices } = require("playwright");

const origin = process.env.MEDIA_MANAGER_TEST_URL || "http://127.0.0.1:8765";
assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(origin).hostname), "Use an isolated loopback test server, never production");
const tools = process.env.MEDIA_MANAGER_TEST_FFMPEG_DIR;
const ffmpeg = tools ? path.join(tools, "ffmpeg") : "ffmpeg";
const ffprobe = tools ? path.join(tools, "ffprobe") : "ffprobe";
const checks = [];
const sizes = [
  { name: "small-phone", width: 320, height: 568 },
  { name: "phone", width: 375, height: 812 },
  { name: "phone-modern", width: 390, height: 844 },
  { name: "phone-large", width: 430, height: 932 },
  { name: "ipad-small", width: 768, height: 1024 },
  { name: "ipad-portrait", width: 834, height: 1194 },
  { name: "ipad-landscape", width: 1194, height: 834 },
  { name: "laptop", width: 1366, height: 768 },
  { name: "desktop", width: 1440, height: 900 },
  { name: "desktop-wide", width: 1920, height: 1080 },
];

async function main() {
  const artifacts = process.env.MEDIA_MANAGER_TEST_ARTIFACT_DIR || await fs.mkdtemp(path.join(os.tmpdir(), "media-manager-browser-"));
  await fs.mkdir(artifacts, { recursive: true });
  const fixtures = await fs.mkdtemp(path.join(os.tmpdir(), "media-manager-fixtures-"));
  function generate(name, args) {
    const output = path.join(fixtures, name);
    execFileSync(ffmpeg, ["-hide_banner", "-loglevel", "error", ...args, "-y", output]);
    return output;
  }
  const image = generate("sample.png", ["-f", "lavfi", "-i", "testsrc2=size=960x640", "-frames:v", "1"]);
  const video = generate("sample.mp4", ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]);
  const audio = generate("sample.wav", ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "3"]);
  const invalid = path.join(fixtures, "invalid.png");
  await fs.writeFile(invalid, "This is not media.");
  const engine = process.env.MEDIA_MANAGER_TEST_BROWSER === "webkit" ? webkit : chromium;
  const browser = await engine.launch({ headless: true, executablePath: engine === chromium ? process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined : undefined });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "light", acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  const captureWarnings = [];
  const capturing = new Set();
  const remoteRequests = [];
  function observe(observedPage) {
    observedPage.on("pageerror", e => errors.push(e.message));
    observedPage.on("console", e => {
      if (e.type() !== "error" || e.text().includes("Failed to load resource")) return;
      // Playwright's WebKit screenshotter injects "body {}" to synchronize animations.
      // The app correctly rejects that inline style; normal interactions must remain clean.
      if (engine === webkit && capturing.has(observedPage) && e.text().startsWith("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline'")) captureWarnings.push(e.text());
      else errors.push(e.text());
    });
    observedPage.on("request", r => { if (!r.url().startsWith(origin) && !r.url().startsWith("blob:")) remoteRequests.push(r.url()); });
  }
  observe(page);
  const $ = id => page.locator(`#${id}`);
  async function noOverflow(label) {
    const size = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(size.scroll <= size.width + 1, `${label}: horizontal overflow ${JSON.stringify(size)}`);
    const shortTargets = await page.locator('button:visible, select:visible, .target-option label:visible, .segment-option label:visible').evaluateAll(elements => elements.filter(e => e.getBoundingClientRect().height < 43.5).map(e => ({ text: e.textContent, height: e.getBoundingClientRect().height })));
    assert.deepEqual(shortTargets, [], `${label}: touch target below 44px`);
  }
  async function screenshot(name, observedPage = page) {
    capturing.add(observedPage);
    try { await observedPage.screenshot({ path: path.join(artifacts, `${name}.png`), fullPage: true, caret: "initial" }); }
    finally { capturing.delete(observedPage); }
  }
  async function pick(file) {
    const chooser = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Choose a media file or drop one here" }).click();
    await (await chooser).setFiles(file);
    await $("workspace").waitFor({ state: "visible" });
  }
  async function discard() {
    await page.getByRole("button", { name: "Discard result" }).click();
    await $("drop-zone").waitFor({ state: "visible" });
  }
  async function ready() { await $("result-view").waitFor({ state: "visible", timeout: 60000 }); }
  async function download(name) {
    const pending = page.waitForEvent("download");
    await $("download-button").click();
    const d = await pending;
    assert.equal(await d.failure(), null);
    const output = path.join(artifacts, name);
    await d.saveAs(output);
    return JSON.parse(execFileSync(ffprobe, ["-v", "error", "-show_streams", "-of", "json", output], { encoding: "utf8" })).streams;
  }
  try {
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelector("#limit-copy").textContent.startsWith("Up to"));
    assert.equal(await $("drop-zone").getAttribute("aria-disabled"), "false");
    assert.equal(await $("connection-status").isVisible(), false);
    assert.equal(await page.locator("#format-guide button").count(), 9);
    await screenshot("desktop-light");
    for (const scheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme: scheme });
      for (const size of sizes) {
        await page.setViewportSize({ width: size.width, height: size.height });
        await noOverflow(`empty ${scheme} ${size.name}`);
        if (size.name.startsWith("ipad")) await screenshot(`empty-${size.name}-${scheme}`);
      }
      await page.setViewportSize({ width: 390, height: 844 });
      await screenshot(`mobile-${scheme}`);
    }
    checks.push("Empty layout: 10 phone/iPad/desktop sizes from 320 to 1920, light and dark; 44px touch targets");
    await page.emulateMedia({ colorScheme: "light" });
    await page.setViewportSize({ width: 1280, height: 900 });
    const chooser = page.waitForEvent("filechooser");
    await $("drop-zone").focus();
    const focus = await $("drop-zone").evaluate(e => ({ outline: getComputedStyle(e).outlineStyle, width: getComputedStyle(e).outlineWidth }));
    assert.equal(focus.outline, "solid");
    assert.equal(focus.width, "3px");
    await page.keyboard.press("Enter");
    await (await chooser).setFiles(image);
    await $("workspace").waitFor({ state: "visible" });
    await page.waitForFunction(() => document.activeElement.classList.contains("source-panel"));
    assert.equal(await page.getByRole("radio", { name: "webp WebP", exact: true }).isChecked(), true);
    await page.getByRole("radio", { name: "webp WebP", exact: true }).focus();
    await page.keyboard.press("ArrowLeft");
    assert.equal(await page.getByRole("radio", { name: "png PNG", exact: true }).isChecked(), true);
    await page.keyboard.press("ArrowRight");
    await $("quality-slider").focus();
    await page.keyboard.press("ArrowRight");
    assert.equal(await $("quality-value").innerText(), "51%");
    await $("resolution-select").selectOption("480p");
    await screenshot("image-workspace");
    for (const scheme of ["light", "dark"]) {
      await page.emulateMedia({ colorScheme: scheme });
      for (const size of sizes) {
        await page.setViewportSize({ width: size.width, height: size.height });
        await noOverflow(`workspace ${scheme} ${size.name}`);
        if (size.name.startsWith("ipad")) await screenshot(`workspace-${size.name}-${scheme}`);
      }
      await page.setViewportSize({ width: 390, height: 844 });
      await screenshot(`mobile-workspace-${scheme}`);
    }
    checks.push("Image workspace: 20 theme/device combinations; keyboard picker, radios, focus, slider and resolution");
    await page.emulateMedia({ colorScheme: "light" });
    await page.setViewportSize({ width: 1280, height: 900 });
    const accepted = page.waitForRequest(r => r.method() === "POST" && r.url().includes("/v1/uploads?"));
    await $("convert-button").click();
    const url = new URL((await accepted).url());
    assert.equal(url.searchParams.get("quality_percent"), "51");
    assert.equal(url.searchParams.get("resolution"), "480p");
    await ready();
    await page.reload();
    await ready();
    const outputImage = await download("converted.webp");
    assert.equal(outputImage[0].codec_name, "webp");
    assert.equal(outputImage[0].width, 854);
    assert.equal(outputImage[0].height, 569);
    await screenshot("image-result");
    await page.setViewportSize({ width: 320, height: 700 });
    await noOverflow("result 320");
    await screenshot("mobile-result");
    await discard();
    checks.push("Real PNG → WebP: 51% quality request, 480p resize, reload recovery, download and probe verification");
    await page.setViewportSize({ width: 390, height: 844 });
    await pick(video);
    await page.locator("#preview video").evaluate(async element => { element.muted = true; await element.play(); });
    await page.waitForFunction(() => document.querySelector("#preview video").currentTime > 0.1);
    await page.locator("#preview video").evaluate(element => element.pause());
    assert.equal(await $("file-dimensions").innerText(), "1280 × 720");
    for (const size of sizes) {
      await page.setViewportSize({ width: size.width, height: size.height });
      await noOverflow(`six video outputs ${size.name}`);
      if (size.name.startsWith("ipad")) await screenshot(`video-${size.name}`);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("radio", { name: "webm WebM (VP9)", exact: true }).check();
    await page.getByRole("radio", { name: "Remove audio", exact: true }).check();
    await $("resolution-select").selectOption("480p");
    await screenshot("video-workspace");
    await $("convert-button").click();
    await $("progress-view").waitFor({ state: "visible" });
    await screenshot("mobile-progress");
    await noOverflow("progress mobile");
    await ready();
    const outputVideo = await download("converted.webm");
    assert.equal(outputVideo.some(s => s.codec_type === "audio"), false);
    assert.equal(outputVideo[0].codec_name, "vp9");
    assert.ok(outputVideo[0].width <= 854);
    await discard();
    checks.push("Real MP4 → WebM: mobile preview, progress, resize, audio removal, download and probe verification");
    await pick(audio);
    await page.locator("#preview audio").evaluate(async element => { element.muted = true; await element.play(); });
    await page.waitForFunction(() => document.querySelector("#preview audio").currentTime > 0.1);
    await page.locator("#preview audio").evaluate(element => element.pause());
    assert.equal(await $("resolution-field").isVisible(), false);
    assert.equal(await $("audio-field").isVisible(), false);
    await screenshot("audio-workspace");
    await $("convert-button").click();
    await ready();
    const outputAudio = await download("converted.mp3");
    assert.equal(outputAudio[0].codec_name, "mp3");
    await discard();
    checks.push("Local video and audio playback; real WAV → MP3 conversion, download and probe verification");
    await pick(video);
    await page.getByRole("radio", { name: "webm WebM (VP9)", exact: true }).check();
    await $("convert-button").click();
    await $("progress-view").waitFor({ state: "visible" });
    await $("cancel-button").click();
    await $("drop-zone").waitFor({ state: "visible" });
    await pick(invalid);
    await $("convert-button").click();
    await $("error-view").waitFor({ state: "visible", timeout: 15000 });
    await noOverflow("error mobile");
    await screenshot("mobile-error");
    assert.ok((await $("error-detail").innerText()).length > 0);
    await $("try-again-button").click();
    await $("drop-zone").waitFor({ state: "visible" });
    checks.push("Cancellation and invalid-media error with recovery to file selection");
    const transfer = await page.evaluateHandle(bytes => {
      const data = new DataTransfer();
      data.items.add(new File([new Uint8Array(bytes)], "dropped.png", { type: "image/png" }));
      return data;
    }, Array.from(await fs.readFile(image)));
    await $("drop-zone").dispatchEvent("dragenter", { dataTransfer: transfer });
    assert.ok((await $("drop-zone").getAttribute("class")).includes("is-dragging"));
    await $("drop-zone").dispatchEvent("drop", { dataTransfer: transfer });
    await $("workspace").waitFor({ state: "visible" });
    assert.equal(await $("file-name").innerText(), "dropped.png");
    const replaceChooser = page.waitForEvent("filechooser");
    await $("replace-file").click();
    await (await replaceChooser).setFiles(audio);
    await page.waitForFunction(() => document.querySelector("#file-name").textContent === "sample.wav");
    assert.equal(await $("resolution-field").isVisible(), false);
    await page.reload();
    await $("drop-zone").waitFor({ state: "visible" });
    checks.push("Drag-and-drop highlight and file selection; Replace switches media type and relevant controls");
    const quickChooser = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Convert to JPEG", exact: true }).click();
    await (await quickChooser).setFiles(image);
    await $("workspace").waitFor({ state: "visible" });
    assert.equal(await page.getByRole("radio", { name: "jpg JPEG", exact: true }).isChecked(), true);
    assert.equal(await $("format-guide").isVisible(), false);
    await page.reload();
    await $("drop-zone").waitFor({ state: "visible" });
    const wrongChooser = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Convert to JPEG", exact: true }).click();
    await (await wrongChooser).setFiles(audio);
    await $("error-view").waitFor({ state: "visible" });
    assert.equal(await $("error-title").innerText(), "Choose a compatible source.");
    await $("try-again-button").click();
    await $("drop-zone").waitFor({ state: "visible" });
    checks.push("Format-first selection preselects JPEG; incompatible source rejected; format guide only in empty state");
    await page.evaluate(() => navigator.serviceWorker.ready);
    const cached = await page.evaluate(async () => {
      const names = await caches.keys();
      const paths = [];
      for (const name of names) for (const request of await (await caches.open(name)).keys()) paths.push(new URL(request.url).pathname + new URL(request.url).search);
      return { names, paths };
    });
    assert.ok(cached.names.includes("media-manager-shell-history1"));
    assert.ok(cached.paths.includes("/assets/app.css?v=history1"));
    assert.equal(cached.paths.some(p => p.startsWith("/v1/")), false);
    await page.reload();
    await $("drop-zone").waitFor({ state: "visible" });
    await context.setOffline(true);
    await page.waitForFunction(() => document.querySelector("#drop-zone").getAttribute("aria-disabled") === "true");
    assert.equal(await $("file-input").isDisabled(), true);
    if (engine === chromium) {
      await page.reload();
      await $("error-view").waitFor({ state: "visible" });
      assert.match(await $("error-title").innerText(), /offline|unavailable/);
    }
    await context.setOffline(false);
    if (engine === chromium) await $("try-again-button").click();
    await $("drop-zone").waitFor({ state: "visible" });
    await page.waitForFunction(() => document.querySelector("#drop-zone").getAttribute("aria-disabled") === "false");
    checks.push(`PWA cache versions aligned, no API/media cached; offline selection disabled and reconnect recovery${engine === chromium ? "; offline shell reload" : ""}`);
    await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "dark", contrast: "more" });
    assert.equal(await page.locator(".media-tile").first().evaluate(e => getComputedStyle(e).transitionDuration), "0s");
    await screenshot("dark-high-contrast");
    checks.push("Reduced motion, increased contrast, no JavaScript errors and no external asset requests");
    for (const deviceName of ["iPhone 13", "iPad Pro 11"]) {
      const deviceContext = await browser.newContext({ ...devices[deviceName], colorScheme: "light" });
      const touchPage = await deviceContext.newPage();
      observe(touchPage);
      try {
        await touchPage.goto(origin);
        await touchPage.waitForFunction(() => document.querySelectorAll("#format-guide button").length === 9);
        await screenshot(`${deviceName.replaceAll(" ", "-")}-empty`, touchPage);
        const chooser = touchPage.waitForEvent("filechooser");
        await touchPage.getByRole("button", { name: "Choose a media file or drop one here" }).tap();
        await (await chooser).setFiles({ name: "a-very-long-source-filename-".repeat(7) + ".png", mimeType: "image/png", buffer: await fs.readFile(image) });
        await touchPage.locator("#workspace").waitFor({ state: "visible" });
        await touchPage.locator('label[for="target-image-png"]').tap();
        assert.equal(await touchPage.getByRole("radio", { name: "png PNG", exact: true }).isChecked(), true);
        for (const mode of ["portrait", "landscape"]) {
          if (mode === "landscape") await touchPage.setViewportSize({ width: devices[deviceName].viewport.height, height: devices[deviceName].viewport.width });
          const overflow = await touchPage.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
          assert.equal(overflow, false, `${deviceName} ${mode}: long filename overflow`);
          await touchPage.locator("#convert-button").scrollIntoViewIfNeeded();
          const clear = await touchPage.locator("#convert-button").evaluate(e => { const r = e.getBoundingClientRect(); return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)); });
          assert.equal(clear, true, `${deviceName} ${mode}: action is obscured`);
          await screenshot(`${deviceName.replaceAll(" ", "-")}-${mode}`, touchPage);
        }
      } finally { await deviceContext.close(); }
    }
    checks.push("iPhone and iPad touch profiles: picker and format taps, long filenames, portrait/landscape overflow and unobscured action");
    assert.deepEqual(errors, []);
    assert.deepEqual(remoteRequests, []);
    const report = { passed: checks, artifacts, browser: await browser.version(), failures: [], captureWarnings: captureWarnings.length ? { count: captureWarnings.length, detail: "Playwright WebKit screenshot helper's inline style was rejected by the strict CSP. No application errors occurred outside screenshot capture." } : null, limitations: engine === webkit ? ["Offline shell reload: Playwright WebKit raised an internal navigation error; verified in Chromium only.", "Physical Apple devices, Home Screen installation and native share destinations require on-device validation."] : ["Physical Apple devices, Home Screen installation and native share destinations require on-device validation."] };
    await fs.writeFile(path.join(artifacts, "validation.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
