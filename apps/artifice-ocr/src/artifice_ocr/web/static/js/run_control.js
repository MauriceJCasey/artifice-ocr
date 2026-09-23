// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// ------------------------------------------------------------------- running

// Pause/Resume is one button whose icon and label swap with run state; both
// icons live in icons.js (Design_Philosophy.md §8.8) rather than as a
// Unicode dingbat mixed into the label — see the app.css note by the
// `.icon-btn` rule for why that used to inflate the button's line box.
function setPauseButtonLabel(paused) {
  els["btn-pause"].innerHTML = (paused ? Icons.play : Icons.pause) +
    `<span>${paused ? "Resume" : "Pause"}</span>`;
}

function setRunning(isRunning) {
  running = isRunning;
  els["btn-run"].disabled = isRunning;
  els["btn-pause"].disabled = !isRunning;
  els["btn-stop"].disabled = !isRunning;
  if (!isRunning) {
    setPauseButtonLabel(false);
    els["progress-bar"].classList.remove("active");
  }
}

function applyRunStatus(status) {
  setRunning(!!status.running);
  if (status.paused) setPauseButtonLabel(true);
}

els["btn-run"].onclick = async () => {
  const stages = [];
  if (els["stage-ocr"].checked) stages.push("ocr");
  if (els["stage-cleanup"].checked) stages.push("cleanup");
  if (els["stage-title"].checked) stages.push("title");
  if (els["stage-translate"].checked) stages.push("translate");
  if (!items.size) { log("Add at least one document first.", "warning"); return; }
  if (!stages.includes("ocr")) { log("OCR is required for every new run.", "warning"); return; }

  const body = {
    stages, output_dir: els["output-dir"].value || "output",
    project: (els["output-dir"].value || "output") === "output" ? "OCR project" : null,
    force: els["stage-force"].checked,
  };
  // Only include segmentation_provider when the toggle is on and a provider is selected.
  if (els["stage-segmentation"] && els["stage-segmentation"].checked) {
    const provider = els["seg-provider"] ? els["seg-provider"].value : "";
    if (provider) body.segmentation_provider = provider;
    if (provider === "diff-residual") {
      const referenceImage = SegmentationToggle.getReferenceImage();
      if (!referenceImage) {
        log("diff-residual needs a reference scan — pick one before running.", "warning");
        return;
      }
      body.segmentation_reference_image = referenceImage;
    }
  }
  try {
    const result = await api("POST", "/api/run/start", body);
    if (result.output_dir) els["output-dir"].value = result.output_dir;
    setRunning(true);
    els["progress-bar"].style.width = "0%";
    const pv = els["progress-value"];
    if (pv) pv.textContent = "0%";
  } catch (err) {
    log(`Could not start: ${friendlyError(err)}`, "error");
  }
};

els["btn-pause"].onclick = async () => {
  const paused = els["btn-pause"].textContent.includes("Resume");
  await api("POST", paused ? "/api/run/resume" : "/api/run/pause");
  setPauseButtonLabel(!paused);
};
els["btn-stop"].onclick = async () => {
  await api("POST", "/api/run/cancel");
    els["status-text"].textContent = "Stopping — finishing current work…";
};

// ---------------------------------------------------------------------- log

function log(message, tag) {
  // Remove the empty-state placeholder when the first real message arrives.
  const empty = els["log"].querySelector(".log-empty");
  if (empty) empty.remove();
  const line = document.createElement("div");
  line.className = `line ${tag || ""}`;
  line.textContent = message;
  els["log"].appendChild(line);
  els["log"].scrollTop = els["log"].scrollHeight;
  // Also show toasts for non-trivial messages
  if (window.ArtificeToast && tag && message.length > 3) {
    var tone = tag === "accent" ? "success" : (tag || "info");
    var duration = tone === "error" ? 0 : 3000;
    window.ArtificeToast.show(message, tone, { duration: duration });
  }
}
