"use strict";

const $ = (selector) => document.querySelector(selector);
const state = { capabilities: null, file: null, mediaClass: null, objectUrl: null, jobId: null, upload: null, resumeAction: null, xhr: null, pollTimer: null, expiryTimer: null, cancelled: false, reloadOnError: false };

const extensionGroups = {
  image: new Set(["jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff"]),
  animation: new Set(["gif"]),
  video: new Set(["mp4", "mov", "m4v", "webm", "mkv", "avi", "mpeg", "mpg", "ts", "m2ts", "flv", "wmv", "asf", "3gp", "3g2"]),
  audio: new Set(["mp3", "m4a", "aac", "wav", "aiff", "aif", "flac", "ogg", "opus"]),
};

const preferredTargets = { image: "image-webp", animation: "video-mp4", video: "video-mp4", audio: "audio-mp3" };
const views = ["#drop-zone", "#workspace", "#progress-view", "#result-view", "#error-view"];

document.addEventListener("DOMContentLoaded", init);

async function init() {
  bindEvents();
  registerPwa();
  showInstallHint();
  updateConnectionStatus();
  try {
    const response = await fetch("/v1/capabilities", { headers: { Accept: "application/json" }, cache: "no-store" });
    state.capabilities = await apiJson(response);
    updateConnectionStatus();
    $("#limit-copy").textContent = `Up to ${formatBytes(state.capabilities.max_upload_bytes)} · results expire after ${formatDuration(state.capabilities.result_ttl_seconds)}`;
    renderQualityOptions();
    announce("Converter ready");
  } catch (error) {
    if (error.reauthenticate) showSessionExpired();
    else showError(navigator.onLine ? "The conversion bench is unavailable." : "You’re offline.", navigator.onLine ? error.message : "Reconnect to choose and convert a file.", "Try again");
  }
}

function bindEvents() {
  const drop = $("#drop-zone");
  const input = $("#file-input");
  drop.addEventListener("click", () => converterAvailable() && input.click());
  drop.addEventListener("keydown", (event) => { if (converterAvailable() && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); input.click(); } });
  input.addEventListener("change", () => input.files[0] && selectFile(input.files[0]));
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); if (converterAvailable()) drop.classList.add("is-dragging"); }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove("is-dragging"); }));
  drop.addEventListener("drop", (event) => converterAvailable() && event.dataTransfer.files[0] && selectFile(event.dataTransfer.files[0]));
  $("#replace-file").addEventListener("click", () => converterAvailable() && input.click());
  $("#conversion-form").addEventListener("submit", startConversion);
  $("#target-options").addEventListener("change", updateTargetOptions);
  $("#cancel-button").addEventListener("click", cancelJob);
  $("#discard-button").addEventListener("click", reset);
  $("#download-button").addEventListener("click", (event) => { if (!navigator.onLine) { event.preventDefault(); announce("Reconnect before downloading the result."); } });
  $("#try-again-button").addEventListener("click", () => state.capabilities && !state.reloadOnError ? reset() : window.location.reload());
  window.addEventListener("online", resumeAfterReconnect);
  window.addEventListener("offline", updateConnectionStatus);
}

function registerPwa() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}

function showInstallHint() {
  const isIos = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  $("#install-hint").hidden = !isIos || standalone;
}

function updateConnectionStatus() {
  const status = $("#connection-status");
  status.classList.toggle("is-offline", !navigator.onLine);
  status.lastChild.textContent = navigator.onLine ? "Private conversion bench" : "Offline · conversion paused";
  const available = converterAvailable();
  const drop = $("#drop-zone");
  drop.classList.toggle("is-disabled", !available);
  drop.setAttribute("aria-disabled", String(!available));
  $("#file-input").disabled = !available;
  $("#replace-file").disabled = !navigator.onLine;
  $("#convert-button").disabled = !navigator.onLine;
  const download = $("#download-button");
  download.classList.toggle("is-disabled", !navigator.onLine);
  download.setAttribute("aria-disabled", String(!navigator.onLine));
  download.tabIndex = navigator.onLine ? 0 : -1;
  announce(navigator.onLine ? "Connection restored." : "Offline. Conversion is paused.");
}

function converterAvailable() { return navigator.onLine && Boolean(state.capabilities); }

function resumeAfterReconnect() {
  updateConnectionStatus();
  if (!state.capabilities) return window.location.reload();
  const action = state.resumeAction;
  state.resumeAction = null;
  if (action === "upload") continueUpload();
  if (action === "poll") pollJob();
}

