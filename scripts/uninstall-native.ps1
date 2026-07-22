$ErrorActionPreference = "SilentlyContinue"
$HostName = "com.localdrop.live"
reg delete "HKCU\Software\Google\Chrome\NativeMessagingHosts\$HostName" /f | Out-Null
Remove-Item (Join-Path $env:LOCALAPPDATA "LocalDropLive") -Recurse -Force
Write-Host "LocalDrop Live native companion removed."
