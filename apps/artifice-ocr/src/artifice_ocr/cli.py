# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

import typer

from artifice_ocr._logging import get_logger, setup_logging
from artifice_ocr._resolution import resolve_models_for_run
from artifice_ocr.config import _USER_DIR
from artifice_ocr.config import get as cfg
from artifice_ocr.output import record_dir
from artifice_ocr.stages import cleanup as cleanup_stage
from artifice_ocr.stages import ocr as ocr_stage
from artifice_ocr.stages import translate as translate_stage
from artifice_ocr.utils import check_lm_studio, check_ollama

log = get_logger("cli")

app = typer.Typer()


@app.callback(invoke_without_command=True)
def main(
    data_dir: bool = typer.Option(
        False,
        "--data-dir",
        help="Print the absolute path of the user-data directory and exit.",
    ),
):
    if data_dir:
        print(str(_USER_DIR.resolve()))
        raise typer.Exit()


@app.command("data-dir")
def data_dir():
    """Print the absolute path of the user-data directory and exit."""
    print(str(_USER_DIR.resolve()))


def _ollama_models_needed() -> list[str]:
    models: list[str] = []
    cleanup_model = cfg("cleanup_model")
    if cleanup_model:
        models.append(cleanup_model)
    if cfg("translate_enabled"):
        translate_model = cfg("translate_model")
        if translate_model:
            models.append(translate_model)
    return models


def _resolve_or_exit(stages: set[str]) -> None:
    """Resolve the models/backends for *stages*, exiting with a legible error.

    Resolution probes the configured local servers once and fails fast when a
    role cannot be resolved (no suitable model installed, or the user's
    explicit model is missing) — instead of reaching the provider and printing
    a raw 404.
    """
    try:
        resolve_models_for_run(stages=stages)
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from None


@app.command()
def preflight():
    """Run a full-stack health check (LM Studio + Ollama + models)."""
    setup_logging(level=0)  # quiet for preflight table

    typer.echo("Pre-flight check")
    typer.echo("-" * 50)

    # LM Studio
    lm_err = check_lm_studio()
    if lm_err:
        typer.echo(f"  LM Studio:  FAIL  ({lm_err})")
    else:
        typer.echo(f"  LM Studio:  OK    ({cfg('lm_studio_url')})")

    # Ollama
    ollama_errors = check_ollama()
    if ollama_errors:
        typer.echo(f"  Ollama:     FAIL  ({ollama_errors[0]})")
    else:
        typer.echo("  Ollama:     OK")

    # Models
    models_needed = _ollama_models_needed()
    model_errors = check_ollama(models_needed)
    for model in models_needed:
        err = next((e for e in model_errors if model in e), None)
        if err:
            typer.echo(f"  Model:      FAIL  {model}  ({err})")
        else:
            typer.echo(f"  Model:      OK    {model}")

    # Config summary
    typer.echo("-" * 50)
    typer.echo(f"  OCR model:        {cfg('ocr_model')}")
    typer.echo(f"  Cleanup model:    {cfg('cleanup_model')}")
    typer.echo(f"  Translate model:  {cfg('translate_model')}")
    typer.echo(f"  Resume:           {cfg('resume')}")
    typer.echo(f"  Max OCR workers:  {cfg('max_ocr_workers')}")
    typer.echo(f"  Document type:    {cfg('document_type')}")
    typer.echo(f"  Confidence:       {'enabled' if cfg('confidence_enabled') else 'disabled'}")
    typer.echo(f"  Chunk max tokens: {cfg('chunk_max_tokens')}")

    has_failure = bool(lm_err) or bool(ollama_errors) or bool(model_errors)
    if has_failure:
        typer.echo("\nSome checks FAILED — pipeline may not run correctly.")
        raise typer.Exit(code=1)
    else:
        typer.echo("\nAll checks passed.")


