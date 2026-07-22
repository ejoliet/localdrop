from __future__ import annotations

import json
import struct
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "native" / "localdrop_host.py"


def write_message(stream, message: dict) -> None:
    encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("@I", len(encoded)))
    stream.write(encoded)
    stream.flush()


def read_message(stream) -> dict:
    raw = stream.read(4)
    if len(raw) != 4:
        raise EOFError("missing response length")
    (length,) = struct.unpack("@I", raw)
    payload = stream.read(length)
    return json.loads(payload.decode("utf-8"))


class NativeProtocolTests(unittest.TestCase):
    def test_hello_round_trip(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(HOST)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin and process.stdout
        try:
            write_message(process.stdin, {"v": 1, "id": "hello-1", "type": "hello", "payload": {}})
            response = read_message(process.stdout)
            self.assertEqual(response["id"], "hello-1")
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"]["host"], "com.localdrop.live")
        finally:
            process.stdin.close()
            process.wait(timeout=5)
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            if process.returncode != 0:
                self.fail(f"native host exited with {process.returncode}: {stderr}")


if __name__ == "__main__":
    unittest.main()
