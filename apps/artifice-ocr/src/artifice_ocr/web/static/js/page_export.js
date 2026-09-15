// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * "Export PAGE…" modal — export the authoritative PAGE XML record for the
 * selected queue rows. Unlike Compile PDF, this is synchronous (no model
 * calls — export_page_xml() just copies already-persisted XML files), so
 * there is no SSE progress stream and no Stage selector: a PAGE document
 * carries raw/cleaned/translated together, not one flattened stage.
 *
 * One selected row downloads its file directly; more than one writes copies
 * into the project's export layout (docs/PRE_PROCESSING_PLAN.md, Commit 2).
 *
 * Loaded after app.js, whose helpers (api, downloadFile) it reuses — same
 * relationship pdf_export.js has.
 */

const pageExportEls = {};
["btn-export-page", "modal-export-page", "page-export-folder",
 "page-export-status", "btn-page-export-start", "btn-page-export-close",
].forEach(id => {
  pageExportEls[id] = document.getElementById(id);
});

let pageExportReturnFocus = null;

function setPageExportStatus(text, cls) {
  pageExportEls["page-export-status"].textContent = text;
  pageExportEls["page-export-status"].className = "dim " + (cls || "");
}

function openPageExport() {
  pageExportReturnFocus = document.activeElement;
  pageExportEls["modal-export-page"].classList.remove("hidden");
  pageExportEls["page-export-folder"].value = window.QueueTab?.outputDirectory() || "output";

  const stems = window.QueueTab?.selectedStems() || [];
  const btn = pageExportEls["btn-page-export-start"];
  if (!stems.length) {
    setPageExportStatus("Select one or more queue rows first.", "warning");
    btn.disabled = true;
    btn.textContent = "Export";
  } else if (stems.length === 1) {
    setPageExportStatus("1 page selected — this will download its PAGE file.", "");
    btn.disabled = false;
    btn.textContent = "Download";
  } else {
    setPageExportStatus(`${stems.length} pages selected — this writes one PAGE XML file per page into the project's exports.`, "");
    btn.disabled = false;
    btn.textContent = "Export";
  }
  focusFirstIn(pageExportEls["modal-export-page"]);
}

function closePageExport() {
  pageExportEls["modal-export-page"].classList.add("hidden");
  pageExportReturnFocus?.focus?.();
}

async function startPageExport() {
  const outputDir = pageExportEls["page-export-folder"].value.trim();
  const stems = window.QueueTab?.selectedStems() || [];
  if (!outputDir || !stems.length) return;

  pageExportEls["btn-page-export-start"].disabled = true;
  setPageExportStatus("Exporting…", "");

  try {
    if (stems.length === 1) {
      await downloadFile(
        `/api/page-export/download?output_dir=${encodeURIComponent(outputDir)}&stem=${encodeURIComponent(stems[0])}`,
        `${stems[0].split("/").pop()}.xml`,
      );
      setPageExportStatus("Downloaded.", "success");
    } else {
      const data = await api("POST", "/api/page-export/start", { output_dir: outputDir, stems });
      const skipped = data.skipped && data.skipped.length
        ? ` (${data.skipped.length} skipped — no PAGE document yet)`
        : "";
      setPageExportStatus(`Exported ${data.exported.length} of ${stems.length} page(s) to ${data.export_dir}${skipped}`, "success");
    }
  } catch (err) {
    setPageExportStatus("Failed: " + err.message, "error");
  } finally {
    pageExportEls["btn-page-export-start"].disabled = false;
  }
}

// ------------------------------------------------------------- event wiring

pageExportEls["btn-export-page"].onclick = openPageExport;
registerModalCloser("modal-export-page", closePageExport);
pageExportEls["btn-page-export-close"].onclick = closePageExport;
pageExportEls["modal-export-page"].querySelector("[data-modal-close]")?.addEventListener("click", closePageExport);
pageExportEls["btn-page-export-start"].onclick = startPageExport;
pageExportEls["modal-export-page"].addEventListener("click", (e) => {
  if (e.target === pageExportEls["modal-export-page"]) closePageExport();
});
