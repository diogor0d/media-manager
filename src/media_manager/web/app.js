"use strict";

const $ = (selector) => document.querySelector(selector);
const state = { capabilities: null, file: null, mediaClass: null, objectUrl: null, jobId: null, xhr: null, pollTimer: null, expiryTimer: null, cancelled: false };

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
  try {
    const response = await fetch("/v1/capabilities", { headers: { Accept: "application/json" } });
    if (!response.ok) throw await apiError(response);
    state.capabilities = await response.json();
    $("#limit-copy").textContent = `Up to ${formatBytes(state.capabilities.max_upload_bytes)} · results expire after ${formatDuration(state.capabilities.result_ttl_seconds)}`;
    renderQualityOptions();
    announce("Converter ready");
  } catch (error) {
    showError("The conversion bench is unavailable.", error.message);
  }
}

function bindEvents() {
  const drop = $("#drop-zone");
  const input = $("#file-input");
  drop.addEventListener("click", () => input.click());
  drop.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); input.click(); } });
  input.addEventListener("change", () => input.files[0] && selectFile(input.files[0]));
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add("is-dragging"); }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove("is-dragging"); }));
  drop.addEventListener("drop", (event) => event.dataTransfer.files[0] && selectFile(event.dataTransfer.files[0]));
  $("#replace-file").addEventListener("click", () => input.click());
  $("#conversion-form").addEventListener("submit", startConversion);
  $("#target-options").addEventListener("change", updateTargetOptions);
  $("#cancel-button").addEventListener("click", cancelJob);
  $("#discard-button").addEventListener("click", reset);
  $("#try-again-button").addEventListener("click", reset);
}

async function selectFile(file) {
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
    if (!createdResponse.ok) return showApiFailure(await responseBody(createdResponse), createdResponse.status);
    let upload = await createdResponse.json();
    state.jobId = upload.id;
    if (state.cancelled) {
      await fetch(`/v1/jobs/${encodeURIComponent(state.jobId)}`, { method: "DELETE" });
      return;
    }

    while (upload.offset < upload.length) {
      if (state.cancelled) return;
      upload = await uploadNextChunk(upload);
    }
    if (state.cancelled) return;

    const completedResponse = await fetch(`/v1/uploads/${encodeURIComponent(state.jobId)}/complete`, {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    if (!completedResponse.ok) return showApiFailure(await responseBody(completedResponse), completedResponse.status);
    const job = await completedResponse.json();
    $("#upload-bar").style.width = "100%";
    $("#progress-view").classList.add("is-processing");
    setProgress("PROCESSING", `Making .${target.extension}…`, "The server is inspecting and converting your file.");
    state.jobId = job.id;
    pollJob();
  } catch (error) {
    if (state.cancelled) return;
    if (error.status) showApiFailure(error.body, error.status);
    else showError("The upload was interrupted.", error.message || "Check the connection and try again.");
  }
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
    if (!status.ok) throw requestError(await responseBody(status), status.status);
    const recovered = await status.json();
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
      if (xhr.status === 200) resolve(xhr.response);
      else reject(requestError(xhr.response, xhr.status));
    });
    xhr.addEventListener("error", () => { state.xhr = null; reject(new Error("The network connection was interrupted.")); });
    xhr.addEventListener("abort", () => { state.xhr = null; reject(new DOMException("Upload cancelled", "AbortError")); });
    xhr.send(chunk);
  });
}

async function pollJob() {
  try {
    const response = await fetch(`/v1/jobs/${encodeURIComponent(state.jobId)}`, { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) throw await apiError(response);
    const job = await response.json();
    if (job.state === "ready") return showResult(job);
    if (job.state === "failed") return showError("This conversion stopped.", job.error?.message || "The media could not be converted.");
    const label = job.state === "queued" ? "Waiting for the converter…" : `Making .${targetFor(job.target).extension}…`;
    $("#progress-title").textContent = label;
    state.pollTimer = window.setTimeout(pollJob, document.hidden ? 1800 : 700);
  } catch (error) {
    showError("The job status is unavailable.", error.message);
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
  state.jobId = null; state.file = null; state.mediaClass = null; state.cancelled = true;
  revokePreview();
  $("#file-input").value = "";
  showView("#drop-zone");
  if (deleteJob && oldJobId) { try { await fetch(`/v1/jobs/${encodeURIComponent(oldJobId)}`, { method: "DELETE" }); } catch (_) { /* Best-effort cleanup. */ } }
  announce("Ready for another file.");
}

function showApiFailure(body, status) {
  if (status === 401) return showError("Your session has expired.", "Reload the page to sign in again.");
  const message = body?.error?.message || "The server rejected this conversion.";
  showError(status === 429 ? "The converter is busy." : "The upload was not accepted.", message);
}

function showError(title, detail) {
  clearTimers();
  $("#error-title").textContent = title;
  $("#error-detail").textContent = detail || "Try another file or output format.";
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
async function apiError(response) { let body = null; try { body = await response.json(); } catch (_) { /* Non-JSON upstream error. */ } return new Error(body?.error?.message || `Server request failed (${response.status}).`); }
async function responseBody(response) { try { return await response.json(); } catch (_) { return null; } }
function requestError(body, status) { const error = new Error(body?.error?.message || `Server request failed (${status}).`); error.status = status; error.body = body; return error; }

function updateExpiry(value) {
  const expires = new Date(value).getTime();
  const tick = () => {
    const remaining = Math.max(0, Math.ceil((expires - Date.now()) / 1000));
    $("#expiry-copy").textContent = remaining ? `Disposable result · removed in ${formatClock(remaining)}` : "This result has expired and was removed.";
    if (!remaining) { clearTimers(); $("#download-button").removeAttribute("href"); }
  };
  tick(); state.expiryTimer = window.setInterval(tick, 1000);
}
