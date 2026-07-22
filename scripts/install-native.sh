#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
EXTENSION_ID=$(tr -d '[:space:]' < "$ROOT_DIR/extension-id.txt")
HOST_NAME="com.localdrop.live"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi
PYTHON_BIN=$(command -v python3)

INSTALL_DIR="$HOME/.localdrop-live/bin"
mkdir -p "$INSTALL_DIR"
cp "$ROOT_DIR/native/localdrop_host.py" "$INSTALL_DIR/localdrop_host.py"
chmod 600 "$INSTALL_DIR/localdrop_host.py"
cat > "$INSTALL_DIR/localdrop_host" <<EOF
#!/bin/sh
exec "$PYTHON_BIN" "$INSTALL_DIR/localdrop_host.py" "\$@"
EOF
chmod 700 "$INSTALL_DIR/localdrop_host"

HOST_MANIFEST=$(cat <<JSON
{
  "name": "$HOST_NAME",
  "description": "LocalDrop Live native file server",
  "path": "$INSTALL_DIR/localdrop_host",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXTENSION_ID/"]
}
JSON
)

install_manifest() {
  local directory="$1"
  mkdir -p "$directory"
  printf '%s\n' "$HOST_MANIFEST" > "$directory/$HOST_NAME.json"
  chmod 600 "$directory/$HOST_NAME.json"
  echo "Installed native host manifest: $directory/$HOST_NAME.json"
}

case "$(uname -s)" in
  Darwin)
    install_manifest "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    install_manifest "$HOME/Library/Application Support/Google/ChromeForTesting/NativeMessagingHosts"
    install_manifest "$HOME/Library/Application Support/Chromium/NativeMessagingHosts"
    ;;
  Linux)
    install_manifest "$HOME/.config/google-chrome/NativeMessagingHosts"
    install_manifest "$HOME/.config/google-chrome-for-testing/NativeMessagingHosts"
    install_manifest "$HOME/.config/chromium/NativeMessagingHosts"
    ;;
  *)
    echo "Unsupported platform. Use install-native.ps1 on Windows." >&2
    exit 1
    ;;
esac

echo
echo "Native companion installed for extension ID: $EXTENSION_ID"
if command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared found: $(command -v cloudflared)"
else
  echo "cloudflared not found. Local links work; public links require cloudflared."
  if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "Install with: brew install cloudflared"
  fi
fi
