# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Windowed launcher for the OCR Pipeline web frontend.

Mirrors `launch_artifice_ocr.pyw` — same self-healing interpreter search, same
"log and show a dialog rather than vanish silently" discipline, because a
`.pyw` process has no console to reveal a crash on. See that file for why each
piece exists; only the dependency list and the entry point differ here.
"""

import os
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = Path.home() / ".artifice_ocr" / "launcher_web.log"

# Same core deps the pipeline itself needs, plus the web stack. `tkinterdnd2`
# is deliberately absent — the web build has no tkinter drop zone to need it.
REQUIRED = ("fastapi", "uvicorn", "openai", "ollama", "yaml", "fitz")

SENTINEL = "ARTIFICE_OCR_WEB_LAUNCHER_REEXEC"


def _log(message: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(message.rstrip() + "\n")
    except OSError:
        pass


def _show_error(title: str, message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        _log(f"{title}: {message}")


def _missing(names: tuple[str, ...]) -> list[str]:
    import importlib.util

    missing = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def _installed_interpreters() -> list[Path]:
    try:
        result = subprocess.run(
            ["py", "-0p"], capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []

    found = []
    for line in result.stdout.splitlines():
        part = line.strip()
        idx = part.lower().find("c:\\")
        if idx == -1:
            continue
        candidate = Path(part[idx:].strip().strip('"'))
        if candidate.name.lower() in ("python.exe", "pythonw.exe"):
            found.append(candidate)
    return found


def _interpreter_imports(exe: Path, names: tuple[str, ...]) -> bool:
    try:
        result = subprocess.run(
            [str(exe), "-c", f"import {', '.join(names)}"],
            capture_output=True, timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _find_interpreter(names: tuple[str, ...]) -> Path | None:
    here = Path(sys.executable).resolve()
    for exe in _installed_interpreters():
        if exe.resolve() == here:
            continue
        if _interpreter_imports(exe, names):
            windowed = exe.with_name("pythonw.exe")
            return windowed if windowed.exists() else exe
    return None


def _relaunch(exe: Path, extra_args: list[str]) -> int:
    env = dict(os.environ, **{SENTINEL: "1"})
    try:
        subprocess.Popen(
            [str(exe), str(Path(__file__).resolve()), *extra_args],
            cwd=str(ROOT), env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        _show_error("OCR Pipeline (web) failed to start",
                    f"Could not start {exe}\n\n{exc}\n\nLog: {LOG}")
        return 1
    return 0


def main() -> int:
    # The package lives under src/, matching a normal editable install; add it
    # to the path so `artifice_ocr` resolves without a real `pip install -e .`.
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "src"))
    extra_args = sys.argv[1:]

    already_switched = bool(os.environ.get(SENTINEL))

    missing = _missing(REQUIRED)
    if missing:
        if already_switched:
            _show_error(
                "OCR Pipeline (web) — missing dependencies",
                f"This Python cannot run the web build.\n\n"
                f"Interpreter: {sys.executable}\n"
                f"Missing: {', '.join(missing)}\n\n"
                f"Install them with:\n    pip install -e \".[web]\"\n\n"
                f"Log: {LOG}",
            )
            return 1

        replacement = _find_interpreter(REQUIRED)
        if replacement is None:
            _show_error(
                "OCR Pipeline (web) — missing dependencies",
                f"Missing packages: {', '.join(missing)}\n\n"
                f"No installed Python has them. From\n{ROOT}\nrun:\n"
                f"    pip install -e \".[web]\"\n\nLog: {LOG}",
            )
            return 1

        _log(f"Re-launching with {replacement} (missing: {', '.join(missing)})")
        return _relaunch(replacement, extra_args)

    from artifice_ocr.web.server import main as web_main

    sys.argv = [str(Path(__file__)), *extra_args]
    web_main()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        detail = traceback.format_exc()
        _log(detail)
        _show_error(
            "OCR Pipeline (web) failed to start",
            f"{detail.strip().splitlines()[-1]}\n\nFull details in:\n{LOG}",
        )
        sys.exit(1)
