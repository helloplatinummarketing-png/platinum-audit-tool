"""POST /api/download - rebuild the audit as a branded PDF.

The request carries the same answers as /api/audit plus the AI result that run
returned, so the PDF matches what was shown on screen and no second Gemini
call is made.
"""

from __future__ import annotations

import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _core import AuditError, compute, to_pdf  # noqa: E402

MAX_BODY = 256 * 1024


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("content-length") or 0)
            if length <= 0:
                raise AuditError("Empty request body.")
            if length > MAX_BODY:
                raise AuditError("Request body too large.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise AuditError("Request body must be a JSON object.")

            name, pdf = to_pdf(compute(payload))

            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(pdf)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(pdf)
        except AuditError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "Request body is not valid JSON."})
        except Exception:
            traceback.print_exc()
            self._send_json(
                500,
                {"ok": False, "error": "The PDF failed to build. Check the function logs."},
            )

    def log_message(self, fmt, *args):
        return
