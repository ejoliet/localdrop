$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
try { py -3 -m PyInstaller --version | Out-Null } catch { py -3 -m pip install --user pyinstaller }
py -3 -m PyInstaller --onefile --name localdrop_host --distpath "$Root\native\windows" --workpath "$Root\build\pyinstaller" --specpath "$Root\build" "$Root\native\localdrop_host.py"
Write-Host "Built $Root\native\windows\localdrop_host.exe"
