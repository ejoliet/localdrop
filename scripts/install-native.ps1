$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ExtensionId = (Get-Content "$Root\extension-id.txt" -Raw).Trim()
$HostName = "com.localdrop.live"
$SourceExe = "$Root\native\windows\localdrop_host.exe"
if (-not (Test-Path $SourceExe)) {
  & "$Root\scripts\build-windows-host.ps1"
}
$InstallDir = Join-Path $env:LOCALAPPDATA "LocalDropLive"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$HostExe = Join-Path $InstallDir "localdrop_host.exe"
Copy-Item $SourceExe $HostExe -Force
$ManifestPath = Join-Path $InstallDir "$HostName.json"
$ManifestJson = @{
  name = $HostName
  description = "LocalDrop Live native file server"
  path = $HostExe
  type = "stdio"
  allowed_origins = @("chrome-extension://$ExtensionId/")
} | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText($ManifestPath, $ManifestJson, (New-Object System.Text.UTF8Encoding($false)))
reg add "HKCU\Software\Google\Chrome\NativeMessagingHosts\$HostName" /ve /t REG_SZ /d $ManifestPath /f | Out-Null
Write-Host "Native companion installed for extension ID: $ExtensionId"
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
  Write-Host "cloudflared is not on PATH. Local links work; public links require cloudflared."
}
