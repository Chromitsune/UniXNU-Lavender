#!/usr/bin/env python3
"""Exercise ipsw-get against a tiny local ranged-ZIP server (no Apple/network downloads)."""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "out/lavender/image-tools-target/release/lavender-image-tools"
PAYLOAD = b"lavender local IPSW-range test payload\n"


def main() -> int:
    if not TOOL.is_file():
        raise SystemExit(f"missing {TOOL}; build with scripts/build-lavender-image-tools.sh")
    (ROOT / "out/lavender").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ipsw-range-test-", dir=ROOT / "out/lavender") as tmp:
        temp = Path(tmp)
        archive = temp / "fixture.ipsw"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("Firmware/test.im4p", PAYLOAD)
        data = archive.read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                raw = self.headers.get("Range", "")
                if not raw.startswith("bytes="):
                    start, end, status = 0, len(data) - 1, 200
                else:
                    lo, hi = raw.removeprefix("bytes=").split("-", 1)
                    start, end, status = int(lo), int(hi), 206
                if start < 0 or end >= len(data) or start > end:
                    self.send_error(416)
                    return
                body = data[start:end + 1]
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            out = temp / "out"
            url = f"http://127.0.0.1:{server.server_port}/fixture.ipsw"
            subprocess.run([str(TOOL), "ipsw-get", url, str(out), "test.im4p", "--jobs", "1", "--chunk-mib", "1"], check=True)
            got = (out / "test.im4p").read_bytes()
            if got != PAYLOAD:
                raise SystemExit(f"ipsw-get output mismatch: {len(got)} bytes")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("PASS: ranged IPSW member download, deflate, and CRC-32 using local fixture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
