# Build RadioGatewayClient.exe (windowed, one file) with PyInstaller.
#
# Usage:  powershell -ExecutionPolicy Bypass -File build_windows_client.ps1
# Needs:  py -m pip install pyinstaller numpy scipy sounddevice soxr customtkinter keyboard pystray pillow
#
# The exe holds Python + dependencies. The client code (windows_audio_client.py,
# windows_audio_gui.py) is bundled only as a first-run seed; after that it is
# loaded from %LOCALAPPDATA%\RadioGatewayClient\app and updated from the gateway,
# so you only rebuild the exe when windows_client_launcher.py or the dependency
# set changes.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name RadioGatewayClient `
    --collect-all customtkinter `
    --collect-submodules scipy.signal `
    --hidden-import pystray._win32 `
    --add-data "windows_audio_client.py;." `
    --add-data "windows_audio_gui.py;." `
    windows_client_launcher.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
Write-Host "Built: $PSScriptRoot\dist\RadioGatewayClient.exe"
