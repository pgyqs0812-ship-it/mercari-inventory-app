# build_windows.ps1 — Build MIA Inventory as a Windows portable ZIP.
#
# Produces:
#   dist\MIAInventory_Windows_<version>.zip   Portable ZIP (no installer required)
#
# Prerequisites:
#   - Python 3.9–3.12 in PATH  (3.12 recommended; 3.13 not yet fully supported by PyInstaller)
#   - Google Chrome installed
#   - Run from the project root in PowerShell:
#       Set-ExecutionPolicy -Scope Process Bypass
#       .\build_windows.ps1
#
# Optional env vars:
#   $env:VERSION  — override version tag (default: latest git tag)
#
# Code signing (future):
#   Uncomment and configure the signtool section at the bottom.
#   Requires an OV or EV code signing certificate.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AppName    = "MIAInventory"
$Entry      = "main.py"
$Version    = if ($env:VERSION) { $env:VERSION } else {
    try { git describe --tags --abbrev=0 2>$null } catch { "v0.0.0" }
}
$ZipName    = "MIAInventory_Windows_$Version.zip"
$DistFolder = "dist\$AppName"
$ZipPath    = "dist\$ZipName"

Write-Host ""
Write-Host "╔══════════════════════════════════════════════╗"
Write-Host "║   MIA Inventory — Windows Build Tool         ║"
Write-Host "╚══════════════════════════════════════════════╝"
Write-Host ""
Write-Host "  バージョン : $Version"
Write-Host ""

# ── Python / venv ─────────────────────────────────────────────────────────────
if (Test-Path "venv\Scripts\Activate.ps1") {
    . "venv\Scripts\Activate.ps1"
    Write-Host "✓ venv アクティベート: $(python --version)"
} else {
    Write-Host "  (venv が見つかりません — システム Python を使用: $(python --version))"
}

# Warn if Python >= 3.13
$pyVer = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$pyVer -ge [version]"3.13") {
    Write-Host ""
    Write-Host "  ⚠  Python $pyVer を検出しました。"
    Write-Host "     PyInstaller は Python 3.12 までを正式サポートしています。"
    Write-Host "     ビルドが失敗する場合は Python 3.12 の venv を作成して再試行してください。"
    Write-Host ""
}

# ── Install dependencies ──────────────────────────────────────────────────────
Write-Host "  依存パッケージをインストール中..."
pip install -r requirements.txt --quiet
if (-not (python -c "import PyInstaller" 2>$null; $?)) {
    pip install pyinstaller --quiet
}
$pyiVer = python -c "import PyInstaller; print(PyInstaller.__version__)"
Write-Host "✓ PyInstaller $pyiVer"

# ── Write _version.py ─────────────────────────────────────────────────────────
"APP_VERSION = '$Version'" | Set-Content "_version.py" -Encoding UTF8
Write-Host "✓ _version.py に $Version を書き込みました"

# ── Collect selenium-manager path ────────────────────────────────────────────
$SelManagerRel = python -c @"
import os, selenium as _s
base = os.path.dirname(os.path.abspath(_s.__file__))
win_mgr = os.path.join(base, 'webdriver', 'common', 'windows', 'selenium-manager.exe')
if os.path.isfile(win_mgr):
    rel = os.path.relpath(win_mgr)
    print(rel.replace(os.sep, '/'))
"@

# ── Build with PyInstaller ────────────────────────────────────────────────────
Write-Host ""
Write-Host "  PyInstaller ビルド中..."

$PyiArgs = @(
    "--noconfirm",
    "--windowed",
    "--name", $AppName,
    "--collect-all", "selenium",
    "--collect-all", "flask",
    "--collect-all", "openpyxl",
    "--hidden-import", "psutil",
    "--hidden-import", "dotenv",
    "--hidden-import", "sqlite3",
    "--hidden-import", "queue",
    "--hidden-import", "concurrent.futures"
)

if ($SelManagerRel) {
    $SelDir = [System.IO.Path]::GetDirectoryName($SelManagerRel) -replace "\\", "/"
    $PyiArgs += "--add-binary"
    $PyiArgs += "${SelManagerRel};${SelDir}/"
    Write-Host "  selenium-manager: $SelManagerRel → $SelDir/"
}

if (Test-Path "AppIcon.ico") {
    $PyiArgs += "--icon"
    $PyiArgs += "AppIcon.ico"
}

$PyiArgs += $Entry
pyinstaller @PyiArgs

if (-not (Test-Path $DistFolder)) {
    Write-Host "ERROR: ビルドに失敗しました。dist\$AppName が存在しません。"
    exit 1
}
Write-Host "✓ ビルド完了: $DistFolder"

# ── Create portable ZIP ───────────────────────────────────────────────────────
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path "$DistFolder\*" -DestinationPath $ZipPath
$zipSize = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Host "✓ ZIP 作成: $ZipPath ($zipSize MB)"

# ── SHA-256 ───────────────────────────────────────────────────────────────────
$hash = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLower()
Write-Host ""
Write-Host "  SHA-256: $hash"
"$hash  $ZipName" | Set-Content "dist\$ZipName.sha256" -Encoding UTF8
Write-Host "  チェックサム: dist\$ZipName.sha256"

# ── (Future) Code signing ─────────────────────────────────────────────────────
# Uncomment and configure when an OV/EV certificate is available:
#
# $ExePath = "$DistFolder\$AppName.exe"
# $TimestampUrl = "http://timestamp.digicert.com"
# signtool sign /fd SHA256 /tr $TimestampUrl /td SHA256 `
#               /n "Your Certificate Subject" $ExePath
# signtool verify /pa $ExePath
# Write-Host "✓ コード署名完了"

# ── Cleanup _version.py ───────────────────────────────────────────────────────
Remove-Item "_version.py" -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "╔══════════════════════════════════════════════╗"
Write-Host "║   ビルド完了！                               ║"
Write-Host "╠══════════════════════════════════════════════╣"
Write-Host "║                                              ║"
Write-Host "║  ZIP : dist\$ZipName"
Write-Host "║                                              ║"
Write-Host "║  配布方法:                                   ║"
Write-Host "║    1. ZIP を展開する                         ║"
Write-Host "║    2. MIAInventory.exe をダブルクリック       ║"
Write-Host "║                                              ║"
Write-Host "║  注意: Google Chrome のインストールが必要です ║"
Write-Host "╚══════════════════════════════════════════════╝"
Write-Host ""
Write-Host "NOTE: ChromeDriver は Selenium Manager が自動管理します。"
Write-Host "      ユーザー環境には Google Chrome のみインストールが必要です。"
