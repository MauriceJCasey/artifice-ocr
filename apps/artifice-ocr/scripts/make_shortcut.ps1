# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

<#
.SYNOPSIS
    Create the "OCR Pipeline" desktop shortcut.

.DESCRIPTION
    Writes "OCR Pipeline.lnk" into the project root, pointing at the windowed
    Python interpreter (pythonw.exe) that actually has the project's
    dependencies installed. Drag the result onto your Desktop, or pass
    -Desktop to have a copy placed there directly.

    Re-run this if you move the project or reinstall Python: the shortcut
    stores an absolute path to the interpreter, and that is the one thing the
    launcher cannot repair by itself.

.EXAMPLE
    py -3.12 scripts\make_icon.py          # (re)generate the icon
    powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
    powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1 -Desktop
#>
[CmdletBinding()]
param(
    [switch]$Desktop
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root "launch_artifice_ocr.pyw"
$icon = Join-Path $root "assets\artifice_ocr.ico"

if (-not (Test-Path $launcher)) {
    throw "Launcher not found: $launcher"
}

# --- find an interpreter that can actually run the tool ---------------------
$required = "tkinterdnd2, openai, ollama, yaml, fitz"
$chosen = $null

$candidates = @()
try {
    foreach ($line in (& py -0p 2>$null)) {
        $idx = $line.IndexOf("C:\")
        if ($idx -ge 0) { $candidates += $line.Substring($idx).Trim().Trim('"') }
    }
} catch {
    Write-Warning "The 'py' launcher is unavailable; falling back to PATH."
}
$candidates += @((Get-Command python.exe -ErrorAction SilentlyContinue).Source)

# Probing prints a traceback for each interpreter that lacks the packages.
# In Windows PowerShell, redirecting a native command's stderr wraps every
# line in an ErrorRecord, which $ErrorActionPreference='Stop' then treats as
# fatal - so relax it for the probe and rely on $LASTEXITCODE instead.
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    foreach ($exe in ($candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)) {
        & $exe -c "import $required" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $chosen = $exe; break }
    }
} finally {
    $ErrorActionPreference = $previousPreference
}

if (-not $chosen) {
    throw "No installed Python has the dependencies. Run 'pip install -e .' in $root first."
}

# Prefer the windowed twin so no console window appears behind the app.
$windowed = Join-Path (Split-Path $chosen) "pythonw.exe"
$target = if (Test-Path $windowed) { $windowed } else { $chosen }
Write-Host "Interpreter : $target"

if (-not (Test-Path $icon)) {
    Write-Host "Icon missing - generating it..."
    & $chosen (Join-Path $root "scripts\make_icon.py")
}

# --- write the shortcut -----------------------------------------------------
function New-AppShortcut([string]$Path) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($Path)
    $sc.TargetPath = $target
    $sc.Arguments = '"' + $launcher + '"'
    $sc.WorkingDirectory = $root      # imports resolve as `src.artifice_ocr`
    $sc.Description = "OCR Pipeline - Historical Document Processor"
    $sc.WindowStyle = 1
    if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
    $sc.Save()
    Write-Host "Created     : $Path"
}

New-AppShortcut (Join-Path $root "OCR Pipeline.lnk")

if ($Desktop) {
    New-AppShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "OCR Pipeline.lnk")
} else {
    Write-Host ""
    Write-Host "Drag 'OCR Pipeline.lnk' onto your Desktop, or re-run with -Desktop."
}
