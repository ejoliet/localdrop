#!/usr/bin/env python3
"""LocalDrop Live native messaging host.

Receives files from the Chrome extension, serves them from localhost, and can
optionally expose the local server through a user-started Cloudflare Quick Tunnel.
Uses only the Python standard library.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

HOST_NAME = "com.localdrop.live"
PROTOCOL_VERSION = 1
MAX_INBOUND_MESSAGE = 64 * 1024 * 1024
MAX_OUTBOUND_MESSAGE = 1024 * 1024
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
MAX_SESSION_SIZE = 5 * 1024 * 1024 * 1024
MAX_PATH_LENGTH = 1024
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


def _set_windows_binary_mode() -> None:
    if os.name != "nt":
        return
    import msvcrt  # pylint: disable=import-outside-toplevel

    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("Unexpected end of native messaging stream")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_native_message() -> dict[str, Any] | None:
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        return None
    if len(raw_length) != 4:
        raise EOFError("Incomplete native messaging length prefix")
    (length,) = struct.unpack("@I", raw_length)
    if length <= 0 or length > MAX_INBOUND_MESSAGE:
        raise ValueError(f"Invalid inbound message length: {length}")
    payload = _read_exact(sys.stdin.buffer, length)
    message = json.loads(payload.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("Native message must be a JSON object")
    return message


class NativeWriter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def send(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_OUTBOUND_MESSAGE:
            encoded = json.dumps(
                {
                    "v": PROTOCOL_VERSION,
                    "type": "event",
                    "event": "host_error",
                    "message": "Native host attempted to send an oversized response",
                },
                separators=(",", ":"),
            ).encode("utf-8")
        with self._lock:
            try:
                sys.stdout.buffer.write(struct.pack("@I", len(encoded)))
                sys.stdout.buffer.write(encoded)
                sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError):
                return


WRITER = NativeWriter()


def send_event(event: str, **payload: Any) -> None:
    WRITER.send({"v": PROTOCOL_VERSION, "type": "event", "event": event, **payload})


def safe_relative_path(raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > MAX_PATH_LENGTH:
        raise ValueError("Invalid file path")
    normalized = raw_path.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError("Unsafe file path")
    raw_parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError("Unsafe file path")
    if any("\x00" in part for part in raw_parts):
        raise ValueError("Unsafe file path")
    pure = PurePosixPath(*raw_parts)
    if pure.is_absolute():
        raise ValueError("Unsafe file path")
    return Path(*pure.parts)


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@dataclass
class UploadState:
    file_id: str
    relative_path: Path
    expected_size: int
    temp_path: Path
    final_path: Path
    stream: BinaryIO
    received: int = 0
    next_sequence: int = 0
    hasher: Any = field(default_factory=hashlib.sha256)


@dataclass
class SessionState:
    session_id: str
    token: str
    root: Path
    server: ThreadingHTTPServer
    server_thread: threading.Thread
    port: int
    spa_fallback: bool = False
    allow_cors: bool = False
    finalized: bool = False
    total_bytes: int = 0
    file_count: int = 0
    uploads: dict[str, UploadState] = field(default_factory=dict)

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/"


class LocalDropRequestHandler(BaseHTTPRequestHandler):
    server_version = "LocalDropLive/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def session(self) -> SessionState:
        return self.server.session  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self.session.allow_cors:
            self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "CORS is disabled")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._security_headers()
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Cache-Control", "no-store")
        if self.session.allow_cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "Accept-Ranges, Content-Length, Content-Range")

    def _record(self, status: int, path: str, sent_bytes: int = 0) -> None:
        send_event(
            "request",
            timestamp=int(time.time() * 1000),
            method=self.command,
            path=path,
            status=status,
            bytes=sent_bytes,
            userAgent=self.headers.get("User-Agent", "")[:300],
        )

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        body = (message + "\n").encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._record(int(status), self.path, len(body))

    def _resolve_request_path(self) -> tuple[Path | None, str]:
        parsed = urllib.parse.urlsplit(self.path)
        decoded = urllib.parse.unquote(parsed.path)
        token_prefix = f"/{self.session.token}"
        if decoded == token_prefix:
            return None, "redirect"
        if not decoded.startswith(token_prefix + "/"):
            return None, "not_found"
        relative_raw = decoded[len(token_prefix) + 1 :]
        if not relative_raw:
            return self.session.root, "ok"
        try:
            relative = safe_relative_path(relative_raw)
        except ValueError:
            return None, "not_found"
        candidate = self.session.root / relative
        if not is_within(candidate, self.session.root):
            return None, "not_found"
        return candidate, "ok"

    def _serve(self, head_only: bool) -> None:
        target, state = self._resolve_request_path()
        if state == "redirect":
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self._security_headers()
            self.send_header("Location", f"/{self.session.token}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self._record(HTTPStatus.PERMANENT_REDIRECT, self.path)
            return
        if state != "ok" or target is None:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        if target.is_dir():
            index_file = target / "index.html"
            if index_file.is_file():
                self._serve_file(index_file, head_only)
            else:
                self._serve_directory(target, head_only)
            return

        if target.is_file():
            self._serve_file(target, head_only)
            return

        fallback = self.session.root / "index.html"
        if self.session.spa_fallback and fallback.is_file():
            self._serve_file(fallback, head_only)
            return

        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _serve_directory(self, directory: Path, head_only: bool) -> None:
        try:
            relative = directory.relative_to(self.session.root)
            entries = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to list directory")
            return

        title = "/" + relative.as_posix() if relative.parts else "/"
        rows: list[str] = []
        if relative.parts:
            rows.append('<li><a href="../">../</a></li>')
        for entry in entries:
            if entry.name.endswith(".part"):
                continue
            suffix = "/" if entry.is_dir() else ""
            label = html.escape(entry.name + suffix)
            href = urllib.parse.quote(entry.name) + suffix
            size = ""
            if entry.is_file():
                with contextlib.suppress(OSError):
                    size = f'<span>{entry.stat().st_size:,} bytes</span>'
            rows.append(f'<li><a href="{href}">{label}</a>{size}</li>')

        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LocalDrop Live · {html.escape(title)}</title>
<style>body{{font:15px system-ui,sans-serif;max-width:900px;margin:48px auto;padding:0 20px;color:#1d2530}}h1{{font-size:24px}}ul{{list-style:none;padding:0;border-top:1px solid #d9dee5}}li{{display:flex;justify-content:space-between;gap:24px;padding:10px 4px;border-bottom:1px solid #e9edf2}}a{{color:#0b63ce;text-decoration:none;overflow-wrap:anywhere}}span{{color:#657080;white-space:nowrap}}footer{{margin-top:24px;color:#657080;font-size:12px}}</style></head>
<body><h1>{html.escape(title)}</h1><ul>{''.join(rows) or '<li>Empty directory</li>'}</ul><footer>Served temporarily by LocalDrop Live</footer></body></html>"""
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if not head_only:
            self.wfile.write(encoded)
        self._record(HTTPStatus.OK, self.path, len(encoded))

    def _parse_range(self, size: int) -> tuple[int, int] | None:
        header = self.headers.get("Range")
        if not header:
            return None
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
        if not match:
            raise ValueError("Invalid Range header")
        start_raw, end_raw = match.groups()
        if not start_raw and not end_raw:
            raise ValueError("Invalid Range header")
        if not start_raw:
            suffix = int(end_raw)
            if suffix <= 0:
                raise ValueError("Invalid Range header")
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
        if start >= size or start < 0 or end < start:
            raise IndexError("Unsatisfiable range")
        return start, min(end, size - 1)

    def _serve_file(self, file_path: Path, head_only: bool) -> None:
        try:
            size = file_path.stat().st_size
            byte_range = self._parse_range(size)
        except IndexError:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self._security_headers()
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self._record(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, self.path)
            return
        except (OSError, ValueError):
            self._send_error(HTTPStatus.BAD_REQUEST, "Unable to read file")
            return

        start, end = byte_range if byte_range else (0, max(0, size - 1))
        content_length = 0 if size == 0 else end - start + 1
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        status = HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK

        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        sent = 0
        if not head_only and content_length:
            try:
                with file_path.open("rb") as stream:
                    stream.seek(start)
                    remaining = content_length
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                        sent += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                pass
        self._record(int(status), self.path, sent if not head_only else content_length)


