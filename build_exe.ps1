# Build dist\server.exe (single file) from server.py with PyInstaller.
# Usage: powershell -ExecutionPolicy Bypass -File build_exe.ps1
# Needs: py -3.13 -m pip install -r requirements.txt pyinstaller

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

py -3.13 -m PyInstaller --noconfirm --clean --onefile --console `
    --name server `
    --collect-all playwright `
    --hidden-import bridge `
    --hidden-import bridge.worker `
    --hidden-import bridge.actions `
    --hidden-import bridge.config `
    --hidden-import bridge.setup `
    server.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

# The exe reads its config from its own folder — ship an editable copy next to it.
Copy-Item ai_models_config.json dist\ -Force

Write-Host "Built dist\server.exe (config: dist\ai_models_config.json)"
