"""The outermost raw-ASGI diagnostic never logs a credential from the query.

``backend/main.py`` wraps the FastAPI app and, when ``NAHLA_RAW_ASGI_LOG`` is
on, logs a preview of every request's query string. The preview must be
redacted (and only then truncated) so an OAuth callback's ``code``/``state``
never reaches the line. Runs in a subprocess (importing ``main`` is heavy and
process-global); credentials are generated at runtime and never leave it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

_PROBE = r"""
import asyncio, secrets, sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/backend"]
import main

code, state = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
captured = []

class _Logger:
    def warning(self, msg, *args, **kwargs):
        captured.append(msg % args if args else msg)

async def _downstream(scope, receive, send):
    return None

main.logger = _Logger()
main._FASTAPI_APPLICATION = _downstream
scope = {"type": "http", "method": "GET", "path": "/merchant/catalog/meta-consent/callback",
         "query_string": f"co%64e={code}&state={state}&x=1".encode(), "client": ("203.0.113.9", 1),
         "scheme": "https", "http_version": "1.1"}
asyncio.run(main.app(scope, None, None))
assert captured and "[RAW_ASGI]" in captured[0], captured
assert code[:8] not in captured[0] and state[:8] not in captured[0], captured[0]
print("ok")
"""


def test_raw_asgi_diagnostic_redacts_the_query_before_logging():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["NAHLA_RAW_ASGI_LOG"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(_REPO)],
        cwd=str(_REPO), env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0 and proc.stdout.strip().endswith("ok"), proc.stderr[-3000:]
