"use strict";

// CSP-safe: all wiring via addEventListener, zero inline handlers (plan D4).
// Server messages are rendered verbatim through textContent, which also keeps
// untrusted video titles inert.

const form = document.getElementById("info-form");
const urlInput = document.getElementById("url-input");
const fetchButton = document.getElementById("fetch-info");
const errorBanner = document.getElementById("error-banner");
const doneNotice = document.getElementById("done-notice");
const metadataCard = document.getElementById("metadata-card");
const metaThumbnail = document.getElementById("meta-thumbnail");
const metaTitle = document.getElementById("meta-title");
const metaChannel = document.getElementById("meta-channel");
const metaDuration = document.getElementById("meta-duration");
const extractPanel = document.getElementById("extract-panel");
const bitrateSelect = document.getElementById("bitrate-select");
const startButton = document.getElementById("start-extract");
const jobStatus = document.getElementById("job-status");
const progressTrack = document.getElementById("progress-track");
const progressBar = document.getElementById("progress-bar");
const downloadButton = document.getElementById("download-file");

let currentUrl = null;
let currentJobId = null;
let pollTimer = null;

function showError(message) {
  errorBanner.textContent = message;
  errorBanner.hidden = false;
  doneNotice.hidden = true;
  metadataCard.hidden = true;
  extractPanel.hidden = true;
}

function clearFeedback() {
  errorBanner.hidden = true;
  errorBanner.textContent = "";
  doneNotice.hidden = true;
  doneNotice.textContent = "";
  metadataCard.hidden = true;
  extractPanel.hidden = true;
  resetJobUi();
}

function resetJobUi() {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  currentJobId = null;
  jobStatus.hidden = true;
  jobStatus.textContent = "";
  progressTrack.hidden = true;
  progressBar.style.width = "0%";
  downloadButton.hidden = true;
  startButton.disabled = false;
}

function renderProgress(percent) {
  progressTrack.hidden = false;
  progressBar.style.width = `${percent}%`;
}

// kind: "info" (normal progress) | "failure" (the job itself failed) |
// "refusal" (the server refused to admit the job: 429/503). Refusals are
// styled distinctly because they are retryable capacity signals, not
// failures of this video.
function setJobStatus(text, kind = "info") {
  jobStatus.textContent = text;
  jobStatus.className = `job-status job-status-${kind}`;
  jobStatus.hidden = false;
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  startButton.disabled = false;
}

function formatDuration(totalSeconds) {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(seconds)}`
    : `${minutes}:${pad(seconds)}`;
}

function renderInfo(info) {
  metaThumbnail.src = info.thumbnail_url;
  metaTitle.textContent = info.title;
  metaChannel.textContent = info.channel;
  metaDuration.textContent = `Duration: ${formatDuration(info.duration_seconds)}`;
  metadataCard.hidden = false;
  extractPanel.hidden = false;
}

async function fetchInfo(url) {
  const response = await fetch(`/api/info?url=${encodeURIComponent(url)}`);
  let body;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const message =
      body && body.error && body.error.message
        ? body.error.message
        : "Unexpected server error.";
    showError(message);
    return;
  }
  renderInfo(body);
}

async function startExtraction() {
  resetJobUi();
  startButton.disabled = true;
  setJobStatus("Starting…");
  let response;
  try {
    response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: currentUrl,
        bitrate_kbps: Number(bitrateSelect.value),
      }),
    });
  } catch {
    setJobStatus("Could not reach the server. Is it running?");
    startButton.disabled = false;
    return;
  }
  let body;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (response.status !== 202) {
    const message =
      body && body.error && body.error.message
        ? body.error.message
        : "Unexpected server error.";
    // 429 CLIENT_LIMIT / 503 AT_CAPACITY / 503 LOW_DISK: the server is busy,
    // not the video's fault — say so in its own style.
    const refused = response.status === 429 || response.status === 503;
    setJobStatus(message, refused ? "refusal" : "failure");
    startButton.disabled = false;
    return;
  }
  currentJobId = body.job_id;
  setJobStatus(`Queued (position ${body.queue_position})`);
  pollTimer = setInterval(pollJob, 2000);
}

async function pollJob() {
  if (currentJobId === null) return;
  let response;
  try {
    response = await fetch(`/api/jobs/${currentJobId}`);
  } catch {
    return; // transient; keep polling
  }
  let body;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    stopPolling();
    setJobStatus(
      body && body.error && body.error.message
        ? body.error.message
        : "The job is no longer available.",
      "failure"
    );
    return;
  }
  if (body.status === "queued") {
    setJobStatus(`Queued (position ${body.queue_position})`);
  } else if (body.status === "running") {
    const percent = typeof body.progress === "number" ? body.progress : 0;
    const label = body.phase === "converting" ? "Converting" : "Downloading";
    setJobStatus(`${label}… ${percent}%`);
    renderProgress(percent);
  } else if (body.status === "completed") {
    stopPolling();
    renderProgress(100);
    setJobStatus("Ready — your MP3 is ready to download.");
    downloadButton.hidden = false;
  } else if (body.status === "failed") {
    stopPolling();
    progressTrack.hidden = true;
    // The server's reason is rendered verbatim (textContent keeps it inert).
    setJobStatus(
      body.error && body.error.message
        ? body.error.message
        : "The extraction did not complete.",
      "failure"
    );
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearFeedback();
  fetchButton.disabled = true;
  try {
    currentUrl = urlInput.value.trim();
    await fetchInfo(currentUrl);
  } catch {
    showError("Could not reach the server. Is it running?");
  } finally {
    fetchButton.disabled = false;
  }
});

startButton.addEventListener("click", startExtraction);

downloadButton.addEventListener("click", () => {
  if (currentJobId === null) return;
  // Native navigation so the browser streams the attachment straight to
  // disk. Delivery purges the job server-side, so the handle is spent the
  // moment this fires — reset to the empty state for the next URL.
  window.location.assign(`/api/jobs/${currentJobId}/file`);
  clearFeedback();
  urlInput.value = "";
  currentUrl = null;
  doneNotice.textContent =
    "Done — nothing retained on the server. Paste another URL to extract again.";
  doneNotice.hidden = false;
  urlInput.focus();
});
