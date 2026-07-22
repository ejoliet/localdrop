#!/usr/bin/env bash
set -euo pipefail
HOST_NAME="com.localdrop.live"
PATHS=(
  "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/$HOST_NAME.json"
  "$HOME/Library/Application Support/Google/ChromeForTesting/NativeMessagingHosts/$HOST_NAME.json"
  "$HOME/Library/Application Support/Chromium/NativeMessagingHosts/$HOST_NAME.json"
  "$HOME/.config/google-chrome/NativeMessagingHosts/$HOST_NAME.json"
  "$HOME/.config/google-chrome-for-testing/NativeMessagingHosts/$HOST_NAME.json"
  "$HOME/.config/chromium/NativeMessagingHosts/$HOST_NAME.json"
)
for path in "${PATHS[@]}"; do rm -f "$path"; done
rm -rf "$HOME/.localdrop-live"
echo "LocalDrop Live native companion removed."