@app.command()
def ocr(
    input_path: str,
    output_dir: str = typer.Option("output", help="Output directory"),
):
    """Run the OCR stage on a given input file."""
    _resolve_or_exit({"ocr"})

    typer.echo(f"Processing {input_path}...")
    result = ocr_stage.perform(input_path, output_dir=output_dir)
    typer.echo(f"Extracted {len(result['extracted_text'])} characters.")
    typer.echo(f"Text written to {output_dir}/raw_ocr/text/")
    typer.echo(f"JSON written to {output_dir}/raw_ocr/json/")


@app.command()
def cleanup(
    text_path: str = typer.Argument(help="Path to raw OCR text file"),
    output_dir: str = typer.Option("output", help="Output directory"),
):
    """Run conservative syntactic cleanup on a raw OCR text file."""
    _resolve_or_exit({"cleanup"})

    p = Path(text_path)
    if not p.exists():
        raise typer.BadParameter(f"File not found: {text_path}")
    raw_text = p.read_text(encoding="utf-8")

    result = cleanup_stage.perform(
        raw_text,
        source_file=str(p),
        output_dir=output_dir,
    )
    typer.echo(f"Cleaned {len(result['cleaned_text'])} characters (was {len(result['raw_text'])}).")
    typer.echo(f"Text written to {output_dir}/cleaned/text/")
    typer.echo(f"JSON written to {output_dir}/cleaned/json/")


@app.command()
def translate(
    text_path: str = typer.Argument(help="Path to cleaned text file"),
    output_dir: str = typer.Option("output", help="Output directory"),
):
    """Translate a cleaned text file into English."""
    _resolve_or_exit({"translate"})

    p = Path(text_path)
    if not p.exists():
        raise typer.BadParameter(f"File not found: {text_path}")
    cleaned_text = p.read_text(encoding="utf-8")

    result = translate_stage.perform(
        cleaned_text,
        source_file=str(p),
        output_dir=output_dir,
    )
    typer.echo(
        f"Translated {len(result['translated_text'])} characters"
        f" (was {len(result['cleaned_text'])})."
    )
    typer.echo(f"Text written to {output_dir}/translated/text/")
    typer.echo(f"JSON written to {output_dir}/translated/json/")


@app.command("audit-translations")
def audit_translations(
    output_dir: str = typer.Option("output", help="Output directory to scan"),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table"
    ),
):
    """Find already-produced translations likely corrupted by the
    already-English mistranslation bug.

    Before the fix, an English-source document sent through the translate
    stage got "helpfully" reworded/rewritten by a model asked to translate
    text that had nothing to translate — rather than being left untouched.
    This scans every `<output_dir>/translated/json/*.json` (recursively, so
    Tropy-item subfolders are covered) and reports every one whose
    `source_language` is "en" but which has no `skipped_translation` marker
    — i.e. it was translated for real under the old, buggy behaviour, and
    the text on disk may have been altered. Those need a forced re-run
    (`--force`) to regenerate; resuming a normal run will just reuse the
    existing, possibly-corrupted output.
    """
    import json as json_module

    json_dir = record_dir(output_dir, "translated")
    if not json_dir.exists():
        json_dir = Path(output_dir) / "translated" / "json"
    if not json_dir.exists():
        typer.echo(f"No translated output found at {json_dir}")
        raise typer.Exit(code=0)

    affected: list[dict] = []
    total = 0
    for json_file in sorted(json_dir.rglob("*.json")):
        total += 1
        try:
            data = json_module.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("source_language") == "en" and not data.get("skipped_translation"):
            affected.append(
                {
                    "stem": str(json_file.relative_to(json_dir).with_suffix("")),
                    "source_file": data.get("source_file", ""),
                    "json_path": str(json_file),
                }
            )

    if as_json:
        typer.echo(json_module.dumps(affected, indent=2))
        return

    typer.echo(f"Scanned {total} translated document(s) under {json_dir}")
    if not affected:
        typer.echo("None affected — nothing to re-run.")
        return

    typer.echo(f"\n{len(affected)} likely affected (English source, translated for real):\n")
    for entry in affected:
        typer.echo(f"  {entry['stem']}")
        if entry["source_file"]:
            typer.echo(f"      source: {entry['source_file']}")

    typer.echo(
        '\nRe-run these with --force (or the GUI/web "Force re-run" option) '
        "so the fixed translate stage regenerates them instead of reusing "
        "the existing output."
    )


