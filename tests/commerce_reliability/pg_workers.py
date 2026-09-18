"""Worker entry points executed in *separate spawned processes*.

Each worker opens its own PostgreSQL connection and calls the real runtime
function under test (``DefaultStateStore.save`` or ``claim_next_batch``).
Nothing here re-implements runtime logic; the barrier only lines the
processes up so their calls overlap.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _bootstrap() -> None:
    for entry in reversed([str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_REPO_ROOT / "database")]):
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)
    import observability  # noqa: F401,PLC0415
    import observability.event_logger  # noqa: F401,PLC0415


def state_save_worker(dsn: str, tenant_id: int, phone: str, field: str, value: Any, barrier, out) -> None:
    """Load brain state, mutate one field, wait for the barrier, save."""
    _bootstrap()
    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from modules.ai.brain.state.store import DefaultStateStore  # noqa: PLC0415

    engine = create_engine(dsn)
    db = sessionmaker(bind=engine)()
    try:
        store = DefaultStateStore()
        state = store.load(db, tenant_id, phone)
        setattr(state, field, value)
        barrier.wait(timeout=60)
        store.save(db, tenant_id, phone, state)
        out.put({"field": field, "status": "saved"})
    except Exception as exc:  # noqa: BLE001 — surfaced to the parent, never hidden
        out.put({"field": field, "status": f"error:{type(exc).__name__}:{exc}"})
    finally:
        db.close()
        engine.dispose()


def claim_worker(dsn: str, limit: int, barrier, out) -> None:
    """Claim a batch of webhook events with the real claim_next_batch."""
    _bootstrap()
    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from core.webhook_events import claim_next_batch  # noqa: PLC0415

    engine = create_engine(dsn)
    db = sessionmaker(bind=engine)()
    try:
        barrier.wait(timeout=60)
        rows = claim_next_batch(db, limit=limit)
        db.commit()
        out.put({"status": "claimed", "ids": [int(r.id) for r in rows]})
    except Exception as exc:  # noqa: BLE001 — surfaced to the parent, never hidden
        out.put({"status": f"error:{type(exc).__name__}:{exc}", "ids": []})
    finally:
        db.close()
        engine.dispose()
