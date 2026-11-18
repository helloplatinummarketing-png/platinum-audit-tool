"""POST /api/audit - run the selected audit and return the scores as JSON."""

from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _core import AuditError, compute, model_name, to_json  # noqa: E402

MAX_BODY = 256 * 1024


class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Health check, so a deploy can be verified without filling the form."""
        self._send(
            200,
            {
                "ok": True,
                "service": "platinum-audit",
                "model": model_name(),
                "gemini_key_set": bool((os.getenv("GEMINI_API_KEY") or "").strip()),
            },
        )

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

            self._send(200, to_json(compute(payload)))
        except AuditError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "Request body is not valid JSON."})
        except Exception:
            traceback.print_exc()
            self._send(
                500,
                {
                    "ok": False,
                    "error": "The audit failed to run. Check the function logs.",
                },
            )

    def log_message(self, fmt, *args):  # quieter function logs
        return
