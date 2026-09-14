# Contributing to Artifice OCR

Thanks for considering a contribution. This project follows the
[Contributor Covenant](CODE_OF_CONDUCT.md); by participating you agree to
abide by it.

## Getting set up

This repo is a small [uv](https://docs.astral.sh/uv/) workspace: the app
itself (`apps/artifice-ocr`) plus the shared support packages it depends on
(`packages/model-harness`, `packages/output-layout`, `packages/secure-io`,
`packages/shared-ui`).

```bash
git clone https://github.com/MauriceJCasey/artifice-ocr.git
cd artifice-ocr
uv sync --extra web
uv run artifice-ocr-web
```

## Running tests

```bash
uv sync --group dev --all-extras
uv run pytest
```

Please add or update tests for any behavioral change, and confirm the suite
still passes before opening a PR.

## Line endings

The repository's line-ending policy lives in [`.gitattributes`](.gitattributes)
at the repo root, and it is the source of truth — not each machine's
`core.autocrlf`. The default rule is `* text=auto eol=lf`: text files are
stored as LF in the repository **and** checked out as LF in the working tree
on every platform, so the same commit reads as clean everywhere.

Set this once on every machine you develop from:

```bash
git config --global core.autocrlf false
```

Do **not** re-enable `core.autocrlf=true` — that is Windows git's default and
defeats the point of the explicit `eol=` rules in `.gitattributes`.

## Project conventions

1. **Structured model interactions only.** Any new feature or model connector
   must go through `packages/model-harness`'s schema-validated call shape,
   not a freeform chat wrapper. Model output is structured data, not
   conversation.
2. **Local-first, no _silent_ network calls.** The rule is not "never touch
   the network" — this app talks to cloud models when the user asks it to.
   The rule is that the user is never surprised. Every outbound request falls
   into exactly one of three tiers:

   | Tier | Rule | Examples |
   |---|---|---|
   | **Never** | Application assets and anything the user did not ask for | Web fonts, JS libraries, telemetry, analytics, update checks |
   | **Only on explicit user action, disclosed before the action** | The user clicks something, having been told what it will contact | — |
   | **User's own credentials, user's own endpoint** | BYOM — the user supplied the key and chose the host | Ollama, LM Studio, or any OpenAI-compatible API |

   Never transmit user documents, transcriptions, or API keys anywhere the
   user did not explicitly direct.

## JavaScript: vanilla, no build step

No framework, no bundler. Modern syntax (`let`, `const`, arrow functions,
classes) is fine — all of it runs natively in every browser this project
targets. The point is that `git clone` + `uv sync` produces working software
on a machine with no Node toolchain at all, and that the source you read is
the code that runs.

## Submitting a pull request

- Describe what changed and why in the PR description; link any relevant
  issue.
- Make sure `pytest` passes.
- If your change affects the Dockerfile, confirm `docker build` succeeds.

## License

Artifice OCR is licensed under the GNU Affero General Public License v3.0 or
later. By contributing you agree to license your contribution under the same
terms. See [LICENSE](LICENSE) for the full text.

Third-party assets (bundled fonts) carry their own licenses; check
[`REUSE.toml`](REUSE.toml) before reusing them.

## Reporting bugs or requesting features

Open a GitHub issue with enough detail to reproduce the problem (OS, Python
version, model backend if relevant) or to understand the requested feature's
motivation.
