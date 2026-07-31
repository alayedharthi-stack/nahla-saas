#!/usr/bin/env python3
"""Non-production probe for gpt-5.6-luna / terra / sol OpenAI access."""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from modules.ai.orchestrator.customer_chat_models import (  # noqa: E402
    MODEL_LUNA,
    MODEL_SOL,
    MODEL_TERRA,
)

MODELS = (MODEL_LUNA, MODEL_TERRA, MODEL_SOL)


def main() -> int:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
    report: dict = {
        "status": "BLOCKED",
        "missing_secret": None,
        "api_base": api_base,
        "models": {},
    }
    if not api_key:
        report["missing_secret"] = "OPENAI_API_KEY"
        print(json.dumps(report, indent=2))
        return 2

    try:
        import httpx
    except ImportError:
        report["missing_secret"] = "httpx"
        print(json.dumps(report, indent=2))
        return 2

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    ok = True
    with httpx.Client(timeout=30.0) as client:
        for model in MODELS:
            entry = {"ok": False, "error": None, "reply_preview": ""}
            try:
                resp = client.post(
                    f"{api_base}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Reply with exactly: probe-ok"}],
                        "max_completion_tokens": 8,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                text = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                entry["ok"] = bool(str(text).strip())
                entry["reply_preview"] = str(text).strip()[:40]
            except Exception as exc:  # noqa: BLE001
                entry["error"] = type(exc).__name__
                ok = False
            report["models"][model] = entry

    report["status"] = "OK" if ok else "PARTIAL_OR_FAILED"
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
