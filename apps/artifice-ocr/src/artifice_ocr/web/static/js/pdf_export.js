// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * "Compile PDF…" modal — one-off PDF export of a folder of processed .txt
 * files. Uses SSE for live progress instead of a synchronous fetch (the
 * structuring pass calls the LLM once per page; blocking for 275 pages
 * would hang the browser tab).
 *
 * Loaded after app.js, whose helpers (api, escapeHtml, pickFolder, log) it
 * reuses rather than duplicating — the same relationship tropy.js has.
 */

const pdfEls = {};
["btn-compile-pdf", "modal-compile-pdf", "pdf-folder", "pdf-stage",
 "pdf-structure", "pdf-output", "pdf-status", "pdf-log",
 "btn-pdf-start", "btn-pdf-download", "btn-pdf-close", "btn-pdf-cancel",
 "btn-pdf-browse-folder", "btn-pdf-browse-output",
 "pdf-format", "pdf-style", "pdf-bilingual", "pdf-bilingual-hint",
].forEach(id => {
  pdfEls[id] = document.getElementById(id);
});

let pdfEventSource = null;

// ------------------------------------------------------------- open / close

function openPdfExport() {
  pdfEls["modal-compile-pdf"].classList.remove("hidden");
  pdfEls["pdf-log"].innerHTML = "";
  pdfEls["btn-pdf-download"].disabled = true;
  pdfEls["btn-pdf-start"].disabled = false;
  pdfEls["btn-pdf-cancel"].disabled = true;
  setPdfStatus("Choose the saved text stage, then press Start.", "");

  // Default the input to the pipeline's output ROOT, not a per-item leaf.
  // The backend reads <folder>/<stage>/text/*.txt, so the output root is
  // always a valid input and the Stage selector below decides which text is
  // used. The previous default appended "/cleaned/text/<item name>", which is
  // never a real directory of .txt files (text is written as <stem>.txt files
  // directly in cleaned/text/, not in a per-item subfolder) — so it produced
  // "No pages found" every time and contradicted the Stage selector.
  const outputDir = window.QueueTab?.outputDirectory() || "output";
  pdfEls["pdf-folder"].value = outputDir;
  pdfEls["pdf-stage"].value = window.QueueTab?.preferredStage() || "raw_ocr";
  pdfEls["pdf-output"].value = "";
  refreshPdfPreview();
}

function closePdfExport() {
  if (pdfEventSource) { pdfEventSource.close(); pdfEventSource = null; }
  pdfEls["modal-compile-pdf"].classList.add("hidden");
}

function setPdfStatus(text, cls) {
  pdfEls["pdf-status"].textContent = text;
  pdfEls["pdf-status"].className = "dim " + (cls || "");
}

function appendPdfLog(text) {
  const line = document.createElement("div");
  line.className = "line";
  line.textContent = text;
  pdfEls["pdf-log"].appendChild(line);
  pdfEls["pdf-log"].scrollTop = pdfEls["pdf-log"].scrollHeight;
}

let pdfPreviewTimer = null;
async function refreshPdfPreview() {
  const folder = pdfEls["pdf-folder"].value.trim();
  if (!folder) return;
  clearTimeout(pdfPreviewTimer);
  pdfPreviewTimer = setTimeout(async () => {
    try {
      const params = new URLSearchParams({
        folder,
        stage: pdfEls["pdf-stage"].value,
        bilingual: String(pdfEls["pdf-bilingual"].checked),
      });
      const data = await api("GET", "/api/pdf-export/preview?" + params.toString());
      const warning = data.warnings && data.warnings.length ? " — " + data.warnings.join("; ") : "";
      setPdfStatus(`${data.pages} page${data.pages === 1 ? "" : "s"} ready${warning}`, warning ? "warning" : "");
    } catch (_) {
      setPdfStatus("Choose a valid output folder to preview pages.", "warning");
    }
  }, 250);
}

// -------------------------------------------------------- browse / download

async function browsePdfFolder() {
  const dir = await pickFolder("folder");
  if (dir) pdfEls["pdf-folder"].value = dir;
}

async function browsePdfOutput() {
  let data;
  try {
    data = await api("POST", "/api/native/save-file", {
      preset: "all",
      default_name: "output.pdf",
    });
  } catch {
    if (window.ArtificeToast) window.ArtificeToast.error("Could not reach the server to open the save dialog.");
    return;
  }
  if (data.state === "selected" && data.paths && data.paths.length) {
    pdfEls["pdf-output"].value = data.paths[0];
    return;
  }
  if (data.state === "unavailable") {
    if (window.ArtificeToast) window.ArtificeToast.show(data.reason || "Save dialog unavailable", "warning");
    const path = prompt("Enter where to save the PDF (e.g. C:\\Users\\you\\Documents\\output.pdf):");
    if (path) pdfEls["pdf-output"].value = path;
    return;
  }
  // cancelled — do nothing
}