async function selectFile(file) {
  if (!navigator.onLine) return showError("You’re offline.", "Reconnect to choose and convert a file.", "Try again");
  if (!state.capabilities) return showError("The server is not ready.", "Reload the page and try again.");
  if (file.size === 0) return showError("This file is empty.", "Choose a media file that contains data.");
  if (file.size > state.capabilities.max_upload_bytes) return showError("This file is too large.", `The server accepts files up to ${formatBytes(state.capabilities.max_upload_bytes)}.`);

  const mediaClass = detectMediaClass(file);
  if (!mediaClass) return showError("This format is not advertised.", "Choose a common image, animation, video, or audio file.");

  clearTimers();
  state.cancelled = false;
  revokePreview();
  state.file = file;
  state.mediaClass = mediaClass;
  state.objectUrl = URL.createObjectURL(file);
  $("#file-name").textContent = file.name || "Unnamed media";
  $("#file-type").textContent = classLabel(mediaClass);
  $("#file-size").textContent = formatBytes(file.size);
  $("#dimensions-fact").hidden = true;
  $("#duration-fact").hidden = true;
  renderPreview(file, mediaClass);
  renderTargets(mediaClass);
  showView("#workspace");
  announce(`${classLabel(mediaClass)} detected. Choose an output format.`);
}

function detectMediaClass(file) {
  const extension = getExtension(file.name);
  if (extension === "gif" || file.type === "image/gif") return "animation";
  for (const [mediaClass, extensions] of Object.entries(extensionGroups)) if (extensions.has(extension)) return mediaClass;
  if (file.type.startsWith("image/")) return "image";
  if (file.type.startsWith("video/")) return "video";
  if (file.type.startsWith("audio/")) return "audio";
  return null;
}

function renderPreview(file, mediaClass) {
  const preview = $("#preview");
  preview.replaceChildren();
  if (mediaClass === "image" || mediaClass === "animation") {
    const image = document.createElement("img");
    image.alt = "Local preview of the selected file";
    image.src = state.objectUrl;
    image.addEventListener("load", () => setDimensions(image.naturalWidth, image.naturalHeight));
    preview.append(image);
  } else if (mediaClass === "video") {
    const video = document.createElement("video");
    video.controls = true; video.preload = "metadata"; video.src = state.objectUrl;
    video.addEventListener("loadedmetadata", () => { setDimensions(video.videoWidth, video.videoHeight); setDuration(video.duration); });
    preview.append(video);
  } else {
    const audio = document.createElement("audio");
    audio.controls = true; audio.preload = "metadata"; audio.src = state.objectUrl;
    audio.addEventListener("loadedmetadata", () => setDuration(audio.duration));
    const placeholder = document.createElement("div");
    placeholder.className = "preview-placeholder"; placeholder.textContent = getExtension(file.name) || "audio";
    preview.append(placeholder, audio);
  }
}

function renderTargets(mediaClass) {
  const targets = state.capabilities.targets.filter((target) => target.accepts.includes(mediaClass));
  const container = $("#target-options");
  container.replaceChildren();
  targets.forEach((target, index) => {
    const wrapper = document.createElement("div"); wrapper.className = "target-option";
    const input = document.createElement("input"); input.type = "radio"; input.name = "target"; input.id = `target-${target.value}`; input.value = target.value;
    input.checked = target.value === preferredTargets[mediaClass] || (index === 0 && !targets.some((item) => item.value === preferredTargets[mediaClass]));
    const label = document.createElement("label"); label.htmlFor = input.id;
    const strong = document.createElement("strong"); strong.textContent = target.extension;
    const small = document.createElement("small"); small.textContent = target.label;
    label.append(strong, small); wrapper.append(input, label); container.append(wrapper);
  });
  $("#target-count").textContent = `${targets.length} available`;
  updateTargetOptions();
}

function renderQualityOptions() {
  const container = $("#quality-options");
  state.capabilities.qualities.forEach((quality) => {
    const wrapper = document.createElement("div"); wrapper.className = "segment-option";
    const input = document.createElement("input"); input.type = "radio"; input.name = "quality"; input.id = `quality-${quality.value}`; input.value = quality.value; input.checked = quality.value === "balanced";
    const label = document.createElement("label"); label.htmlFor = input.id; label.textContent = quality.label;
    wrapper.append(input, label); container.append(wrapper);
  });
}

