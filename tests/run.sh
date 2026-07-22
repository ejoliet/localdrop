#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"
python3 -m py_compile native/localdrop_host.py tests/test_host.py tests/test_native_protocol.py tests/validate_repo.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/validate_repo.py
if command -v node >/dev/null 2>&1; then
  node --check extension/background.js
  node --check extension/app.js
else
  echo "WARN: node not found; JavaScript syntax check skipped" >&2
fi
bash -n scripts/install-native.sh scripts/uninstall-native.sh scripts/package.sh
echo "All checks passed"