// --------------------------------------------------------- start / download

async function startPdfExport() {
  const folder = pdfEls["pdf-folder"].value.trim();
  if (!folder) { setPdfStatus("Choose a folder first.", "warning"); return; }

  pdfEls["btn-pdf-start"].disabled = true;
  pdfEls["btn-pdf-cancel"].disabled = false;
  pdfEls["btn-pdf-download"].disabled = true;
  pdfEls["pdf-log"].innerHTML = "";
  setPdfStatus("Starting…", "");

  try {
    await api("POST", "/api/pdf-export/start", {
      folder: folder,
      stage: pdfEls["pdf-stage"].value,
      structure: pdfEls["pdf-structure"].checked,
      output: pdfEls["pdf-output"].value.trim() || null,
      format: pdfEls["pdf-format"].value,
      style: pdfEls["pdf-style"].value,
      bilingual: pdfEls["pdf-bilingual"].checked,
    });
  } catch (err) {
    setPdfStatus("Failed to start: " + err.message, "error");
    pdfEls["btn-pdf-start"].disabled = false;
    return;
  }

  pdfEventSource = new EventSource("/api/pdf-export/events");
  pdfEventSource.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch (_) { return; }
    if (data.type === "log") {
      appendPdfLog(data.message);
    } else if (data.type === "done") {
      appendPdfLog("Done: " + data.output_path);
      setPdfStatus(`${pdfEls["pdf-format"].value.toUpperCase()} compiled successfully.`, "success");
      pdfEls["btn-pdf-download"].disabled = false;
      pdfEls["btn-pdf-start"].disabled = false;
      pdfEls["btn-pdf-cancel"].disabled = true;
      pdfEventSource.close();
      pdfEventSource = null;
    } else if (data.type === "error") {
      appendPdfLog("ERROR: " + data.message);
      setPdfStatus("Error: " + data.message, "error");
      pdfEls["btn-pdf-start"].disabled = false;
      pdfEls["btn-pdf-cancel"].disabled = true;
      pdfEventSource.close();
      pdfEventSource = null;
    }
  };
  pdfEventSource.onerror = () => {
    // EventSource auto-reconnects; only surface a real error after
    // the stream should have ended.
  };
}

async function downloadPdf() {
  if (pdfEls["btn-pdf-download"].disabled) return;
  try {
    await downloadFile("/api/pdf-export/download", "export.pdf");
  } catch (err) {
    if (window.ArtificeToast) window.ArtificeToast.error(`Could not download: ${err.message}`);
  }
}

// ------------------------------------------------------------- event wiring

pdfEls["btn-compile-pdf"].onclick = openPdfExport;
pdfEls["btn-pdf-close"].onclick = closePdfExport;
pdfEls["modal-compile-pdf"].querySelector("[data-modal-close]")?.addEventListener("click", closePdfExport);
pdfEls["btn-pdf-start"].onclick = startPdfExport;
pdfEls["btn-pdf-cancel"].onclick = async () => {
  await api("POST", "/api/pdf-export/cancel").catch((err) => {
    if (window.ArtificeToast) window.ArtificeToast.error(`Could not cancel export: ${err.message}`);
  });
  setPdfStatus("Cancelling…", "warning");
};
pdfEls["btn-pdf-download"].onclick = downloadPdf;
pdfEls["btn-pdf-browse-folder"].onclick = browsePdfFolder;
pdfEls["btn-pdf-browse-output"].onclick = browsePdfOutput;
pdfEls["pdf-folder"].addEventListener("change", refreshPdfPreview);
pdfEls["pdf-stage"].addEventListener("change", refreshPdfPreview);
pdfEls["modal-compile-pdf"].addEventListener("click", (e) => {
  if (e.target === pdfEls["modal-compile-pdf"]) closePdfExport();
});

// Bilingual mode: disable stage selector, show hint, default no-structure
pdfEls["pdf-bilingual"].addEventListener("change", (e) => {
  const on = e.target.checked;
  pdfEls["pdf-bilingual-hint"].style.display = on ? "block" : "none";
  pdfEls["pdf-stage"].disabled = on;
  if (on) {
    pdfEls["pdf-structure"].checked = false;
  }
  refreshPdfPreview();
});
