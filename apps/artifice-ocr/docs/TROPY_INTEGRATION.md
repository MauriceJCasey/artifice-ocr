# Tropy Integration

**Status: supported — live read-only `.tpy` browse and official Developer API note write-back.**

The supported round trip is intentionally narrow: browse a live Tropy project,
enqueue its photos, process and edit the OCR results, then attach notes through
Tropy's loopback Developer API. Existing output files and history remain
usable, but the old JSON-LD bridge and direct SQLite write-back are no longer
web or CLI integration paths. Deprecated settings are ignored.


## Overview

```
Tropy project
         │
         ▼
    artifice-ocr ──────────────────────────► OCR pipeline
                                              output directory
         │
         ▼
  "Send to Tropy…" ─────────────────► Tropy local HTTP API (port 2029)
```

The live route is the only supported import path:

| Mode | Module | Writes to `.tpy`? | Feature flag |
|---|---|---|---|
| Live read-only browse | `tropy_db.py` | Never | Settings toggle `tropy_live_browse_enabled` |
| Developer API notes | `tropy_api.py` | Through Tropy | Enable Developer API in Tropy |

Live-browsed photos map to the same `JobItem` pipeline entry and retain their
project, item, page, orientation, and photo identifiers for note write-back.


## Developer API notes ("Send to Tropy…")

Two entry points share the same modal and preview/commit logic:

- **Main → Send to Tropy…** sends the currently selected queue items.
- **History → Send run to Tropy…** sends every eligible document in the
  selected run at once (single-document selection was the original behaviour
  and was a bug — History only sent whichever one row happened to be
  selected).

Only pages processed by artifice-ocr, and imported through Browse Project
(carrying a numeric photo id), are eligible; everything else is reported as
"ineligible" in the preview rather than silently skipped.

The Send to Tropy modal uses only the notes API. Tropy must be
running, the target project must be open, and **Preferences → Developer API**
must be enabled. Artifice OCR discovers stable port 2019 and beta port 2029 (or
uses the custom port in Settings), verifies the open project, checks each photo
and parent item, previews blockers and duplicates, and only then enables commit.
Identical notes are skipped, so retrying after a partial failure is safe.

Checking is one HTTP round trip per photo (more for photos that already carry
notes), so a run of hundreds of pages can take a while if Tropy itself is busy
rendering its own window. The modal shows live elapsed time, a batch larger
than 150 pages asks for confirmation before starting, and closing the modal
cancels an in-flight check rather than leaving it to finish unobserved. A
single photo's failure is recorded and skipped — it no longer discards every
other page's already-checked result.

The preview checks project identity, missing photos, item mismatches, empty
stages, and duplicate notes. Commit rechecks the project and photo immediately
before each API write, and reports written, skipped, and partial-failure
counts.

The former JSON-LD path validator and manifest writer remain only as internal
compatibility helpers for existing output/history data; they are not exposed
as import or export endpoints and are not used by the supported workflow.


## Live Read-Only Browse (`tropy_db.py`)

**Opt-in, feature-flagged.** Enable via the Settings UI toggle
`tropy_live_browse_enabled` (takes effect immediately, no restart required).
The environment variable `ARTIFICE_OCR_TROPY_LIVE_READ=1` also works as a
fallback override for advanced/CI use. When disabled, the browse routes
return 404.

Opens `.tpy` SQLite databases in `file:<path>?mode=ro` — SQLite enforces
read-only at the connection level. A running Tropy instance holds a write
lock; `tropy_db` handles `SQLITE_BUSY` with a clean error message telling the
user to close Tropy and retry. Connections are short-lived and per-query.

### Browse routes

All mounted at `/api/tropy/browse/` when enabled:

