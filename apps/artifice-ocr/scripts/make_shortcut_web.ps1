# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

<#
.SYNOPSIS
    Create the "OCR Pipeline (Web)" desktop shortcut.

.DESCRIPTION
    Writes "OCR Pipeline (Web).lnk" into the project root, pointing at the
    windowed Python interpreter (pythonw.exe) best able to run the web
    frontend. Drag the result onto your Desktop, or pass -Desktop to place a
    copy there directly.

    Re-run this after moving the project or reinstalling Python: the shortcut
    stores an absolute path to the interpreter, which the launcher cannot
    repair by itself once it is gone.

.EXAMPLE
    py -3.12 scripts\make_web_icon.py       # (re)generate the icon
    powershell -ExecutionPolicy Bypass -File scripts\make_shortcut_web.ps1
    powershell -ExecutionPolicy Bypass -File scripts\make_shortcut_web.ps1 -Desktop
#>
[CmdletBinding()]
param(
    [switch]$Desktop
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root "launch_artifice_ocr_web.pyw"
$icon = Join-Path $root "assets\artifice_ocr_web.ico"

if (-not (Test-Path $launcher)) {
    throw "Launcher not found: $launcher"
}

# Same core deps the pipeline itself needs, plus the web stack — matches
# launch_artifice_ocr_web.pyw's REQUIRED tuple. No tkinterdnd2: the web build
# has no tkinter drop zone to need it.
$required = "fastapi, uvicorn, openai, ollama, yaml, fitz"

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
$candidates = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

# Probing prints a traceback for each interpreter that lacks a package. In
# Windows PowerShell, redirecting a native command's stderr wraps every line in
# an ErrorRecord, which $ErrorActionPreference='Stop' then treats as fatal - so
# relax it for the probe and rely on $LASTEXITCODE instead.
function Test-Imports([string]$Exe, [string]$Modules) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe -c "import $Modules" 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previous
    }
}

$chosen = $null
foreach ($exe in $candidates) {
    if (Test-Imports $exe $required) {
        $chosen = $exe
        break
    }
}
if (-not $chosen) {
    throw "No installed Python has the web dependencies. Run 'pip install -e "".[web]""' in $root first."
}

$windowed = Join-Path (Split-Path $chosen) "pythonw.exe"
$target = if (Test-Path $windowed) { $windowed } else { $chosen }
Write-Host "Interpreter : $target"

if (-not (Test-Path $icon)) {
    Write-Host "Icon missing - generating it..."
    & $chosen (Join-Path $root "scripts\make_web_icon.py")
}

function New-AppShortcut([string]$Path) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($Path)
    $sc.TargetPath = $target
    $sc.Arguments = '"' + $launcher + '"'
    $sc.WorkingDirectory = $root
    $sc.Description = "OCR Pipeline (Web) - browser frontend for the same pipeline"
    $sc.WindowStyle = 1
    if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
    $sc.Save()
    Write-Host "Created     : $Path"
}

New-AppShortcut (Join-Path $root "OCR Pipeline (Web).lnk")

if ($Desktop) {
    New-AppShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "OCR Pipeline (Web).lnk")
} else {
    Write-Host ""
    Write-Host "Drag 'OCR Pipeline (Web).lnk' onto your Desktop, or re-run with -Desktop."
}
