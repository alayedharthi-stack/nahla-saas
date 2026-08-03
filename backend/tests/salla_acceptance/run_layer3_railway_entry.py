"""
Layer 3 Railway entry — strip production data/channel vars BEFORE imports.

``railway run`` injects OPENAI_API_KEY (kept) and often DATABASE_URL (removed).
Run as: ``python -m tests.salla_acceptance.run_layer3_railway_entry``
"""
from __future__ import annotations

import os
import sys

_STRIP = (
    "DATABASE_URL",
    "DATABASE_PUBLIC_URL",
    "POSTGRES_URL",
    "REDIS_URL",
    "REDIS_PUBLIC_URL",
    "WHATSAPP_TOKEN",
    "WA_TOKEN",
    "D360_API_KEY",
    "META_WHATSAPP_TOKEN",
    "SENTRY_DSN",
)

for _key in _STRIP:
    os.environ.pop(_key, None)

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("NAHLA_MODEL_ROUTER_ENABLED", "true")
os.environ.setdefault("ALLOW_PREMIUM_MODEL", "false")
os.environ.setdefault("ORDER_FLOW_V2_ENABLED", "false")
os.environ.setdefault("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

if not (os.environ.get("OPENAI_API_KEY") or "").strip():
    print("OPENAI_API_KEY=ABSENT", flush=True)
    raise SystemExit(2)
print("OPENAI_API_KEY=PRESENT", flush=True)
print("DATABASE_URL=sqlite_memory_override", flush=True)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_BACKEND, os.path.join(_BACKEND, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.salla_acceptance.run_layer3_dialogue import main  # noqa: E402

raise SystemExit(main())
