# OCR test fixtures

## `proceedings_usnm_173.*`

*Proceedings of the United States National Museum*, p. 173 — "Notes on the Ornithology of Southern
Texas", 1878. Supplied by the maintainer as a ground-truth pair, 2026-07-29.

| File | What it is |
|---|---|
| `proceedings_usnm_173.jpg` | The scanned page |
| `proceedings_usnm_173.raw.txt` | The page as OCR sees it — original line breaks, original OCR errors |
| `proceedings_usnm_173.groundtruth.txt` | The maintainer's validated transcription: the target |

**`raw.txt` is a reconstruction, not the output of an actual OCR run.** It was transcribed from the
image preserving line breaks, spacing and legible OCR errors, so that the deterministic pre-pass has
a realistic input to work against. If a real OCR run over the JPEG becomes available it should
replace this file, and the differences are worth recording rather than silently overwriting.

### Why this page earns its place in the suite

It contains, in one page, an example of nearly every case the cleanup stage has to distinguish.

**Line-break hyphens that must be rejoined** — three, all continuing lowercase:

- `ascer-` / `tained` → `ascertained`
- `confir-` / `mation` → `confirmation`
- `North-` / `west` → `Northwest`

**Em-dashes that must never be joined** — four. These are `—` (U+2014), not hyphens, and three of
them sit at a line end where a naive rule would treat them as hyphenation:

- `eggs—` / `there were no birds—are`
- `Grande"—not`
- `positive.—T. M. B.`
- `winter.—(DRESSER`

**Genuine OCR misreadings, which no regex can safely repair** — these are the residue that must stay
with the model:

- `fourided` → `founded`
- `Eio` → `Rio` (and note `Rio` appears correctly four words later, so the error is not systematic)

**Doubled spaces after sentence-terminal punctuation**, throughout — period typesetting, and the
thing a whitespace rule must normalise without touching anything else.

**A running head and page number** (`PROCEEDINGS OF UNITED STATES NATIONAL MUSEUM.  173`), which is
not body text and is not currently handled by any stage.

### The trap this fixture exists to catch

`North-` / `west` → `Northwest` and a hypothetical `self-` / `evident` → `self-evident` **both
continue lowercase**. Any rule that decides purely on the case of the character after the break gets
one of them wrong. Deciding correctly needs a wordlist, not a character class — see
`IMPLEMENTATION_PLAN.md`.

Note also that this corpus is **English**, while `prompts/cleanup_prompt.txt` is written around
German transliterated umlauts (`ueber`, `Taetigkeit`). The tool targets both, so the pre-pass must
not assume either language.
