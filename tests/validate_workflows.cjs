"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const read = (name) => fs.readFileSync(path.join(root, name), "utf8");
const html = read("src/media_manager/web/index.html");
const app = read("src/media_manager/web/app.js");
const worker = read("src/media_manager/web/sw.js");
const shortcuts = read("shortcuts/job.cherri");
const convert = read("shortcuts/convert.cherri");
const compress = read("shortcuts/compress.cherri");
const spec = read("shortcuts/spec.md");

assert.match(html, /app\.css\?v=workflows3/);
assert.match(html, /app\.js\?v=workflows3/);
assert.match(worker, /media-manager-shell-workflows3/);
assert.match(worker, /app\.css\?v=workflows3/);
assert.match(worker, /app\.js\?v=workflows3/);
assert.match(worker, /url\.pathname\.startsWith\("\/v1\/"\)/);

for (const mode of ["download", "convert", "compress"]) {
  assert.match(html, new RegExp(`data-mode="${mode}"`));
}
for (const id of ["download-form", "media-url", "download-type", "download-quality", "download-format", "download-codec", "download-auto-process", "workflow-select", "compression-target-bytes", "compression-resolution", "compression-audio", "compression-quality", "estimate-review", "refine-estimate", "start-job-button"]) {
  assert.match(html, new RegExp(`id="${id}"`), `missing workflow control ${id}`);
}
for (const value of ["convert", "compress", "convert_compress"]) assert.match(html, new RegExp(`value="${value}"`));

for (const contract of [
  'fetch("/v1/downloads"',
  '/v1/uploads/${encodeURIComponent(jobId)}/prepare',
  "auto_start: false",
  "target_bytes:",
  "validateStatusUrl",
  '"queued", "downloading", "processing"',
]) assert.ok(app.includes(contract), `client is missing contract: ${contract}`);
assert.match(app, /jobApiUrl\(id\)\}\/estimate/);
assert.match(app, /jobApiUrl\(state\.jobId\)\}\/start/);
assert.match(app, /size_min_bytes/);
assert.match(app, /size_max_bytes/);
assert.match(app, /eta_min_seconds/);
assert.match(app, /eta_max_seconds/);
assert.match(app, /entry\.kind === "download" \? "downloads"/);

assert.match(convert, /prepare_only=true/);
assert.match(compress, /prepare_only=true/);
assert.match(shortcuts, /"awaiting_review"/);
assert.match(shortcuts, /"Refine estimate"/);
assert.match(shortcuts, /"Refined estimate:/);
assert.match(shortcuts, /"queued"/);
assert.equal((shortcuts.match(/"WFHTTPMethod": "DELETE"/g) || []).length, 2,
  "Shortcut deletion should exist only on explicit Discard choices");
assert.match(shortcuts, /if @decision == "discard"[\s\S]*?WFHTTPMethod": "DELETE"/);
assert.match(shortcuts, /if @keep == false[\s\S]*?WFHTTPMethod": "DELETE"/);
assert.match(spec, /Keeping and\s+sharing a result does not send DELETE/);

process.stdout.write("Workflow assets, API request paths, review gates, and explicit Shortcut cleanup contracts are consistent.\n");
