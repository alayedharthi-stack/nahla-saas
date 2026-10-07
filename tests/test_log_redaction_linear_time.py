"""Redaction must stay linear on adversarial free text (no regex backtracking blowup).

Log records, Sentry breadcrumbs and request query strings are attacker-
influenced. Every input below is fed through the real redaction entry points
in a fresh subprocess with a hard timeout; a backtracking pattern would blow
the budget by orders of magnitude (``"%ab" * 24`` alone took more than 2 s
with the earlier encoded-key pattern). Credentials are generated at runtime.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

_PROBE = r"""
import json, secrets, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/backend"]
from core.log_redaction import redact_raw_query, redact_secrets, redacted_query_preview
from core.observability_sentry import _before_breadcrumb, _before_send

n = int(sys.argv[2])
code = secrets.token_urlsafe(24)
cases = {
    "pct_12": "%ab" * 12,
    "pct_24": "%ab" * 24,
    "pct_run": "%ab" * n,
    "pct_run_eq": "%ab" * n + "=" + code,
    "pct_key_chain": "a%" * n + "=" + code,
    "slash_pct": "/%" * n,
    "slash_pct_query": "/%" * n + "?co%64e=" + code,
    "secret_run": "secret" * n + "=" + code,
    "enc_key_run": "enc_key" * n + "=" + code,
    "token_run": "token_" * n,
    "spaces": "code" + " " * n + "=" + code,
    "fernet_run": "A" * (43 * (n // 40 + 1)),
    "query": "&".join(f"k{i}%41=v{i}" for i in range(n // 4)) + "&co%64e=" + code,
}
times = {}
for name, text in cases.items():
    t = time.perf_counter()
    out = redact_secrets(text)
    redacted_query_preview(text, limit=200)
    redact_raw_query(text)
    _before_send({"request": {"query_string": text, "url": "https://h.example/p?" + text},
                  "breadcrumbs": {"values": [{"data": {"http.query": text}, "message": text}]}}, {})
    _before_breadcrumb({"data": {"http.query": text}}, {})
    times[name] = time.perf_counter() - t
    if name in ("pct_run_eq", "pct_key_chain", "slash_pct_query", "secret_run", "enc_key_run", "spaces", "query"):
        assert code not in out, name
print(json.dumps(times))
"""


def test_redaction_is_linear_on_adversarial_inputs():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(_REPO), "20000"],
        cwd=str(_REPO), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    times = json.loads(proc.stdout.strip().splitlines()[-1])
    # Linear scans finish these in well under a second each on CI hardware;
    # a backtracking pattern takes seconds for 72 bytes and hours for these.
    slow = {name: t for name, t in times.items() if t > 5.0}
    assert not slow, slow
    assert times["pct_24"] < 0.5 and times["pct_12"] < 0.5, times