@app.command()
def pipeline(
    input_path: str = typer.Argument(help="Path to image file or directory of images"),
    output_dir: str = typer.Option("output", help="Output directory"),
    skip_ocr: bool = typer.Option(False, "--skip-ocr", help="Skip the OCR stage"),
    skip_cleanup: bool = typer.Option(False, "--skip-cleanup", help="Skip the cleanup stage"),
    skip_translate: bool = typer.Option(
        False, "--skip-translate", help="Skip the translation stage"
    ),
    force: bool = typer.Option(False, "--force", help="Re-process even if outputs exist"),
    document_type: str = typer.Option(
        "default",
        "--doc-type",
        help="Document type (default, handwritten, typed_clean,"
        " technical, formal, casual, multi_lang)",
    ),
    no_confidence: bool = typer.Option(False, "--no-confidence", help="Disable confidence scoring"),
):
    """Run the full pipeline: OCR -> Cleanup -> Translate.

    Accepts a single image file or a directory. When given a directory,
    all supported images inside it are processed as a batch.
    """
    from artifice_ocr import config
    from artifice_ocr.pipeline import run_pipeline

    # Apply CLI overrides to config
    config.apply_overrides(
        {
            "document_type": document_type,
            "confidence_enabled": not no_confidence,
        }
    )

    # Resolve the models/backends for the stages that will actually run.
    # Fails fast with a legible message instead of a provider 404.
    stages: set[str] = set()
    if not skip_ocr:
        stages.add("ocr")
    if not skip_cleanup or cfg("title_enabled"):
        stages.add("cleanup")  # the "chat" role (cleanup + title share it)
    if not skip_translate:
        stages.add("translate")
    _resolve_or_exit(stages)

    p = Path(input_path)
    if p.is_dir():
        from artifice_ocr.stages.ocr import SUPPORTED_EXTENSIONS

        files = sorted(
            f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        if not files:
            typer.echo(f"No supported files in {input_path}", err=True)
            raise typer.Exit(code=1)

        typer.echo(f"Running batch pipeline for {len(files)} file(s)...")
        result = run_pipeline(
            input_path,
            output_dir=output_dir,
            skip_translate=skip_translate,
            skip_cleanup=skip_cleanup,
            skip_ocr=skip_ocr,
            force=force,
        )

        _print_batch_summary(result, output_dir)
    else:
        stages_done = []
        if not skip_ocr:
            stages_done.append("raw")
        if not skip_cleanup:
            stages_done.append("cleaned")
        if not skip_translate:
            stages_done.append("translated")

        typer.echo(f"Running pipeline for {input_path} ({', '.join(stages_done)})...")
        result = run_pipeline(
            input_path,
            output_dir=output_dir,
            skip_translate=skip_translate,
            skip_cleanup=skip_cleanup,
            skip_ocr=skip_ocr,
            force=force,
        )

        typer.echo("Pipeline complete.")
        if not skip_ocr:
            typer.echo(f"  OCR:       {len(result['raw']['extracted_text'])} chars")
        if not skip_cleanup:
            typer.echo(f"  Cleanup:   {len(result['cleaned']['cleaned_text'])} chars")
        if "translated" in result:
            typer.echo(f"  Translate: {len(result['translated']['translated_text'])} chars")
        typer.echo(f"Output: {output_dir}/")


@app.command("compile-pdf")
def compile_pdf(
    folder: str = typer.Argument(help="Folder of processed .txt output"),
    stage: str = typer.Option("cleaned", "--stage", help="cleaned|raw_ocr|translated"),
    output: str = typer.Option(None, "--output", help="Output PDF/MD path"),
    structure: bool = typer.Option(
        None,
        "--structure/--no-structure",
        help="Apply structuring pass (bilingual defaults to off)",
    ),
    manifest: str = typer.Option(None, "--manifest", help="Explicit tropy_manifest.json path"),
    format: str = typer.Option("pdf", "--format", help="Output format: pdf or md"),
    style: str = typer.Option(
        "readable",
        "--style",
        help="PDF style preset: readable, academic, compact",
    ),
    bilingual: bool = typer.Option(
        False,
        "--bilingual",
        help="Two-column original + translation (uses cleaned + translated stages)",
    ),
):
    """Compile processed text files into a single readable PDF or Markdown file.

    Takes a folder of already-processed .txt output (cleaned, raw_ocr, or
    translated) and produces one continuous-flow reading document.

    --bilingual pairs cleaned/ and translated/ text into two-column output.
    Missing translations produce blank right columns.  Structure pass is
    skipped by default for bilingual mode (pass --structure to opt in).

    Examples:

        artifice_ocr compile-pdf "output/cleaned/text/Fritz Eberhard KV" --no-structure

        artifice_ocr compile-pdf "output/cleaned/text/ISK Comms" --output isk.pdf

        artifice_ocr compile-pdf output/ --bilingual --format pdf
    """
    from artifice_ocr import pdf_export

    folder_path = Path(folder)
    if not folder_path.exists():
        raise typer.BadParameter(f"Folder not found: {folder}")

    structure_flag = not bilingual if structure is None else structure

    try:
        result_path = pdf_export.compile(
            folder,
            stage=stage,
            structure=structure_flag,
            output=output,
            manifest_path=manifest,
            on_progress=lambda msg: typer.echo(msg),
            format=format,
            style=style,
            bilingual=bilingual,
        )
    except (ValueError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Output: {result_path}")


def _print_batch_summary(result: dict, output_dir: str):
    """Print a detailed summary table for batch processing."""
    files = result.get("files", {})
    timings = result.get("timings", {})
    batch_elapsed = result.get("batch_elapsed", 0)
    batch_size = result.get("batch_size", len(files))

    typer.echo(f"\n{'=' * 60}")
    typer.echo(f"  BATCH SUMMARY — {batch_size} file(s)  ({batch_elapsed:.1f}s)")
    typer.echo(f"{'=' * 60}")

    for fpath, data in files.items():
        name = Path(fpath).name
        ocr_chars = len(data["raw"]["extracted_text"])
        clean_chars = len(data["cleaned"]["cleaned_text"])
        skip_marker = " [skip]" if data.get("raw", {}).get("_skipped") else ""

        line = f"  {name}{skip_marker}"
        line += f"  OCR:{ocr_chars}  Clean:{clean_chars}"

        if "translated" in data:
            trans_chars = len(data["translated"]["translated_text"])
            lang = data["translated"].get("source_language_name", "?")
            line += f"  Trans:{trans_chars} ({lang})"
            conf = data["translated"].get("confidence", {}).get("overall_score")
            if conf is not None:
                line += f"  conf:{conf}/100"

        file_t = timings.get(fpath, {})
        parts = []
        if "ocr" in file_t:
            parts.append(f"ocr={file_t['ocr']:.1f}s")
        if "cleanup" in file_t:
            parts.append(f"clean={file_t['cleanup']:.1f}s")
        if "translate" in file_t:
            parts.append(f"trans={file_t['translate']:.1f}s")
        if parts:
            line += f"  [{', '.join(parts)}]"

        typer.echo(line)

    typer.echo(f"{'=' * 60}")
    typer.echo(f"Output: {output_dir}/")


if __name__ == "__main__":
    app()
