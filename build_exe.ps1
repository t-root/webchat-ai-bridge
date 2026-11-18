# Build dist\server.exe (single file) from server.py with PyInstaller.
# Usage: powershell -ExecutionPolicy Bypass -File build_exe.ps1
# Needs: py -3.13 -m pip install -r requirements.txt pyinstaller

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

py -3.13 -m PyInstaller --noconfirm --clean --onefile --console `
    --name server `
    --collect-all playwright `
    --add-data "ai_models_config.json;." `
    --hidden-import bridge `
    --hidden-import bridge.worker `
    --hidden-import bridge.actions `
    --hidden-import bridge.config `
    --hidden-import bridge.setup `
    --hidden-import bridge.google_login `
    server.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

# The exe carries ai_models_config.json and writes it next to itself on first run if missing.
Write-Host "Built dist\server.exe"
