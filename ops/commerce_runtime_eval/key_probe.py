"""One minimal model call; prints only the API's own status and error message.

The key is read by the SDK from ANTHROPIC_API_KEY and never printed; any echo
of it in an error message is scrubbed.
"""
import json
import os

import anthropic

key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY") or ""
out = {"probe": "anthropic_minimal_call", "model": os.environ.get("EVAL_MODEL", "")}
try:
    client = anthropic.Anthropic(api_key=key, max_retries=0)
    r = client.messages.create(model=out["model"], max_tokens=5,
                               messages=[{"role": "user", "content": "ping"}])
    out["status"] = "ok"
    out["stop_reason"] = r.stop_reason
except anthropic.APIStatusError as exc:
    out["status"] = "api_status_error"
    out["http_status"] = exc.status_code
    body = getattr(exc, "body", None)
    err = body.get("error", {}) if isinstance(body, dict) else {}
    out["error_type"] = err.get("type")
    out["error_message"] = str(err.get("message") or exc.message)[:300]
except Exception as exc:  # noqa: BLE001
    out["status"] = "exception"
    out["error_type"] = type(exc).__name__
line = json.dumps(out, ensure_ascii=False)
if key:
    line = line.replace(key, "[redacted]")
print("KEY_PROBE=" + line, flush=True)
