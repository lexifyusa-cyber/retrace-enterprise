# build-portable-win.ps1 — Build KnowledgePod Windows portable .exe
#
# Usage:  .\scripts\build-portable-win.ps1
# Output: main-app\frontend\dist-electron\KnowledgePod-*.exe (portable, no install)
#
# Run this on Windows. Requires: Node.js, npm, Python 3.11 or 3.12

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$FrontendDir = Join-Path $RootDir "main-app\frontend"
$BackendDir = Join-Path $RootDir "main-app\backend"
$BundleDir = Join-Path $FrontendDir "backend-bundle"

Write-Host "======================================"
Write-Host "  KnowledgePod Portable EXE Builder"
Write-Host "======================================"
Write-Host ""

# ── 0. Prerequisites ───────────────────────────────────────────────────────

Write-Host "[1/6] Checking prerequisites..."

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Node.js required. Install from https://nodejs.org/"
    exit 1
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: npm required."
    exit 1
}

$Python = $null
foreach ($candidate in @("python3.12", "python3.11", "py -3.12", "py -3.11", "python")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "3\.(11|12)") { $Python = $candidate; break }
    } catch {}
}
if (-not $Python) {
    Write-Host "ERROR: Python 3.11 or 3.12 required. Install from https://python.org/"
    exit 1
}

Write-Host "  Node: $(node --version)"
Write-Host "  npm:  $(npm --version)"
Write-Host "  Python: $Python"
Write-Host ""

# ── 1. Build React frontend ───────────────────────────────────────────────

Write-Host "[2/6] Building React frontend..."
Set-Location $FrontendDir
npm install --no-audit --no-fund 2>&1 | Select-Object -Last 1
npx vite build
Write-Host "  Frontend built -> $FrontendDir\dist\"
Write-Host ""

# ── 2. Icon (optional) ─────────────────────────────────────────────────────

$IconIco = Join-Path $FrontendDir "public\icon.ico"
if (-not (Test-Path $IconIco)) {
    Write-Host "[3/6] No icon.ico found - electron-builder will use default icon."
} else {
    Write-Host "[3/6] Using icon.ico"
}
Write-Host ""

# ── 3. Bundle Python backend + venv ────────────────────────────────────────

Write-Host "[4/6] Bundling Python backend..."

if (Test-Path $BundleDir) { Remove-Item -Recurse -Force $BundleDir }
New-Item -ItemType Directory -Path $BundleDir | Out-Null

# Copy backend with robocopy (exclude venv, caches, db)
Remove-Item -Recurse -Force $BundleDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $BundleDir | Out-Null
$robocopyResult = robocopy $BackendDir $BundleDir /E /XD __pycache__ venv .git screenops_screenshots /XF .env knowledgepod.db knowledgepod.db-wal knowledgepod.db-shm screenops_debug.log directory_tool.py
if ($robocopyResult -gt 7) { exit 1 }

Write-Host "  Creating Python venv..."
& $Python -m venv (Join-Path $BundleDir "venv")
$VenvPython = Join-Path $BundleDir "venv\Scripts\python.exe"

Write-Host "  Installing dependencies (this may take a few minutes)..."
& $VenvPython -m pip install --upgrade pip setuptools wheel -q
& $VenvPython -m pip install -r (Join-Path $BundleDir "requirements.txt") -q
& $VenvPython -m pip install "uvicorn[standard]" -q 2>$null

Write-Host "  Backend bundled -> $BundleDir"
$BundleSize = (Get-ChildItem $BundleDir -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host "  Bundle size: $([math]::Round($BundleSize, 1)) MB"
Write-Host ""

# ── 4. Copy frontend dist into backend bundle ──────────────────────────────

Write-Host "[5/6] Copying frontend dist into backend bundle..."
$FrontendDist = Join-Path $BundleDir "frontend-dist"
New-Item -ItemType Directory -Path $FrontendDist -Force | Out-Null
Copy-Item (Join-Path $FrontendDir "dist\*") $FrontendDist -Recurse -Force
Write-Host "  Done"
Write-Host ""

# ── 5. Build Electron portable exe ─────────────────────────────────────────

Write-Host "[6/6] Building portable EXE..."
Set-Location $FrontendDir
npx electron-builder --win --publish=never

Write-Host ""
Write-Host "======================================"
Write-Host "  BUILD COMPLETE"
Write-Host "======================================"
Write-Host ""
$ExePath = Get-ChildItem (Join-Path $FrontendDir "dist-electron\*.exe") -ErrorAction SilentlyContinue | Select-Object -First 1
if ($ExePath) {
    Write-Host "Portable EXE: $($ExePath.FullName)"
    Write-Host ""
    Write-Host "Copy this .exe anywhere and run it - no installation required."
} else {
    Write-Host "Check dist-electron\ for output."
}
Write-Host ""
