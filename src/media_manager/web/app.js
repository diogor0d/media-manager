"use strict";

const $ = (selector) => document.querySelector(selector);
const RECOVERY_KEY = "media-manager-active-job";
const state = { capabilities: null, file: null, mediaClass: null, requestedTarget: null, objectUrl: null, jobId: null, result: null, sharing: false, upload: null, resumeAction: null, xhr: null, pollTimer: null, expiryTimer: null, operation: 0, cancelled: false, reloadOnError: false, waitingWorker: null, updateRequested: false };
const historyState = { items: [], total: 0, request: 0, loading: false, saving: false, deleting: new Set(), timer: null, settingsLoaded: false };

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
    renderQuickFormats();
    updateConnectionStatus();
    $("#limit-copy").textContent = `Up to ${formatBytes(state.capabilities.max_upload_bytes)} · results expire after ${formatDuration(state.capabilities.result_ttl_seconds)}`;
    configureQualitySlider();
    if (await restoreJob()) return;
    announce("Converter ready");
  } catch (error) {
    if (error.reauthenticate) showSessionExpired();
    else showError(navigator.onLine ? "The converter is unavailable." : "You’re offline.", navigator.onLine ? error.message : "Reconnect to choose and convert a file.", "Try again");
  }
}

function bindEvents() {
  const drop = $("#drop-zone");
  const input = $("#file-input");
  drop.addEventListener("click", (event) => { if (event.target !== input) openFilePicker(); });
  drop.addEventListener("keydown", (event) => { if (converterAvailable() && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); openFilePicker(); } });
  input.addEventListener("change", () => input.files[0] && selectFile(input.files[0]));
  ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); if (converterAvailable()) drop.classList.add("is-dragging"); }));
  ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.remove("is-dragging"); }));
  drop.addEventListener("drop", (event) => { if (converterAvailable() && event.dataTransfer.files[0]) { state.requestedTarget = null; selectFile(event.dataTransfer.files[0]); } });
  $("#replace-file").addEventListener("click", () => openFilePicker());
  $("#conversion-form").addEventListener("submit", startConversion);
  $("#target-options").addEventListener("change", updateTargetOptions);
  $("#audio-options").addEventListener("change", updateQualityDetails);
  $("#quality-slider").addEventListener("input", updateQualityDetails);
  $("#cancel-button").addEventListener("click", cancelJob);
  $("#discard-button").addEventListener("click", discardResult);
  $("#another-button").addEventListener("click", () => reset(false));
  $("#share-button").addEventListener("click", shareResult);
  $("#download-button").addEventListener("click", (event) => { if (!navigator.onLine) { event.preventDefault(); announce("Reconnect before downloading the result."); } });
  $("#try-again-button").addEventListener("click", () => state.capabilities && !state.reloadOnError ? reset() : window.location.reload());
  $("#update-button").addEventListener("click", activatePwaUpdate);
  $("#history-button").addEventListener("click", toggleHistory);
  $("#history-refresh").addEventListener("click", () => loadHistory());
  $("#history-more").addEventListener("click", () => loadHistory(true));
  $("#history-settings").addEventListener("submit", saveRetention);
  window.addEventListener("online", resumeAfterReconnect);
  window.addEventListener("offline", updateConnectionStatus);
}

async function registerPwa() {
  if (!("serviceWorker" in navigator)) return;
  try {
    const registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    if (registration.waiting) offerPwaUpdate(registration.waiting);
    registration.addEventListener("updatefound", () => {
      const worker = registration.installing;
      worker?.addEventListener("statechange", () => {
        if (worker.state === "installed" && navigator.serviceWorker.controller) offerPwaUpdate(worker);
      });
    });
    navigator.serviceWorker.addEventListener("controllerchange", () => { if (state.updateRequested) window.location.reload(); });
  } catch (_) { /* The online converter still works without installation. */ }
}

function offerPwaUpdate(worker) { state.waitingWorker = worker; $("#update-notice").hidden = false; }
function activatePwaUpdate() {
  if (!state.waitingWorker) return;
  if (!$("#progress-view").hidden) {
    state.updateRequested = true;
    $("#update-button").textContent = "Waiting for conversion";
    $("#update-button").disabled = true;
    announce("The update will install after this conversion finishes.");
    return;
  }
  state.updateRequested = true;
  state.waitingWorker.postMessage("SKIP_WAITING");
}
function finishDeferredUpdate() {
  if (!state.updateRequested || !state.waitingWorker) return;
  $("#update-button").disabled = false;
  $("#update-button").textContent = "Updating…";
  activatePwaUpdate();
}

function showInstallHint() {
  const isIos = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  $("#install-hint").hidden = !isIos || standalone;
}