function updateTargetOptions() {
  const selected = document.querySelector('input[name="target"]:checked');
  if (!selected) return;
  const target = state.capabilities.targets.find((item) => item.value === selected.value);
  const resolution = $("#resolution-select");
  resolution.replaceChildren();
  target.allowed_resolutions.forEach((value) => {
    const definition = state.capabilities.resolutions.find((item) => item.value === value);
    const option = document.createElement("option"); option.value = value; option.textContent = definition.label; resolution.append(option);
  });
  $("#resolution-field").hidden = target.allowed_resolutions.length === 1;
  const audio = $("#audio-options"); audio.replaceChildren();
  target.allowed_audio_modes.forEach((value) => {
    const definition = state.capabilities.audio_modes.find((item) => item.value === value);
    const wrapper = document.createElement("div"); wrapper.className = "segment-option";
    const input = document.createElement("input"); input.type = "radio"; input.name = "audio"; input.id = `audio-${value}`; input.value = value; input.checked = value === "keep";
    const label = document.createElement("label"); label.htmlFor = input.id; label.textContent = definition.label;
    wrapper.append(input, label); audio.append(wrapper);
  });
  $("#audio-field").hidden = target.allowed_audio_modes.length === 1;
  $("#convert-extension").textContent = `.${target.extension}`;
}

async function startConversion(event) {
  event.preventDefault();
  if (!navigator.onLine) return showError("You’re offline.", "Reconnect before starting the upload.", "Try again");
  const targetValue = document.querySelector('input[name="target"]:checked').value;
  const target = state.capabilities.targets.find((item) => item.value === targetValue);
  const quality = document.querySelector('input[name="quality"]:checked').value;
  const resolution = $("#resolution-select").value || "source";
  const audio = document.querySelector('input[name="audio"]:checked')?.value || "keep";
  const query = new URLSearchParams({ target: targetValue, quality, resolution, audio });
  state.cancelled = false;
  showView("#progress-view");
  $("#progress-view").classList.remove("is-processing");
  setProgress("UPLOADING", "Sending secure chunks…", `0 of ${formatBytes(state.file.size)}`);
  try {
    const createdResponse = await fetch(`/v1/uploads?${query}`, {
      method: "POST",
      headers: { Accept: "application/json", "Upload-Length": String(state.file.size) },
    });
    let upload = await apiJson(createdResponse);
    state.jobId = upload.id;
    if (state.cancelled) {
      await fetch(`/v1/jobs/${encodeURIComponent(state.jobId)}`, { method: "DELETE" });
      return;
    }

    state.upload = upload;
    continueUpload(target);
  } catch (error) {
    handleUploadError(error);
  }
}

async function continueUpload(target = targetFor(document.querySelector('input[name="target"]:checked').value)) {
  try {
    while (state.upload.offset < state.upload.length) {
      if (state.cancelled) return;
      state.upload = await uploadNextChunk(state.upload);
    }
    if (state.cancelled) return;

    const completedResponse = await fetch(`/v1/uploads/${encodeURIComponent(state.jobId)}/complete`, {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    const job = await apiJson(completedResponse);
    state.upload = null;
    $("#upload-bar").style.width = "100%";
    $("#progress-view").classList.add("is-processing");
    setProgress("PROCESSING", `Making .${target.extension}…`, "The server is inspecting and converting your file.");
    state.jobId = job.id;
    pollJob();
  } catch (error) {
    handleUploadError(error);
  }
}

function handleUploadError(error) {
  if (state.cancelled) return;
  if (error.reauthenticate) showSessionExpired();
  else if (error.status) showApiFailure(error.body, error.status);
  else if (!navigator.onLine && state.upload) {
    state.resumeAction = "upload";
    setProgress("PAUSED", "Upload paused offline", "Reconnecting will resume from the last accepted chunk.");
    announce("Upload paused offline.");
  } else showError("The connection or session was lost.", "Reload to reconnect or sign in again.", "Reload", true);
}

async function uploadNextChunk(upload) {
  if (state.cancelled) throw new DOMException("Upload cancelled", "AbortError");
  const end = Math.min(upload.offset + upload.chunk_size, upload.length);
  const chunk = state.file.slice(upload.offset, end);
  try {
    return await sendChunk(upload, chunk);
  } catch (error) {
    if (state.cancelled || error.name === "AbortError") throw error;
    const status = await fetch(`/v1/uploads/${encodeURIComponent(upload.id)}`, {
      headers: { Accept: "application/json" }, cache: "no-store",
    });
    const recovered = await apiJson(status);
    if (recovered.offset > upload.offset) return recovered;
    return sendChunk(recovered, state.file.slice(recovered.offset, Math.min(recovered.offset + recovered.chunk_size, recovered.length)));
  }
}

function sendChunk(upload, chunk) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    state.xhr = xhr;
    xhr.open("PATCH", `/v1/uploads/${encodeURIComponent(upload.id)}`);
    xhr.responseType = "json";
    xhr.setRequestHeader("Content-Type", "application/offset+octet-stream");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Upload-Offset", String(upload.offset));
    xhr.upload.addEventListener("progress", (progress) => {
      if (!progress.lengthComputable) return;
      const loaded = upload.offset + progress.loaded;
      const percent = Math.round((loaded / upload.length) * 100);
      $("#upload-bar").style.width = `${percent}%`;
      $("#upload-meter").setAttribute("aria-valuenow", String(percent));
      $("#progress-detail").textContent = `${formatBytes(loaded)} of ${formatBytes(upload.length)}`;
    });
    xhr.addEventListener("load", () => {
      state.xhr = null;
      const contentType = xhr.getResponseHeader("Content-Type") || "";
      const sameOrigin = !xhr.responseURL || new URL(xhr.responseURL).origin === window.location.origin;
      if (!sameOrigin) reject(requestError(null, 401, true));
      else if (!contentType.includes("application/json")) reject(requestError(null, xhr.status, xhr.status === 401 || xhr.status === 403));
      else if (xhr.status === 200) resolve(xhr.response);
      else reject(requestError(xhr.response, xhr.status, xhr.status === 401 || xhr.status === 403));
    });
    xhr.addEventListener("error", () => { state.xhr = null; reject(new Error("The network connection was interrupted.")); });
    xhr.addEventListener("abort", () => { state.xhr = null; reject(new DOMException("Upload cancelled", "AbortError")); });
    xhr.send(chunk);
  });
}