| Route | What it returns |
|---|---|
| `POST /projects` | The single `project` row (name, base path) |
| `POST /lists` | All lists except ROOT (`list_id != 0`) |
| `POST /tags` | All tags |
| `POST /items?list_id=N` | Items in a list (recursive via join on `list_items`) |
| `POST /items?tag=name` | Items with a given tag |
| `POST /items` | All non-trashed items |
| `POST /items/{item_id}` | Single item with its photos |
| `POST /enqueue` | Map selected items to `JobItem` and add to queue |

The **Add from Tropy…** modal shows a live-browse tab when the
settings toggle `tropy_live_browse_enabled` is on (or
`ARTIFICE_OCR_TROPY_LIVE_READ=1` is set). Selecting items and clicking
**Add to Queue** enqueues the selected live-browse items directly.


## Output Layout

All output goes into the standard stage-first tree. The unique output key
prevents PDF page collision:

    output/
      raw_ocr/text/Max Hodann KV File Part 1/KV-2-2339_01_p0001.txt
      raw_ocr/json/Max Hodann KV File Part 1/KV-2-2339_01_p0001.json
      cleaned/text/Max Hodann KV File Part 1/KV-2-2339_01_p0001.txt
      translated/…

The key format is `<Item Title>/<file>_p<page:04d>` for PDFs, or
`<Item Title>/<file>` for images. This is the same naming used by `tropy_db`.

`ocr.perform()`, `cleanup.perform()` and `translate.perform()` all accept an
optional `stem` parameter that overrides the filename-derived key and may contain
a subdirectory. `run_ocr_step()` additionally takes `page` to render a single
PDF page rather than all pages of an item.


## The Tropy Archive, As Verified

These are properties of Tropy's own data model, confirmed by the schema
comments in `tropy_db.py`.

**A `.tropy` "managed" project is a directory:**

    ISK Project Primary Sources.tropy/
      project.tpy      SQLite database
      assets/          content-addressed originals, <checksum>.pdf / .jpg

The `project` table holds `base='project'` (paths relative to the bundle) and
`store='assets'`.

Four structural facts shaped the design:

1. **Photos are pages, not files.** A 275-page item is 275 rows sharing one
   `assets/<checksum>.pdf`, differing only by the `page` column. The output
   key's per-page suffix (`_p0001`, `_p0002`, …) prevents all 275 pages from
   writing to the same `.txt` file.

2. **Both path separators occur.** Tropy stores paths as-is; backslash and
   forward-slash both appear in the database. `_resolve_photo_path` in
   `tropy_db` normalises them before resolving.

3. **Lists nest.** A parent list includes everything beneath it via a
   recursive CTE-style join on `list_items`.

4. **Mixed media.** Photos may be PDFs (paginated, one row per page) or images
   (JPEG, PNG, TIFF — unpaginated, one row per file). The `mimetype` column
   distinguishes them.


## What Was Removed

The JSON-LD bridge endpoints (`/api/tropy/import/*`, `/api/tropy/export*`) and
the direct SQLite write-back endpoints (`/api/tropy/writeback/*`) are removed.
The corresponding CLI commands (`tropy-import`, `tropy-export`) and the
`tropy_writeback_enabled` setting are also retired. Existing JSON-LD manifests,
history entries, and output files are left in place for compatibility; they are
not imported, exported, or written back by this application.

`tropy_write.py`, `tropy_bridge.py`, and `tropy_writeback.py` are deleted, not
retained — there is no legacy fallback. All writes go through `tropy_notes.py`
and `tropy_api.py`.


## Known Limits

- **Selections (crops) are not read.** Tropy's `selections` table is not
  queried by the live browse reader.
- **Existing notes are not modified.** The Developer API path creates notes
  and skips identical text; it never updates or deletes existing notes.
- **Missing assets.** iCloud-backed projects may hold placeholder paths rather
  than files. Live browse marks missing photos and reports them before enqueue.
- **Live browse is read-only.** A locked or open project may return `SQLITE_BUSY`;
  the browse error tells the user to close Tropy and retry.