class LocalDropHost:
    def __init__(self) -> None:
        self.session: SessionState | None = None
        self.tunnel_process: subprocess.Popen[str] | None = None
        self.tunnel_url: str | None = None
        self._tunnel_lock = threading.Lock()
        self._closed = False

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("v") != PROTOCOL_VERSION:
            raise ValueError("Unsupported protocol version")
        command = message.get("type")
        payload = message.get("payload") or {}
        if not isinstance(command, str) or not isinstance(payload, dict):
            raise ValueError("Invalid command envelope")

        handlers = {
            "hello": self.hello,
            "status": self.status,
            "create_session": self.create_session,
            "begin_file": self.begin_file,
            "file_chunk": self.file_chunk,
            "end_file": self.end_file,
            "cancel_file": self.cancel_file,
            "finalize": self.finalize,
            "start_tunnel": self.start_tunnel,
            "stop_tunnel": self.stop_tunnel,
            "clear_session": self.clear_session,
        }
        handler = handlers.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")
        return handler(payload)

    def hello(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "host": HOST_NAME,
            "protocol": PROTOCOL_VERSION,
            "python": sys.version.split()[0],
            "cloudflared": self._cloudflared_path(),
            "limits": {"maxFileBytes": MAX_FILE_SIZE, "maxSessionBytes": MAX_SESSION_SIZE},
        }

    def status(self, _payload: dict[str, Any]) -> dict[str, Any]:
        session = self.session
        return {
            "session": self._session_summary(session) if session else None,
            "tunnel": {
                "running": self.tunnel_process is not None and self.tunnel_process.poll() is None,
                "publicUrl": f"{self.tunnel_url}/{session.token}/" if self.tunnel_url and session else None,
            },
            "cloudflared": self._cloudflared_path(),
        }

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._cleanup_session()
        spa_fallback = bool(payload.get("spaFallback", False))
        allow_cors = bool(payload.get("allowCors", False))
        session_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(24)
        root = Path(tempfile.mkdtemp(prefix=f"localdrop-{session_id}-"))

        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalDropRequestHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="localdrop-http", daemon=True)
        session = SessionState(
            session_id=session_id,
            token=token,
            root=root,
            server=server,
            server_thread=thread,
            port=int(server.server_address[1]),
            spa_fallback=spa_fallback,
            allow_cors=allow_cors,
        )
        server.session = session  # type: ignore[attr-defined]
        self.session = session
        thread.start()
        send_event("session_started", session=self._session_summary(session))
        return self._session_summary(session)

    def begin_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session()
        file_id = str(payload.get("fileId", ""))
        relative = safe_relative_path(str(payload.get("path", "")))
        size = int(payload.get("size", -1))
        if not file_id or file_id in session.uploads:
            raise ValueError("Invalid or duplicate file ID")
        if size < 0 or size > MAX_FILE_SIZE:
            raise ValueError("File exceeds the 2 GiB limit")
        if session.total_bytes + size > MAX_SESSION_SIZE:
            raise ValueError("Session exceeds the 5 GiB limit")

        final_path = session.root / relative
        if not is_within(final_path, session.root):
            raise ValueError("Unsafe file path")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = final_path.with_name(final_path.name + f".{file_id}.part")
        stream = temp_path.open("wb")
        session.uploads[file_id] = UploadState(
            file_id=file_id,
            relative_path=relative,
            expected_size=size,
            temp_path=temp_path,
            final_path=final_path,
            stream=stream,
        )
        return {"fileId": file_id, "path": relative.as_posix(), "accepted": True}

    def file_chunk(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session()
        file_id = str(payload.get("fileId", ""))
        upload = session.uploads.get(file_id)
        if upload is None:
            raise ValueError("Unknown file upload")
        sequence = int(payload.get("sequence", -1))
        if sequence != upload.next_sequence:
            raise ValueError(f"Unexpected chunk sequence {sequence}; expected {upload.next_sequence}")
        data_raw = payload.get("data")
        if not isinstance(data_raw, str):
            raise ValueError("Chunk data must be base64 text")
        try:
            data = base64.b64decode(data_raw, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Invalid base64 chunk") from exc
        if upload.received + len(data) > upload.expected_size:
            raise ValueError("Chunk exceeds declared file size")
        upload.stream.write(data)
        upload.hasher.update(data)
        upload.received += len(data)
        upload.next_sequence += 1
        return {"fileId": file_id, "sequence": sequence, "received": upload.received}

    def end_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session()
        file_id = str(payload.get("fileId", ""))
        upload = session.uploads.pop(file_id, None)
        if upload is None:
            raise ValueError("Unknown file upload")
        upload.stream.flush()
        os.fsync(upload.stream.fileno())
        upload.stream.close()
        if upload.received != upload.expected_size:
            with contextlib.suppress(OSError):
                upload.temp_path.unlink()
            raise ValueError(f"Size mismatch: received {upload.received}, expected {upload.expected_size}")
        os.replace(upload.temp_path, upload.final_path)
        session.total_bytes += upload.received
        session.file_count += 1
        digest = upload.hasher.hexdigest()
        send_event(
            "file_ready",
            path=upload.relative_path.as_posix(),
            size=upload.received,
            sha256=digest,
        )
        return {
            "fileId": file_id,
            "path": upload.relative_path.as_posix(),
            "size": upload.received,
            "sha256": digest,
        }

    def cancel_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session()
        file_id = str(payload.get("fileId", ""))
        upload = session.uploads.pop(file_id, None)
        if upload:
            with contextlib.suppress(Exception):
                upload.stream.close()
            with contextlib.suppress(OSError):
                upload.temp_path.unlink()
        return {"fileId": file_id, "cancelled": bool(upload)}

    def finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session()
        if session.uploads:
            raise ValueError("Cannot finalize while files are uploading")
        session.spa_fallback = bool(payload.get("spaFallback", session.spa_fallback))
        session.allow_cors = bool(payload.get("allowCors", session.allow_cors))
        session.finalized = True
        summary = self._session_summary(session)
        send_event("session_ready", session=summary)
        return summary

    def start_tunnel(self, _payload: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session()
        if not session.finalized:
            raise ValueError("Finalize the file set before starting a public tunnel")
        with self._tunnel_lock:
            if self.tunnel_process and self.tunnel_process.poll() is None and self.tunnel_url:
                return {"publicUrl": f"{self.tunnel_url}/{session.token}/", "running": True}

            executable = self._cloudflared_path()
            if not executable:
                raise RuntimeError("cloudflared is not installed or not on PATH")

            command = [executable, "tunnel", "--url", f"http://127.0.0.1:{session.port}", "--no-autoupdate"]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            self.tunnel_process = process
            self.tunnel_url = None
            found: queue.Queue[str] = queue.Queue(maxsize=1)

            def read_output() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    match = TUNNEL_URL_RE.search(line)
                    if match and self.tunnel_url is None:
                        self.tunnel_url = match.group(0).rstrip("/")
                        with contextlib.suppress(queue.Full):
                            found.put_nowait(self.tunnel_url)
                    if "ERR" in line or "error" in line.lower():
                        send_event("tunnel_log", line=line.strip()[:500])
                code = process.wait()
                send_event("tunnel_stopped", exitCode=code)
                with self._tunnel_lock:
                    if self.tunnel_process is process:
                        self.tunnel_process = None
                        self.tunnel_url = None

            threading.Thread(target=read_output, name="localdrop-tunnel", daemon=True).start()

        try:
            base_url = found.get(timeout=25)
        except queue.Empty as exc:
            self._stop_tunnel_process()
            raise RuntimeError("Timed out while creating the Cloudflare Quick Tunnel") from exc

        public_url = f"{base_url}/{session.token}/"
        send_event("tunnel_started", publicUrl=public_url)
        return {"publicUrl": public_url, "running": True}

    def stop_tunnel(self, _payload: dict[str, Any]) -> dict[str, Any]:
        stopped = self._stop_tunnel_process()
        return {"stopped": stopped}

    def clear_session(self, _payload: dict[str, Any]) -> dict[str, Any]:
        self._cleanup_session()
        send_event("session_cleared")
        return {"cleared": True}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cleanup_session()

    def _require_session(self) -> SessionState:
        if self.session is None:
            raise RuntimeError("No active session")
        return self.session

    def _session_summary(self, session: SessionState) -> dict[str, Any]:
        return {
            "id": session.session_id,
            "localUrl": session.local_url,
            "port": session.port,
            "fileCount": session.file_count,
            "totalBytes": session.total_bytes,
            "spaFallback": session.spa_fallback,
            "allowCors": session.allow_cors,
            "finalized": session.finalized,
        }

    def _cloudflared_path(self) -> str | None:
        configured = os.environ.get("LOCALDROP_CLOUDFLARED")
        candidates = [
            configured,
            shutil.which("cloudflared"),
            "/opt/homebrew/bin/cloudflared",
            "/usr/local/bin/cloudflared",
            str(Path.home() / ".local" / "bin" / "cloudflared"),
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Cloudflare" / "cloudflared.exe") if os.name == "nt" else None,
            str(Path(os.environ.get("ProgramFiles", "")) / "cloudflared" / "cloudflared.exe") if os.name == "nt" else None,
            str(Path(os.environ.get("ProgramFiles(x86)", "")) / "cloudflared" / "cloudflared.exe") if os.name == "nt" else None,
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        return None

    def _stop_tunnel_process(self) -> bool:
        with self._tunnel_lock:
            process = self.tunnel_process
            self.tunnel_process = None
            self.tunnel_url = None
        if process is None or process.poll() is not None:
            return False
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return True

    def _cleanup_session(self) -> None:
        self._stop_tunnel_process()
        session = self.session
        self.session = None
        if session is None:
            return
        for upload in list(session.uploads.values()):
            with contextlib.suppress(Exception):
                upload.stream.close()
        session.uploads.clear()
        with contextlib.suppress(Exception):
            session.server.shutdown()
        with contextlib.suppress(Exception):
            session.server.server_close()
        with contextlib.suppress(Exception):
            session.server_thread.join(timeout=2)
        shutil.rmtree(session.root, ignore_errors=True)



def cleanup_stale_temp_dirs(max_age_seconds: int = 24 * 60 * 60) -> None:
    cutoff = time.time() - max_age_seconds
    temp_root = Path(tempfile.gettempdir())
    for candidate in temp_root.glob("localdrop-*-*"):
        try:
            if candidate.is_dir() and candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)
        except OSError:
            continue


def main() -> int:
    _set_windows_binary_mode()
    cleanup_stale_temp_dirs()
    host = LocalDropHost()

    def terminate(_signum: int, _frame: Any) -> None:
        host.close()
        raise SystemExit(0)

    if os.name != "nt":
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)

    try:
        while True:
            message = read_native_message()
            if message is None:
                break
            request_id = message.get("id")
            try:
                result = host.handle(message)
                WRITER.send({"v": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result})
            except Exception as exc:  # noqa: BLE001
                WRITER.send(
                    {
                        "v": PROTOCOL_VERSION,
                        "id": request_id,
                        "ok": False,
                        "error": {"code": exc.__class__.__name__, "message": str(exc)},
                    }
                )
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