function updateConnectionStatus() {
  const status = $("#connection-status");
  status.classList.toggle("is-offline", !navigator.onLine);
  status.hidden = navigator.onLine;
  status.lastChild.textContent = navigator.onLine ? "" : "Offline · conversion paused";
  const available = converterAvailable();
  const drop = $("#drop-zone");
  drop.classList.toggle("is-disabled", !available);
  drop.setAttribute("aria-disabled", String(!available));
  $("#file-input").disabled = !available;
  document.querySelectorAll("#format-guide button").forEach((button) => { button.disabled = !available; });
  $("#replace-file").disabled = !navigator.onLine;
  $("#convert-button").disabled = !navigator.onLine;
  updateResultActions();
  updateHistoryActions();
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
  if (!$("#history-panel").hidden) loadHistory();
}

async function selectFile(file) {
  if (!navigator.onLine) return showError("You’re offline.", "Reconnect to choose and convert a file.", "Try again");
  if (!state.capabilities) return showError("The server is not ready.", "Reload the page and try again.");
  if (file.size === 0) return showError("This file is empty.", "Choose a media file that contains data.");
  if (file.size > state.capabilities.max_upload_bytes) return showError("This file is too large.", `The server accepts files up to ${formatBytes(state.capabilities.max_upload_bytes)}.`);

  const mediaClass = detectMediaClass(file);
  if (!mediaClass) return showError("This format is not advertised.", "Choose a common image, animation, video, or audio file.");
  if (state.requestedTarget) {
    const requested = targetFor(state.requestedTarget);
    if (!requested.accepts.includes(mediaClass)) return showError("Choose a compatible source.", `${requested.label} accepts ${requested.accepts.map(classLabel).join(", ").toLowerCase()}.`);
  }

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

function openFilePicker(targetValue = null) {
  if (!converterAvailable()) return;
  state.requestedTarget = targetValue;
  const input = $("#file-input");
  const sourceTypes = { image: "image/*", animation: "image/gif", video: "video/*", audio: "audio/*" };
  input.accept = targetValue ? targetFor(targetValue).accepts.map((kind) => sourceTypes[kind]).join(",") : "image/*,video/*,audio/*";
  input.value = "";
  input.click();
}

function renderQuickFormats() {
  ["image", "video", "audio"].forEach((group) => {
    const container = $(`#${group}-presets`);
    container.replaceChildren();
    state.capabilities.targets.filter((target) => target.value.startsWith(`${group}-`) || (group === "video" && target.value === "animation-gif")).forEach((target) => {
      const button = document.createElement("button"); button.type = "button"; button.dataset.target = target.value;
      button.setAttribute("aria-label", `Convert to ${target.label}`);
      const strong = document.createElement("strong"); strong.textContent = target.extension;
      const small = document.createElement("small"); small.textContent = target.label;
      button.append(strong, small);
      button.addEventListener("click", () => openFilePicker(target.value));
      container.append(button);
    });
  });
}

function renderTargets(mediaClass) {
  const targets = state.capabilities.targets.filter((target) => target.accepts.includes(mediaClass));
  const preferred = state.requestedTarget || preferredTargets[mediaClass];
  state.requestedTarget = null;
  const container = $("#target-options");
  container.replaceChildren();
  targets.forEach((target, index) => {
    const wrapper = document.createElement("div"); wrapper.className = "target-option";
    const input = document.createElement("input"); input.type = "radio"; input.name = "target"; input.id = `target-${target.value}`; input.value = target.value;
    input.checked = target.value === preferred || (index === 0 && !targets.some((item) => item.value === preferred));
    const label = document.createElement("label"); label.htmlFor = input.id;
    const strong = document.createElement("strong"); strong.textContent = target.extension;
    const small = document.createElement("small"); small.textContent = target.label;
    label.append(strong, small); wrapper.append(input, label); container.append(wrapper);
  });
  $("#target-count").textContent = `${targets.length} available`;
  updateTargetOptions();
}

function configureQualitySlider() {
  const scale = state.capabilities.quality_scale;
  const slider = $("#quality-slider");
  slider.min = scale.minimum; slider.max = scale.maximum; slider.step = scale.step; slider.value = scale.default;
}

async function restoreJob() {
  const jobId = readRecovery();
  if (!jobId) return false;
  const operation = state.operation;
  state.cancelled = false;
  state.jobId = jobId;
  try {
    const response = await fetch(`/v1/jobs/${encodeURIComponent(jobId)}`, { headers: { Accept: "application/json" }, cache: "no-store" });
    const job = await apiJson(response);
    if (!isCurrentOperation(operation, jobId)) return true;
    if (job.state === "ready") showResult(job);
    else if (job.state === "failed") { clearRecovery(); showError("This conversion stopped.", job.error?.message || "The media could not be converted."); }
    else {
      showView("#progress-view");
      $("#progress-view").classList.add("is-processing");
      renderJobProgress(job);
      pollJob();
    }
    announce("Recovered the active conversion.");
    return true;
  } catch (error) {
    if (!isCurrentOperation(operation, jobId)) return true;
    if (error.status === 404 || error.status === 410) {
      clearRecovery();
      state.jobId = null;
      return false;
    }
    if (error.reauthenticate) throw error;
    if (!navigator.onLine) {
      showView("#progress-view");
      state.resumeAction = "poll";
      setProgress("PAUSED", "Status check paused offline", "Reconnecting will recover your conversion automatically.");
    } else showError("The conversion could not be recovered yet.", "Reload to check the retained job again.", "Reload", true);
    return true;
  }
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
  updateQualityDetails();
}

function updateQualityDetails() {
  const slider = $("#quality-slider");
  const percent = Number(slider.value);
  $("#quality-value").textContent = `${percent}%`;
  const targetValue = document.querySelector('input[name="target"]:checked')?.value;
  if (!targetValue) return;
  const target = targetFor(targetValue);
  const dropAudio = document.querySelector('input[name="audio"]:checked')?.value === "drop";
  const details = target.quality_metrics
    .filter((metric) => !(dropAudio && metric.label.toLowerCase().includes("audio")))
    .map((metric) => `${metric.label} ${interpolateMetric(metric, percent)}${metric.unit === "CRF" ? "" : ` ${metric.unit}`}`);
  $("#quality-detail").textContent = details.join(" · ");
  $("#quality-note").textContent = target.value === "image-png" ? "Lossless · compression effort only" : "100% is not necessarily lossless.";
  slider.setAttribute("aria-valuetext", `${percent}%, ${details.join(", ")}`);
}

function interpolateMetric(metric, percent) {
  if (percent <= 50) return Math.round(metric.economy + (metric.balanced - metric.economy) * percent / 50);
  return Math.round(metric.balanced + (metric.high - metric.balanced) * (percent - 50) / 50);
}

async function startConversion(event) {
  event.preventDefault();
  if (!$("#progress-view").hidden) return;
  if (!navigator.onLine) return showError("You’re offline.", "Reconnect before starting the upload.", "Try again");
  const targetValue = document.querySelector('input[name="target"]:checked').value;
  const qualityPercent = $("#quality-slider").value;
  const resolution = $("#resolution-select").value || "source";
  const audio = document.querySelector('input[name="audio"]:checked')?.value || "keep";
  const query = new URLSearchParams({ target: targetValue, quality_percent: qualityPercent, resolution, audio });
  state.cancelled = false;
  const operation = ++state.operation;
  showView("#progress-view");
  $("#progress-view").classList.remove("is-processing");
  renderStage("upload", 0);
  setProgress("UPLOADING", "Sending secure chunks…", `0 of ${formatBytes(state.file.size)}`);
  try {
    const createdResponse = await fetch(`/v1/uploads?${query}`, {
      method: "POST",
      headers: { Accept: "application/json", "Upload-Length": String(state.file.size), "Upload-Filename": encodeURIComponent(state.file.name) },
    });
    const upload = await apiJson(createdResponse);
    if (!isCurrentOperation(operation)) {
      await fetch(`/v1/jobs/${encodeURIComponent(upload.id)}`, { method: "DELETE" });
      return;
    }

    state.jobId = upload.id;
    state.upload = upload;
    continueUpload();
  } catch (error) {
    if (isCurrentOperation(operation)) handleUploadError(error);
  }
}

async function continueUpload() {
  const operation = state.operation;
  const jobId = state.jobId;
  try {
    while (state.upload.offset < state.upload.length) {
      if (!isCurrentOperation(operation, jobId)) return;
      const upload = await uploadNextChunk(state.upload);
      if (!isCurrentOperation(operation, jobId)) return;
      state.upload = upload;
    }
    if (!isCurrentOperation(operation, jobId)) return;

    let job;
    try {
      const completedResponse = await fetch(`/v1/uploads/${encodeURIComponent(jobId)}/complete`, {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      job = await apiJson(completedResponse);
    } catch (error) {
      if (!isCurrentOperation(operation, jobId) || (error.status && error.body?.error?.code !== "UPLOAD_ALREADY_COMPLETED")) throw error;
      // A previous completion may have succeeded before its reply was lost.
      const response = await fetch(`/v1/jobs/${encodeURIComponent(jobId)}`, { headers: { Accept: "application/json" }, cache: "no-store" });
      job = await apiJson(response);
      if (job.state === "uploading") throw error;
    }
    if (!isCurrentOperation(operation, jobId)) return;
    state.upload = null;
    $("#progress-view").classList.add("is-processing");
    renderStage("inspecting", null);
    state.jobId = job.id;
    rememberJob(job.id);
    pollJob();
  } catch (error) {
    if (isCurrentOperation(operation, jobId)) handleUploadError(error);
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
  const operation = state.operation;
  if (state.cancelled) throw new DOMException("Upload cancelled", "AbortError");
  const end = Math.min(upload.offset + upload.chunk_size, upload.length);
  const chunk = state.file.slice(upload.offset, end);
  try {
    return await sendChunk(upload, chunk);
  } catch (error) {
    if (!isCurrentOperation(operation, upload.id) || error.name === "AbortError" || error.reauthenticate) throw error;
    const status = await fetch(`/v1/uploads/${encodeURIComponent(upload.id)}`, {
      headers: { Accept: "application/json" }, cache: "no-store",
    });
    const recovered = await apiJson(status);
    if (!isCurrentOperation(operation, upload.id)) throw new DOMException("Upload cancelled", "AbortError");
    if (recovered.offset > upload.offset) return recovered;
    return sendChunk(recovered, state.file.slice(recovered.offset, Math.min(recovered.offset + recovered.chunk_size, recovered.length)));
  }
}

function sendChunk(upload, chunk) {
  const operation = state.operation;
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    state.xhr = xhr;
    xhr.open("PATCH", `/v1/uploads/${encodeURIComponent(upload.id)}`);
    xhr.responseType = "json";
    xhr.setRequestHeader("Content-Type", "application/offset+octet-stream");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Upload-Offset", String(upload.offset));
    xhr.upload.addEventListener("progress", (progress) => {
      if (!isCurrentOperation(operation, upload.id)) return;
      if (!progress.lengthComputable) return;
      const loaded = upload.offset + progress.loaded;
      const percent = Math.round((loaded / upload.length) * 100);
      $("#upload-bar").style.width = `${percent}%`;
      $("#upload-meter").setAttribute("aria-valuenow", String(percent));
      $("#progress-detail").textContent = `${formatBytes(loaded)} of ${formatBytes(upload.length)}`;
    });
    xhr.addEventListener("load", () => {
      if (state.xhr === xhr) state.xhr = null;
      const contentType = xhr.getResponseHeader("Content-Type") || "";
      const sameOrigin = !xhr.responseURL || new URL(xhr.responseURL).origin === window.location.origin;
      if (!sameOrigin) reject(requestError(null, 401, true));
      else if (!contentType.includes("application/json")) reject(requestError(null, xhr.status, xhr.status === 401 || xhr.status === 403));
      else if (xhr.status === 200) resolve(xhr.response);
      else reject(requestError(xhr.response, xhr.status, xhr.status === 401 || xhr.status === 403));
    });
    xhr.addEventListener("error", () => { if (state.xhr === xhr) state.xhr = null; reject(new Error("The network connection was interrupted.")); });
    xhr.addEventListener("abort", () => { if (state.xhr === xhr) state.xhr = null; reject(new DOMException("Upload cancelled", "AbortError")); });
    xhr.send(chunk);
  });
}

async function pollJob() {
  const operation = state.operation;
  const jobId = state.jobId;
  if (!isCurrentOperation(operation, jobId) || !jobId) return;
  try {
    const response = await fetch(`/v1/jobs/${encodeURIComponent(jobId)}`, { headers: { Accept: "application/json" }, cache: "no-store" });
    const job = await apiJson(response);
    if (!isCurrentOperation(operation, jobId)) return;
    if (job.state === "ready") return showResult(job);
    if (job.state === "failed") {
      clearRecovery();
      showError("This conversion stopped.", job.error?.message || "The media could not be converted.");
      finishDeferredUpdate();
      return;
    }
    renderJobProgress(job);
    state.pollTimer = window.setTimeout(pollJob, document.hidden ? 1800 : 700);
  } catch (error) {
    if (!isCurrentOperation(operation, jobId)) return;
    if (error.reauthenticate) showSessionExpired();
    else if (!navigator.onLine) {
      state.resumeAction = "poll";
      setProgress("PAUSED", "Status check paused offline", "Reconnecting will resume this conversion automatically.");
      announce("Status checks paused offline.");
    }
    else if (!error.status) showError("The connection or session was lost.", "Reload to reconnect or sign in again.", "Reload", true);
    else if (error.status === 404 || error.status === 410) {
      clearRecovery();
      showError("This conversion is no longer available.", "It expired or was removed. Choose another file.");
    } else showError("The job status is unavailable.", error.message, "Reload", true);
  }
}

function renderJobProgress(job) {
  if (job.state === "queued") return renderStage("queued", null);
  const stage = job.progress?.stage || "inspecting";
  renderStage(stage, job.progress?.percent ?? null, targetFor(job.target));
}

function renderStage(stage, percent, target = null) {
  const order = ["upload", "inspecting", "converting", "validating"];
  const active = stage === "queued" ? "inspecting" : stage;
  const activeIndex = order.indexOf(active);
  document.querySelectorAll("#progress-stages li").forEach((item) => {
    const index = order.indexOf(item.dataset.stage);
    item.classList.toggle("is-complete", index < activeIndex);
    item.classList.toggle("is-active", index === activeIndex);
    if (index === activeIndex) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
  const copy = {
    upload: ["UPLOADING", "Sending secure chunks…", `${percent ?? 0}% transferred to the private workspace.`],
    queued: ["QUEUED", "Waiting for the converter…", "Your upload is complete and safely queued."],
    inspecting: ["INSPECTING", "Reading streams and limits…", "FFprobe is validating codecs, dimensions, duration, and stream layout."],
    converting: ["CONVERTING", `Encoding ${target ? `.${target.extension}` : "output"}…`, percent == null ? "FFmpeg is converting the selected streams." : `${percent}% of the media timeline encoded.`],
    validating: ["VERIFYING", "Checking the finished file…", "The output container, codec, streams, size, and metadata are being verified."],
  }[stage];
  setProgress(...copy);
  setMeter(percent, stage === "upload" ? "Upload progress" : "Conversion progress");
}

function setMeter(percent, label) {
  const meter = $("#upload-meter");
  meter.setAttribute("aria-label", label);
  meter.classList.toggle("is-indeterminate", percent == null);
  if (percent == null) meter.removeAttribute("aria-valuenow");
  else {
    meter.setAttribute("aria-valuenow", String(percent));
    $("#upload-bar").style.width = `${percent}%`;
  }
}

function showResult(job) {
  clearTimers();
  state.jobId = job.id;
  state.result = job.output;
  state.sharing = false;
  rememberJob(job.id);
  const input = job.input; const output = job.output; const target = targetFor(job.target);
  $("#result-input-size").textContent = formatBytes(input.bytes);
  $("#result-output-size").textContent = formatBytes(output.bytes);
  $("#result-input-meta").textContent = mediaMeta(input);
  $("#result-output-meta").textContent = mediaMeta(output);
  const difference = input.bytes - output.bytes;
  $("#size-difference").textContent = difference >= 0 ? `${formatBytes(difference)} smaller than the source.` : `${formatBytes(Math.abs(difference))} larger than the source.`;
  $("#download-button").href = `/v1/jobs/${encodeURIComponent(job.id)}/content`;
  $("#download-button").setAttribute("download", output.filename);
  $("#download-button").firstChild.textContent = "Open ";
  $("#download-extension").textContent = `.${target.extension}`;
  $("#share-extension").textContent = `.${target.extension}`;
  $("#share-button").firstChild.textContent = "Share or save ";
  $("#share-button").hidden = !(navigator.share && window.File);
  showView("#result-view");
  updateExpiry(job.expires_at);
  updateResultActions();
  if (state.result) announce("Conversion ready. Review the result size before downloading.");
  if (!$("#history-panel").hidden) loadHistory();
  finishDeferredUpdate();
}

async function shareResult() {
  if (state.sharing) return;
  if (!navigator.onLine || !state.jobId || !state.result) return announce("Reconnect before sharing the result.");
  const button = $("#share-button");
  const operation = state.operation;
  const jobId = state.jobId;
  const result = state.result;
  state.sharing = true;
  button.disabled = true;
  button.firstChild.textContent = "Preparing… ";
  try {
    const response = await fetch(`/v1/jobs/${encodeURIComponent(jobId)}/content`);
    if (!response.ok) throw new Error("The result could not be opened.");
    const blob = await response.blob();
    if (!isCurrentOperation(operation, jobId) || state.result !== result) return;
    const file = new File([blob], result.filename, { type: result.media_type });
    if (!navigator.canShare?.({ files: [file] })) throw new Error("This result cannot be shared directly. Use Open instead.");
    await navigator.share({ files: [file], title: result.filename });
    if (!isCurrentOperation(operation, jobId)) return;
    announce("Share sheet closed. The result remains in history until expiry.");
  } catch (error) {
    if (isCurrentOperation(operation, jobId) && error.name !== "AbortError") announce(error.message || "Use Open to save this result.");
  } finally {
    if (isCurrentOperation(operation, jobId)) {
      state.sharing = false;
      updateResultActions();
      button.firstChild.textContent = "Share or save ";
    }
  }
}

async function cancelJob() {
  await reset();
}

async function discardResult() {
  if (!state.jobId) return reset(false);
  if (!navigator.onLine) return announce("Reconnect before discarding the result.");
  const jobId = state.jobId;
  const operation = state.operation;
  const button = $("#discard-button");
  button.disabled = true;
  try {
    const response = await fetch(`/v1/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE", cache: "no-store" });
    if (!response.ok && response.status !== 404 && response.status !== 410) await apiJson(response);
    if (!isCurrentOperation(operation, jobId)) return;
    await reset(false);
    if (!$("#history-panel").hidden) loadHistory();
  } catch (error) {
    if (error.reauthenticate) showSessionExpired();
    else announce(error.message || "The result could not be discarded. Try again.");
  } finally { button.disabled = false; }
}

async function reset(deleteJob = !state.result) {
  const oldJobId = state.jobId;
  ++state.operation;
  state.cancelled = true;
  if (state.xhr) state.xhr.abort();
  clearTimers();
  state.jobId = null; state.result = null; state.sharing = false; state.file = null; state.mediaClass = null; state.upload = null; state.resumeAction = null; state.reloadOnError = false;
  clearRecovery();
  revokePreview();
  $("#file-input").value = "";
  state.requestedTarget = null;
  showView("#drop-zone");
  finishDeferredUpdate();
  announce("Ready for another file.");
  if (deleteJob && oldJobId) { try { await fetch(`/v1/jobs/${encodeURIComponent(oldJobId)}`, { method: "DELETE" }); } catch (_) { /* Best-effort cleanup. */ } }
}

function showApiFailure(body, status) {
  if (status === 401 || status === 403) return showSessionExpired();
  const message = body?.error?.message || "The server rejected this conversion.";
  showError(status === 429 ? "The converter is busy." : "The upload was not accepted.", message);
}

function showSessionExpired() {
  ++historyState.request;
  historyState.items = []; historyState.total = 0; historyState.settingsLoaded = false; historyState.loading = false;
  $("#history-list").replaceChildren();
  $("#history-more").hidden = true;
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

function showView(selector) {
  views.forEach((view) => { $(view).hidden = view !== selector; });
  const focusTarget = $(selector).matches("[data-view-focus]") ? $(selector) : $(selector).querySelector("[data-view-focus]");
  window.requestAnimationFrame(() => focusTarget?.focus({ preventScroll: false }));
}
function setProgress(kicker, title, detail) { $("#progress-kicker").textContent = kicker; $("#progress-title").textContent = title; $("#progress-detail").textContent = detail; }
function setDimensions(width, height) { if (!width || !height) return; $("#file-dimensions").textContent = `${width} × ${height}`; $("#dimensions-fact").hidden = false; }
function setDuration(seconds) { if (!Number.isFinite(seconds)) return; $("#file-duration").textContent = formatClock(seconds); $("#duration-fact").hidden = false; }
function targetFor(value) { return state.capabilities.targets.find((target) => target.value === value); }
function getExtension(name) { const match = String(name).toLowerCase().match(/\.([a-z0-9]+)$/); return match ? match[1] : ""; }
function classLabel(value) { return ({ image: "Still image", animation: "Animation", video: "Video", audio: "Audio" })[value]; }
function formatBytes(bytes) { if (bytes < 1024) return `${bytes} B`; const units = ["KB", "MB", "GB"]; let value = bytes; let unit = "B"; for (const next of units) { value /= 1024; unit = next; if (value < 1024) break; } return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${unit}`; }
function formatClock(seconds) { const rounded = Math.round(seconds); const minutes = Math.floor(rounded / 60); return `${minutes}:${String(rounded % 60).padStart(2, "0")}`; }
function formatDuration(seconds) {
  if (seconds >= 86400) { const days = Math.round(seconds / 86400); return `${days} ${days === 1 ? "day" : "days"}`; }
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600} hr`;
  return seconds % 60 === 0 ? `${seconds / 60} min` : `${seconds} sec`;
}
function mediaMeta(media) { const pieces = []; if (media.width && media.height) pieces.push(`${media.width} × ${media.height}`); if (media.duration_ms != null) pieces.push(formatClock(media.duration_ms / 1000)); return pieces.join(" · ") || "Exact server result"; }
function announce(message) { $("#live-status").textContent = message; }
function revokePreview() { if (state.objectUrl) URL.revokeObjectURL(state.objectUrl); state.objectUrl = null; $("#preview")?.replaceChildren(); }
function clearTimers() { window.clearTimeout(state.pollTimer); window.clearInterval(state.expiryTimer); state.pollTimer = null; state.expiryTimer = null; }
function isCurrentOperation(operation, jobId = state.jobId) { return operation === state.operation && !state.cancelled && jobId === state.jobId; }
function readRecovery() { try { return sessionStorage.getItem(RECOVERY_KEY); } catch (_) { return null; } }
function rememberJob(jobId) { try { sessionStorage.setItem(RECOVERY_KEY, jobId); } catch (_) { /* Recovery is optional when browser storage is unavailable. */ } }
function clearRecovery() { try { sessionStorage.removeItem(RECOVERY_KEY); } catch (_) { /* Browser storage may be unavailable. */ } }
function updateResultActions() {
  const available = navigator.onLine && Boolean(state.result);
  const download = $("#download-button");
  download.classList.toggle("is-disabled", !available);
  download.setAttribute("aria-disabled", String(!available));
  download.tabIndex = available ? 0 : -1;
  $("#share-button").disabled = !available || state.sharing;
  $("#discard-button").disabled = !navigator.onLine;
}
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
    $("#expiry-copy").textContent = remaining ? `Saved in history · expires ${formatHistoryDate(value)}` : "This result has expired.";
    if (!remaining) {
      clearTimers(); clearRecovery(); state.result = null;
      const download = $("#download-button");
      download.removeAttribute("href"); download.removeAttribute("download"); download.classList.add("is-disabled"); download.setAttribute("aria-disabled", "true"); download.tabIndex = -1; download.firstChild.textContent = "Expired ";
      $("#share-button").disabled = true;
      announce("The result expired.");
    }
  };
  state.expiryTimer = window.setInterval(tick, 1000); tick();
}

function formatHistoryDate(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

async function toggleHistory() {
  const panel = $("#history-panel");
  panel.hidden = !panel.hidden;
  $("#history-button").setAttribute("aria-expanded", String(!panel.hidden));
  window.clearInterval(historyState.timer);
  historyState.timer = null;
  if (panel.hidden) return;
  $("#history-title").focus();
  panel.scrollIntoView({ behavior: "auto", block: "start" });
  await loadHistory();
  if (!panel.hidden) historyState.timer = window.setInterval(() => { if (!document.hidden && !historyState.loading) loadHistory(); }, 60000);
}

async function loadHistory(append = false) {
  const request = ++historyState.request;
  if (!navigator.onLine) {
    historyState.loading = false;
    $("#history-message").textContent = "Reconnect to load history or download results.";
    updateHistoryActions();
    return;
  }
  historyState.loading = true;
  $("#history-message").textContent = "Loading history…";
  updateHistoryActions();
  try {
    const offset = append ? historyState.items.length : 0;
    const limit = append ? 20 : Math.max(20, Math.min(100, historyState.items.length));
    const tasks = [fetch(`/v1/history?limit=${limit}&offset=${offset}`, { headers: { Accept: "application/json" }, cache: "no-store" }).then(apiJson)];
    if (!historyState.settingsLoaded) tasks.push(fetch("/v1/history/settings", { headers: { Accept: "application/json" }, cache: "no-store" }).then(apiJson));
    const [history, settings] = await Promise.all(tasks);
    if (request !== historyState.request) return;
    historyState.items = append ? [...historyState.items, ...history.items] : history.items;
    historyState.total = history.total;
    if (settings) { $("#retention-days").value = settings.retention_days; historyState.settingsLoaded = true; }
    renderHistory();
    $("#history-message").textContent = history.total ? `${history.total} ${history.total === 1 ? "result" : "results"}` : "No saved results yet. Completed conversions appear here.";
  } catch (error) {
    if (request !== historyState.request) return;
    $("#history-message").textContent = error.reauthenticate ? "Sign in again to view history." : "History could not be loaded. Try Refresh.";
    if (error.reauthenticate) showSessionExpired();
  } finally {
    if (request === historyState.request) { historyState.loading = false; updateHistoryActions(); }
  }
}

function renderHistory() {
  const list = $("#history-list");
  list.replaceChildren();
  historyState.items.forEach((entry) => {
    const job = entry.job;
    const row = document.createElement("li"); row.className = "history-row";
    const detail = document.createElement("div"); detail.className = "history-detail";
    const name = document.createElement("strong"); name.textContent = job.output?.filename || entry.source_filename || "Unnamed media";
    const metadata = document.createElement("p");
    metadata.textContent = `${entry.kind === "compression" ? "Compressed" : "Converted"} · ${formatHistoryDate(entry.completed_at)}${job.output ? ` · ${formatBytes(job.output.bytes)}` : ""}`;
    const expiry = document.createElement("p"); expiry.className = job.state === "failed" ? "history-failure" : "history-expiry";
    expiry.textContent = job.state === "failed" ? `Failed · ${job.error?.message || "The media could not be processed."}` : `Expires ${formatHistoryDate(job.expires_at)}`;
    detail.append(name, metadata, expiry);
    const actions = document.createElement("div"); actions.className = "history-actions";
    const base = `/v1/${entry.kind === "compression" ? "compressions" : "jobs"}/${encodeURIComponent(entry.id)}`;
    if (job.state === "ready" && job.output) {
      const download = document.createElement("a"); download.className = "secondary-action history-download";
      download.href = `${base}/content`; download.setAttribute("download", job.output.filename); download.textContent = "Download";
      download.setAttribute("aria-label", `Download ${job.output.filename}`);
      download.dataset.expires = job.expires_at;
      download.addEventListener("click", (event) => { if (!navigator.onLine || new Date(job.expires_at).getTime() <= Date.now()) { event.preventDefault(); announce("Reconnect or refresh history before downloading."); } });
      actions.append(download);
    }
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "text-action history-delete"; remove.textContent = "Delete";
    remove.dataset.jobId = entry.id;
    remove.setAttribute("aria-label", `Delete ${name.textContent}`);
    remove.addEventListener("click", () => deleteHistoryEntry(entry, remove));
    actions.append(remove); row.append(detail, actions); list.append(row);
  });
  $("#history-more").hidden = historyState.items.length >= historyState.total;
}

function updateHistoryActions() {
  $("#history-refresh").disabled = !navigator.onLine || historyState.loading;
  $("#history-more").disabled = !navigator.onLine || historyState.loading;
  $("#retention-save").disabled = !navigator.onLine || !historyState.settingsLoaded || historyState.saving;
  $("#retention-days").disabled = !navigator.onLine || !historyState.settingsLoaded || historyState.saving;
  document.querySelectorAll(".history-delete").forEach((button) => { button.disabled = !navigator.onLine || historyState.deleting.has(button.dataset.jobId); });
  document.querySelectorAll(".history-download").forEach((link) => {
    const available = navigator.onLine && new Date(link.dataset.expires).getTime() > Date.now();
    link.classList.toggle("is-disabled", !available); link.setAttribute("aria-disabled", String(!available)); link.tabIndex = available ? 0 : -1;
  });
}

async function saveRetention(event) {
  event.preventDefault();
  const input = $("#retention-days");
  const days = Number(input.value);
  if (!navigator.onLine || !historyState.settingsLoaded || historyState.saving) return;
  if (!Number.isInteger(days) || days < 1 || days > 90) { input.reportValidity(); return; }
  historyState.saving = true;
  updateHistoryActions();
  try {
    const settings = await apiJson(await fetch("/v1/history/settings", { method: "PUT", headers: { "Content-Type": "application/json", Accept: "application/json" }, cache: "no-store", body: JSON.stringify({ retention_days: days }) }));
    input.value = settings.retention_days;
    if (state.capabilities) { state.capabilities.result_ttl_seconds = settings.retention_days * 86400; $("#limit-copy").textContent = `Up to ${formatBytes(state.capabilities.max_upload_bytes)} · results expire after ${formatDuration(state.capabilities.result_ttl_seconds)}`; }
    $("#history-message").textContent = `Saved. New results will be kept for ${settings.retention_days} ${settings.retention_days === 1 ? "day" : "days"}.`;
  } catch (error) {
    $("#history-message").textContent = error.reauthenticate ? "Sign in again to save retention." : "Retention could not be saved. Try again.";
    if (error.reauthenticate) showSessionExpired();
  } finally { historyState.saving = false; updateHistoryActions(); }
}

async function deleteHistoryEntry(entry, button) {
  if (!navigator.onLine || historyState.deleting.has(entry.id) || !window.confirm(`Delete ${entry.job.output?.filename || entry.source_filename || "this result"} from history? This removes the saved result.`)) return;
  historyState.deleting.add(entry.id);
  button.disabled = true;
  const base = `/v1/${entry.kind === "compression" ? "compressions" : "jobs"}/${encodeURIComponent(entry.id)}`;
  try {
    const response = await fetch(base, { method: "DELETE", cache: "no-store" });
    if (!response.ok && response.status !== 404 && response.status !== 410) await apiJson(response);
    if (state.jobId === entry.id) await reset(false);
    await loadHistory();
  } catch (error) {
    $("#history-message").textContent = error.reauthenticate ? "Sign in again to delete results." : "The result could not be deleted. Try again.";
    if (error.reauthenticate) showSessionExpired();
  } finally { historyState.deleting.delete(entry.id); updateHistoryActions(); }
}
