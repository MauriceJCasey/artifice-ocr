#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

#
# install.sh --- standalone installer for Artifice OCR.
#
# Installs the latest published version of `artifice-ocr` (with the `[web]`
# and `[window]` extras) as a uv-managed tool, creates a desktop shortcut,
# and prints the commands to start your local model runners (LM Studio,
# Ollama).
#
# The `[window]` extra pulls in pywebview so that `artifice-ocr-web` can
# open a native desktop window (WebView2 on Windows, WKWebView on macOS,
# WebKitGTK on Linux). On headless or display-less sessions the app
# gracefully falls back to opening the URL in a browser.
#
# Usage:
#   bash install.sh            # full install with native-window support
#   bash install.sh --web-only  # skip pywebview, browser-only launch
#
# After installing, the package is runnable from:
#   artifice-ocr       → CLI pipeline
#   artifice-ocr-web   → Web UI (native window where available)
#
# Requires Python 3.11+ (uv will install one automatically if needed).

set -euo pipefail

# --- argument parsing ---------------------------------------------------------

WEB_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --web-only) WEB_ONLY=true ;;
        -h|--help)
            head -20 "$0"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

# --- helpers ------------------------------------------------------------------

die() { printf '\nERROR: %s\n' "$*" >&2; printf '\nPress Enter to exit.\n' >&2; read -r; exit 1; }
info() { printf '  %s\n' "$*"; }
banner() { printf '\n--- %s ---\n' "$*"; }

# --- pre-flight validation ----------------------------------------------------

if ! curl -fsSL --connect-timeout 5 --max-time 10 https://astral.sh/ >/dev/null 2>&1; then
    die "No network connection to https://astral.sh. Check your internet connection and try again."
fi

# --- ensure uv is available (pinned checksum verification) --------------------
#
# The uv installer is fetched from a version-pinned URL, so its bytes are
# immutable and the checksum below changes only when UV_VERSION is bumped.
# When bumping, change UV_VERSION and EXPECTED_HASH together and re-verify
# the hash against the pinned release before committing.

UV_VERSION="0.12.5"
UV_INSTALLER_URL="https://astral.sh/uv/${UV_VERSION}/install.sh"
EXPECTED_HASH="504511fbbbd811aeaba6738abc79408956b6c7da0ca35437b3dcc24a41efc111"

if ! command -v uv &>/dev/null; then
    banner "Installing uv (Python package manager)"

    uv_installer="$(mktemp /tmp/uv-install.XXXXXX.sh)"
    trap 'rm -f "$uv_installer"' EXIT

    info "Downloading uv installer ..."
    if ! curl -fsSL "$UV_INSTALLER_URL" -o "$uv_installer"; then
        rm -f "$uv_installer"
        die "Failed to download uv installer from $UV_INSTALLER_URL"
    fi

    info "Verifying checksum ..."
    actual_hash=$(sha256sum "$uv_installer" | awk '{print $1}')

    if [ "$actual_hash" != "$EXPECTED_HASH" ]; then
        rm -f "$uv_installer"
        die "SHA256 checksum MISMATCH for the uv installer.
  Expected: $EXPECTED_HASH
  Actual:   $actual_hash

This could mean the upstream installer has changed or the download was
tampered with. The installer was NOT executed.

If you are the maintainer:
  1. Verify the new checksum against a trusted source.
  2. Update the EXPECTED_HASH constant in this script.
  3. Re-run this installer."
    fi

    info "Checksum verified. Running installer ..."
    sh "$uv_installer" || {
        rm -f "$uv_installer"
        die "uv installer exited with a non-zero status. uv was not installed."
    }

    rm -f "$uv_installer"

    # uv installs itself to ~/.local/bin (honouring XDG_BIN_HOME), not
    # ~/.cargo/bin --- that path is a legacy of uv's old cargo-based
    # installer.  The profile update only takes effect in new sessions, so
    # prepend both directories now for the current one: the modern location
    # first, ~/.cargo/bin retained as a fallback for older existing installs.
    export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"

    if ! command -v uv &>/dev/null; then
        die "uv was installed but is not on PATH. Try restarting your terminal and running this script again."
    fi
fi

info "uv $(uv --version 2>&1) found."

# --- platform detection -------------------------------------------------------

os_type="$(uname -s)"
is_wsl=false
is_apple_silicon=false

if [ "$os_type" = "Darwin" ]; then
    is_apple_silicon=true
elif grep -qi microsoft /proc/version 2>/dev/null; then
    is_wsl=true
    is_apple_silicon=false
fi

if [ "$is_wsl" = true ]; then
    echo ""
    echo "  Detected: WSL2 (Windows Subsystem for Linux)"
elif [ "$is_apple_silicon" = true ]; then
    echo ""
    echo "  Detected: macOS"
elif [ "$os_type" = "Linux" ]; then
    echo ""
    echo "  Detected: Linux"
fi

# --- install artifice-ocr ------------------------------------------------------

if [ "$WEB_ONLY" = true ]; then
    extra_spec="artifice-ocr[web]"
else
    extra_spec="artifice-ocr[web,window]"
fi

banner "Installing $extra_spec from PyPI"

if ! uv tool install "$extra_spec"; then
    die "Failed to install artifice-ocr. Check the output above for details."
fi