async function pollJob() {
  try {
    const response = await fetch(`/v1/jobs/${encodeURIComponent(state.jobId)}`, { headers: { Accept: "application/json" }, cache: "no-store" });
    const job = await apiJson(response);
    if (job.state === "ready") return showResult(job);
    if (job.state === "failed") return showError("This conversion stopped.", job.error?.message || "The media could not be converted.");
    const label = job.state === "queued" ? "Waiting for the converter…" : `Making .${targetFor(job.target).extension}…`;
    $("#progress-title").textContent = label;
    state.pollTimer = window.setTimeout(pollJob, document.hidden ? 1800 : 700);
  } catch (error) {
    if (error.reauthenticate) showSessionExpired();
    else if (!navigator.onLine) {
      state.resumeAction = "poll";
      setProgress("PAUSED", "Status check paused offline", "Reconnecting will resume this conversion automatically.");
      announce("Status checks paused offline.");
    }
    else if (!error.status) showError("The connection or session was lost.", "Reload to reconnect or sign in again.", "Reload", true);
    else showError("The job status is unavailable.", error.message);
  }
}

function showResult(job) {
  clearTimers();
  const input = job.input; const output = job.output; const target = targetFor(job.target);
  $("#result-input-size").textContent = formatBytes(input.bytes);
  $("#result-output-size").textContent = formatBytes(output.bytes);
  $("#result-input-meta").textContent = mediaMeta(input);
  $("#result-output-meta").textContent = mediaMeta(output);
  const difference = input.bytes - output.bytes;
  $("#size-difference").textContent = difference >= 0 ? `${formatBytes(difference)} smaller than the source.` : `${formatBytes(Math.abs(difference))} larger than the source.`;
  $("#download-button").href = `/v1/jobs/${encodeURIComponent(job.id)}/content`;
  $("#download-button").setAttribute("download", output.filename);
  $("#download-extension").textContent = `.${target.extension}`;
  showView("#result-view");
  updateExpiry(job.expires_at);
  announce("Conversion ready. Review the result size before downloading.");
}

