#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DIST_DIR="$ROOT_DIR/dist"
mkdir -p "$DIST_DIR"
rm -f "$DIST_DIR/localdrop-live-extension.zip" "$DIST_DIR/localdrop-live-bundle.zip"
(
  cd "$ROOT_DIR/extension"
  zip -qr "$DIST_DIR/localdrop-live-extension.zip" . -x '*.DS_Store'
)
(
  cd "$ROOT_DIR"
  zip -qr "$DIST_DIR/localdrop-live-bundle.zip" \
    extension native scripts tests README.md LICENSE extension-id.txt \
    -x 'native/windows/*' 'build/*' 'dist/*' '*.DS_Store' '__pycache__/*'
)
echo "Created:"
echo "  $DIST_DIR/localdrop-live-extension.zip"
echo "  $DIST_DIR/localdrop-live-bundle.zip"