# Resolve the installed entry-point location.  uv tool install places
# executables in the uv tool bin directory (~/.local/bin, or $XDG_BIN_HOME
# if set), with ~/.cargo/bin retained as a fallback for older installs.
entry_point="${XDG_BIN_HOME:-$HOME/.local/bin}/artifice-ocr-web"
if [ ! -x "$entry_point" ]; then
    entry_point="$HOME/.cargo/bin/artifice-ocr-web"
fi
if [ ! -x "$entry_point" ]; then
    entry_point="$(command -v artifice-ocr-web 2>/dev/null || true)"
fi

if [ -z "$entry_point" ]; then
    die "artifice-ocr was installed but the 'artifice-ocr-web' entry point was not found."
fi

info "Entry point: $entry_point"

# --- WSL native-window guidance -----------------------------------------------

if [ "$is_wsl" = true ] && [ "$WEB_ONLY" = false ]; then
    echo ""
    echo "  ── WSL native-window note ─────────────────────────────────"
    echo "  pywebview is installed, but WSL has no native display server."
    echo "  To launch in a native window (instead of the browser fallback):"
    echo ""
    echo "    Option A — WSLg (Windows 11 22H2+, zero setup):"
    echo "      Just run:  artifice-ocr-web"
    echo "      If WSLg is enabled, the window opens directly."
    echo ""
    echo "    Option B — X11 forwarding (X410, VcXsrv, GWSL):"
    echo "      1. Start your X server on Windows"
    echo "      2. In WSL:  export DISPLAY=\$(grep -m 1 nameserver /etc/resolv.conf | awk '{print \$2}'):0"
    echo "      3. Install GTK libs:  sudo apt install libgtk-3-0 gir1.2-webkit2-4.0"
    echo "      4. Run:  artifice-ocr-web"
    echo ""
    echo "  Without a display, the app automatically opens in your"
    echo "  Windows browser at http://localhost:8765."
    echo "  ──────────────────────────────────────────────────────────"
elif [ "$WEB_ONLY" = true ]; then
    echo ""
    echo "  Installed web-only mode.  Launch with:"
    echo "    artifice-ocr-web"
    echo ""
    echo "  For native-window mode, re-run with:  bash install.sh"
fi

# --- desktop shortcut ---------------------------------------------------------

banner "Desktop shortcut"

if [ "$is_wsl" = true ] || [ "$os_type" = "Linux" ]; then

    desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$desktop_dir"

    cat > "$desktop_dir/artifice-ocr.desktop" << DESKTOP_EOF
[Desktop Entry]
Type=Application
Name=ArtificeOCR
Comment=Local-first OCR for historical documents
Exec=$entry_point
Terminal=true
Categories=Office;Scanning;
StartupNotify=true
DESKTOP_EOF

    chmod +x "$desktop_dir/artifice-ocr.desktop"
    info "Created: $desktop_dir/artifice-ocr.desktop"

    desktop_folder="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
    if [ -d "$desktop_folder" ]; then
        cp "$desktop_dir/artifice-ocr.desktop" "$desktop_folder/"
        info "Copied to: $desktop_folder/artifice-ocr.desktop"
    fi

    # On WSL, also try to place the shortcut on the Windows Desktop so it
    # is visible in the Windows shell.  USERPROFILE comes from the WSL
    # interop environment; if it is absent fall back to /mnt/c/Users.
    if [ "$is_wsl" = true ]; then
        win_userprofile="${USERPROFILE:-/mnt/c/Users}"
        if [ -n "$win_userprofile" ] && [ -d "/mnt/c" ]; then
            win_desktop="$win_userprofile/Desktop"
            # Resolve USERPROFILE to a real WSL path if it is a Windows path
            if [ -d "$win_userprofile" ]; then
                :
            elif command -v wslpath >/dev/null 2>&1; then
                win_desktop="$(wslpath "$win_userprofile")/Desktop" 2>/dev/null || win_desktop=""
            else
                win_desktop=""
            fi
            if [ -n "$win_desktop" ] && [ -d "$win_desktop" ]; then
                cp "$desktop_dir/artifice-ocr.desktop" "$win_desktop/" 2>/dev/null && \
                    info "Copied to Windows Desktop: $win_desktop/artifice-ocr.desktop" || true
            fi
        fi
    fi

elif [ "$os_type" = "Darwin" ]; then

    command_path="$HOME/Desktop/ArtificeOCR.command"

    cat > "$command_path" << COMMAND_EOF
#!/usr/bin/env bash
exec "$entry_point"
COMMAND_EOF

    chmod +x "$command_path"
    info "Created: $command_path"
    info "  Double-click it in Finder to launch ArtificeOCR."

fi

# --- summary & next steps -----------------------------------------------------

echo ""
echo "==============================================="
echo "  Install complete!"
echo "==============================================="
echo ""
echo "  GUI:   artifice-ocr-web   (native window or http://127.0.0.1:8765)"
echo "  CLI:   artifice-ocr       (e.g. artifice-ocr pipeline doc.png)"
echo "  Uninstall: uv tool uninstall artifice-ocr"
echo ""
echo "  Next: start your local model runners"
echo "    LM Studio:  load allenai/olmocr-2-7b               (port 1234)"
echo "    Ollama:     ollama pull richardyoung/olmocr2:7b-q8 (port 11434, OCR)"
echo "                ollama pull aya-expanse:8b             (optional, translation)"
echo ""
printf "Press Enter to exit."
read -r
