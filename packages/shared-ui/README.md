# shared-ui

Canonical design tokens for The New Masses design system (see
`Design_Philosophy.md` at the repo root). `tokens.css` is the source of
truth for the `:root` custom-property block; `artifice-ocr`,
`artifice-draft`, and `artifice-transcribe` each currently declare an
identical copy of these tokens at the top of their own `static/css/app.css`.
Consolidating those three call sites to `@import` or `<link>` this file
instead of redeclaring the literals is the intended next step, not done as
part of the directory reorganization.

`artifice-graph` was consolidated into this file first (2026-07): its
app-local `tokens.css` had accumulated 30 tokens (shadows, radius, spacing,
motion, layout, `--font-mono`) that shared-ui was simply missing, plus the
`@media (prefers-color-scheme: dark)` block — without which this file could
only respond to an explicit `[data-theme]` and never follow the OS. It also
carried one naming drift (`--font-sans` → renamed to the already-canonical
`--font-label`) and two dead/out-of-scope token families that were removed
or relocated rather than promoted:

- `--w-*` ("Word-Status Colours (Reading View)") — deleted outright.
  ArtificeGraph has no reading view; this was copy-paste residue.
- `--reg-*` (register taxonomy) and `--type-*` (entity-type accents) —
  ArtificeGraph domain vocabulary, not suite-wide identity. These now live
  in `apps/artifice-graph/web/static/entity-colors.css`, loaded after this
  file.

`artifice-graph` no longer keeps a copy at all. Its `web/static/tokens.css`
was deleted; `web/server.py` resolves assets through the installed package
via `importlib.resources`, so the app serves *this* file rather than a mirror
of it. There is nothing to keep in sync, and no per-app `tokens.css` should
be reintroduced.

```python
# apps/artifice-graph/src/artifice_graph/web/server.py
import importlib.resources
import shared_ui

_SHARED_UI = importlib.resources.files(shared_ui) / "assets"
app.mount("/shared", StaticFiles(directory=str(_SHARED_UI)), name="shared")
```

```html
<link rel="stylesheet" href="/shared/tokens.css?v={{ asset_v }}" />
```

Load this before any stylesheet that consumes the variables, and before an
app's own domain colours (e.g. `entity-colors.css`).

Adopt the same mount when consolidating the remaining three apps.

## Fonts (2026-07)

`shared_ui/assets/fonts/` and `shared_ui/assets/fonts.css` vendor the three type families named in
`Design_Philosophy.md` §3 (Playfair Display, Libre Baskerville, Archivo) so
that no app fetches them from `fonts.googleapis.com` / `fonts.gstatic.com`
at runtime — a hard requirement of the suite's local-first, offline
guarantee. `fonts.css` declares `@font-face` rules with `url()`s relative
to `fonts/`; it is a sibling file to `tokens.css` in `shared_ui/assets/`
rather than folded into it, because it performs asset loading (network/file
fetches) while `tokens.css` only declares custom properties — different
concern, kept in a different file so each can be reasoned about (and diffed)
independently.

`shared_ui/assets/fonts/PlayfairDisplay(.ttf|-Italic.ttf)` and
`shared_ui/assets/fonts/LibreBaskerville(.ttf|-Italic.ttf)` are copies of the files already
vendored at `apps/artifice-ocr/assets/fonts/` — copied, not moved, since
that app's own asset pipeline may reference its copy. Both are variable
fonts (a single file's `wght` axis spans the full weight range each family
needs), so `fonts.css` uses one `@font-face` rule per style with a
`font-weight` range rather than one rule per static weight.

`shared_ui/assets/fonts/Archivo.woff2` was fetched fresh (the OCR app does not carry
Archivo) directly from Google's CDN, already in woff2 format.

**Format note:** woff2 is preferred (roughly half the size of TTF, and
what every supported browser wants), but only Archivo ships as woff2.
Converting the two variable TTFs to woff2 requires `fonttools`' woff2
support, which itself requires the `brotli` Python extension — neither
`brotli` nor a standalone `woff2_compress` binary was available in this
environment, and per project instruction no conversion tooling was
installed to manufacture one. Playfair Display and Libre Baskerville
therefore ship as TTF. Revisit if `brotli` becomes available (e.g. as a
project dependency for another reason) — conversion is then a single
`fonttools ttLib.woff2 compress` call per file.

**Licence:** all three families are SIL Open Font License 1.1. The licence
text is vendored per-family — `shared_ui/assets/fonts/OFL-Archivo.txt`,
`shared_ui/assets/fonts/OFL-PlayfairDisplay.txt`,
`shared_ui/assets/fonts/OFL-LibreBaskerville.txt` — rather
than merged into one file, because each carries a distinct copyright
holder in its header and this repository is heading toward a Zenodo DOI /
JOSS review where that attribution needs to stay traceable to its source.
