# LocalDrop Live

LocalDrop Live is a Manifest V3 Chrome extension with a small native companion. Drop files or a static website into the extension, get a localhost URL, and optionally create a temporary public URL through Cloudflare Quick Tunnels.

## Why a native companion exists

Chrome extensions cannot bind a listening TCP port. The extension uses Chrome Native Messaging to send file chunks to a local Python process. The process stores the files in a temporary directory and runs an HTTP server bound only to `127.0.0.1`.

## Features

- Drag/drop files and folders.
- Static-site hosting with `index.html` support.
- Optional single-page application fallback.
- Byte-range requests for video and large files.
- Optional Cross-Origin Resource Sharing (CORS).
- High-entropy capability path in every URL.
- Temporary public link through an explicit `cloudflared` Quick Tunnel.
- Request activity log.
- Stop action terminates the tunnel, server, and deletes temporary files.
- No accounts, database, build step, remote JavaScript, or file upload service.

## Architecture

```text
app.html / app.js
  -> chrome.runtime messaging
background.js service worker
  -> chrome.runtime.connectNative("com.localdrop.live")
localdrop_host.py
  -> temporary directory
  -> ThreadingHTTPServer on 127.0.0.1:<random-port>
  -> optional cloudflared tunnel
```

The service worker maintains the native messaging port. Chrome 105 or newer keeps an extension service worker alive while a native messaging port is connected.

## Install: macOS or Linux

Requirements:

- Google Chrome 105+
- Python 3.10+
- Optional: `cloudflared` for public links

1. Extract the bundle.
2. Install the native companion:

   ```bash
   ./scripts/install-native.sh
   ```

3. Open `chrome://extensions`.
4. Enable **Developer mode**.
5. Click **Load unpacked**.
6. Select the `extension/` directory.
7. Click the LocalDrop Live toolbar icon.

For public links on macOS:

```bash
brew install cloudflared
```

On Linux, install `cloudflared` using Cloudflare's package instructions or place it on `PATH`.

## Install: Windows

Requirements:

- Google Chrome 105+
- Python 3
- PyInstaller, installed automatically by the build script if missing
- Optional: `cloudflared.exe` on `PATH`

Run PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-native.ps1
```

Then load `extension/` unpacked from `chrome://extensions`.

Windows packaging was not runtime-tested in this build environment.

## Usage

1. Open LocalDrop Live.
2. Drop files, choose files, or choose a folder.
3. Select SPA fallback or CORS only when needed.
4. Click **Start local server**.
5. Open or copy the localhost link.
6. Click **Create temporary public link** only when external access is needed.
7. Click **Stop and delete temporary files** when finished.

If the dropped root contains `index.html`, it is served at the capability URL. Otherwise the server shows a file listing.

## Security and privacy

- The HTTP server binds only to `127.0.0.1`; it does not open a LAN-facing port.
- Public access is disabled until the user explicitly starts a tunnel.
- URLs contain a random 192-bit capability token.
- Uploaded paths are normalized and checked against traversal.
- Files are copied into a private temporary directory. Original files are never modified.
- Temporary files are removed when the session is stopped or the native process exits.
- The extension has no host permissions and does not inspect browser pages.
- Public traffic passes through Cloudflare while a Quick Tunnel is active. Do not use it for confidential files unless this trust model is acceptable.
- Capability links are not authentication. Anyone who receives the link can access the files while the session is active.

## Permissions

- `nativeMessaging`: communicate with the local companion.
- `storage`: persist whether the service worker should reconnect after a restart.
- `clipboardWrite`: copy local and public links after a user click.

No `host_permissions`, `tabs`, or `<all_urls>` access is requested. The `tabs` API methods used to open the extension page and links do not require the `tabs` permission because no sensitive tab fields are read.

## Limits

- 2 GiB per file.
- 5 GiB per session.
- Files are base64-encoded in 384 KiB chunks for Native Messaging. This is functional but slower than direct filesystem access.
- Empty directories are not preserved.
- Symbolic links are not followed because browser file drops expose file contents rather than filesystem links.
- Cloudflare Quick Tunnels are intended for temporary development/testing use, not stable production hosting.
- Public URLs change every time the tunnel starts.
- The host process and server stop when Chrome closes or the native messaging port is lost.

## Development and tests

Run all checks:

```bash
./tests/run.sh
```

The test suite checks:

- Python syntax and unit tests.
- Native Messaging framing and upload integration.
- Local HTTP serving, directory listing, SPA fallback, range requests, and traversal rejection.
- JavaScript syntax.
- Manifest JSON and referenced files.
- Absence of remote executable code and broad host permissions.

## Package

```bash
./scripts/package.sh
```

Outputs:

- `dist/localdrop-live-extension.zip`: extension-only store upload artifact.
- `dist/localdrop-live-bundle.zip`: complete source, installers, tests, and companion.

The extension-only ZIP is not useful without installing the native companion.

## Uninstall

macOS/Linux:

```bash
./scripts/uninstall-native.sh
```

Windows:

```powershell
.\scripts\uninstall-native.ps1
```

Then remove the unpacked extension from `chrome://extensions`.
