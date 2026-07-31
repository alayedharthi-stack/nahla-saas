#!/usr/bin/env python3
"""Same-payload comparison: claude-haiku baseline vs gpt-5.6-luna vs terra."""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from modules.ai.orchestrator.customer_chat_models import MODEL_LUNA, MODEL_TERRA  # noqa: E402

BASELINE_PAYLOAD = {
    "system": "أنت مساعد متجر. أجب بجملة واحدة فقط.",
    "user": "هل عندكم حذاء رياضي أبيض مقاس 42؟",
}

COMPARE_TARGETS = [
    ("anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY"),
    ("openai", MODEL_LUNA, "OPENAI_API_KEY"),
    ("openai", MODEL_TERRA, "OPENAI_API_KEY"),
]


def _call_openai(model: str, api_key: str, api_base: str) -> dict:
    import httpx

    resp = httpx.post(
        f"{api_base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": BASELINE_PAYLOAD["system"]},
                {"role": "user", "content": BASELINE_PAYLOAD["user"]},
            ],
            "max_tokens": 120,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return {"reply_len": len(text), "reply_preview": text[:120]}


def _call_anthropic(model: str, api_key: str) -> dict:
    import httpx

    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 120,
            "system": BASELINE_PAYLOAD["system"],
            "messages": [{"role": "user", "content": BASELINE_PAYLOAD["user"]}],
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    return {"reply_len": len(text), "reply_preview": text[:120]}


def main() -> int:
    report: dict = {
        "status": "BLOCKED",
        "payload": BASELINE_PAYLOAD,
        "results": {},
        "missing_secrets": [],
    }
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    anthropic_key = (
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("CLAUDE_API_KEY", "").strip()
    )
    api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")

    for provider, model, secret_name in COMPARE_TARGETS:
        key = openai_key if secret_name == "OPENAI_API_KEY" else anthropic_key
        label = f"{provider}:{model}"
        if not key:
            report["missing_secrets"].append(secret_name)
            report["results"][label] = {"status": "BLOCKED", "missing_secret": secret_name}
            continue
        try:
            if provider == "openai":
                result = _call_openai(model, key, api_base)
            else:
                result = _call_anthropic(model, key)
            report["results"][label] = {"status": "OK", **result}
        except Exception as exc:  # noqa: BLE001
            report["results"][label] = {"status": "ERROR", "error": type(exc).__name__}

    if report["missing_secrets"]:
        report["status"] = "BLOCKED"
    elif all(r.get("status") == "OK" for r in report["results"].values()):
        report["status"] = "OK"
    else:
        report["status"] = "PARTIAL"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
