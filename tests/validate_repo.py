from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

errors: list[str] = []
if manifest.get("manifest_version") != 3:
    errors.append("manifest_version must be 3")
permissions = set(manifest.get("permissions", []))
if "<all_urls>" in permissions or manifest.get("host_permissions"):
    errors.append("broad host permissions are not allowed")
if permissions != {"nativeMessaging", "storage", "clipboardWrite"}:
    errors.append(f"unexpected permissions: {sorted(permissions)}")

references = [
    manifest["background"]["service_worker"],
    *manifest.get("icons", {}).values(),
]
for reference in references:
    if not (EXTENSION / reference).is_file():
        errors.append(f"missing manifest reference: {reference}")

for required in ("app.html", "app.css", "app.js", "background.js"):
    if not (EXTENSION / required).is_file():
        errors.append(f"missing extension file: {required}")

for path in EXTENSION.rglob("*"):
    if not path.is_file():
        continue
    if path.suffix.lower() not in {".js", ".html", ".css", ".json"}:
        continue
    text = path.read_text(encoding="utf-8")
    if re.search(r"<script[^>]+src=[\"']https?://", text, re.IGNORECASE):
        errors.append(f"remote executable script in {path.relative_to(ROOT)}")
    if "eval(" in text or "new Function(" in text:
        errors.append(f"dynamic code execution in {path.relative_to(ROOT)}")
    if re.search(r"\bTODO\b", text):
        errors.append(f"core TODO remains in {path.relative_to(ROOT)}")


html_text = (EXTENSION / "app.html").read_text(encoding="utf-8")
js_text = (EXTENSION / "app.js").read_text(encoding="utf-8")
html_ids = set(re.findall(r'\bid=["\']([A-Za-z][A-Za-z0-9_-]*)["\']', html_text))
referenced_ids = set(re.findall(r'\belements\.([A-Za-z][A-Za-z0-9_]*)', js_text))
for missing_id in sorted(referenced_ids - html_ids):
    errors.append(f"app.js references missing DOM id: {missing_id}")

extension_id = (ROOT / "extension-id.txt").read_text(encoding="utf-8").strip()
if not re.fullmatch(r"[a-p]{32}", extension_id):
    errors.append("extension-id.txt is invalid")
else:
    public_key_der = base64.b64decode(manifest["key"], validate=True)
    digest = hashlib.sha256(public_key_der).hexdigest()[:32]
    derived_id = "".join(chr(ord("a") + int(char, 16)) for char in digest)
    if derived_id != extension_id:
        errors.append(f"manifest key derives {derived_id}, not {extension_id}")

if errors:
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)
print("Repository validation passed")