async function cancelJob() {
  state.cancelled = true;
  const jobId = state.jobId;
  if (state.xhr) state.xhr.abort();
  if (jobId) { try { await fetch(`/v1/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }); } catch (_) { /* Expiry also removes the job. */ } }
  reset(false);
}

async function reset(deleteJob = true) {
  const oldJobId = state.jobId;
  clearTimers();
  state.jobId = null; state.file = null; state.mediaClass = null; state.upload = null; state.resumeAction = null; state.cancelled = true; state.reloadOnError = false;
  revokePreview();
  $("#file-input").value = "";
  showView("#drop-zone");
  if (deleteJob && oldJobId) { try { await fetch(`/v1/jobs/${encodeURIComponent(oldJobId)}`, { method: "DELETE" }); } catch (_) { /* Best-effort cleanup. */ } }
  announce("Ready for another file.");
}

function showApiFailure(body, status) {
  if (status === 401 || status === 403) return showSessionExpired();
  const message = body?.error?.message || "The server rejected this conversion.";
  showError(status === 429 ? "The converter is busy." : "The upload was not accepted.", message);
}

function showSessionExpired() {
  state.capabilities = null;
  state.resumeAction = null;
  updateConnectionStatus();
  showError("Your session has expired.", "Sign in again to continue. Your selected file stays on this device.", "Sign in again", true);
}

function showError(title, detail, action = "Choose another file", reload = false) {
  clearTimers();
  state.reloadOnError = reload;
  $("#error-title").textContent = title;
  $("#error-detail").textContent = detail || "Try another file or output format.";
  $("#try-again-button").textContent = action;
  showView("#error-view");
  announce(`${title} ${detail || ""}`);
}

function showView(selector) { views.forEach((view) => { $(view).hidden = view !== selector; }); }
function setProgress(kicker, title, detail) { $("#progress-kicker").textContent = kicker; $("#progress-title").textContent = title; $("#progress-detail").textContent = detail; }
function setDimensions(width, height) { if (!width || !height) return; $("#file-dimensions").textContent = `${width} × ${height}`; $("#dimensions-fact").hidden = false; }
function setDuration(seconds) { if (!Number.isFinite(seconds)) return; $("#file-duration").textContent = formatClock(seconds); $("#duration-fact").hidden = false; }
function targetFor(value) { return state.capabilities.targets.find((target) => target.value === value); }
function getExtension(name) { const match = String(name).toLowerCase().match(/\.([a-z0-9]+)$/); return match ? match[1] : ""; }
function classLabel(value) { return ({ image: "Still image", animation: "Animation", video: "Video", audio: "Audio" })[value]; }
function formatBytes(bytes) { if (bytes < 1024) return `${bytes} B`; const units = ["KB", "MB", "GB"]; let value = bytes; let unit = "B"; for (const next of units) { value /= 1024; unit = next; if (value < 1024) break; } return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${unit}`; }
function formatClock(seconds) { const rounded = Math.round(seconds); const minutes = Math.floor(rounded / 60); return `${minutes}:${String(rounded % 60).padStart(2, "0")}`; }
function formatDuration(seconds) { return seconds % 60 === 0 ? `${seconds / 60} min` : `${seconds} sec`; }
function mediaMeta(media) { const pieces = []; if (media.width && media.height) pieces.push(`${media.width} × ${media.height}`); if (media.duration_ms != null) pieces.push(formatClock(media.duration_ms / 1000)); return pieces.join(" · ") || "Exact server result"; }
function announce(message) { $("#live-status").textContent = message; }
function revokePreview() { if (state.objectUrl) URL.revokeObjectURL(state.objectUrl); state.objectUrl = null; $("#preview")?.replaceChildren(); }
function clearTimers() { window.clearTimeout(state.pollTimer); window.clearInterval(state.expiryTimer); state.pollTimer = null; state.expiryTimer = null; }
async function apiJson(response) {
  const contentType = response.headers.get("Content-Type") || "";
  const sameOrigin = !response.url || new URL(response.url).origin === window.location.origin;
  if (response.redirected || !sameOrigin) throw requestError(null, 401, true);
  if (!contentType.includes("application/json")) throw requestError(null, response.status, response.status === 401 || response.status === 403);
  const body = await response.json();
  if (!response.ok) throw requestError(body, response.status, response.status === 401 || response.status === 403);
  return body;
}
function requestError(body, status, reauthenticate = false) { const error = new Error(body?.error?.message || (reauthenticate ? "Your session needs to be refreshed." : `Server request failed (${status}).`)); error.status = status; error.body = body; error.reauthenticate = reauthenticate; return error; }

function updateExpiry(value) {
  const expires = new Date(value).getTime();
  const tick = () => {
    const remaining = Math.max(0, Math.ceil((expires - Date.now()) / 1000));
    $("#expiry-copy").textContent = remaining ? `Disposable result · removed in ${formatClock(remaining)}` : "This result has expired and was removed.";
    if (!remaining) { clearTimers(); $("#download-button").removeAttribute("href"); }
  };
  tick(); state.expiryTimer = window.setInterval(tick, 1000);
}
