"""Staging-safe conditional-coupon shadow observation probe (read-only, closed JSON).

Deterministic Layer 0 observation helpers for operators and CI. Does not enable
runtime flags, call compose/providers, or materialise coupons for customers.
"""
from __future__ import annotations

import importlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from scripts.operators.customer_conditional_coupon_shadow_observation_contract import (
    APP_CONTAINER_SYS_PATH,
    CODE_COMPOSE_FLAG_ENABLED,
    CODE_FIXTURE_CONVERSATION_MISSING,
    CODE_FIXTURE_CONVERSATION_NOT_FOUND,
    CODE_SESSION_LOCAL_IMPORT_INVALID,
    CODE_SHADOW_FLAG_NOT_ENABLED,
    DEPLOYMENT_APP_ROOT,
    INVALID_LEGACY_SESSION_LOCAL_IMPORT,
    OBSERVATION_PROBE_MESSAGE,
    REPORT_SCHEMA_VERSION,
    SESSION_LOCAL_MODULE,
    VALID_SESSION_LOCAL_IMPORT,
)


def resolve_app_root(artifact_root: Path | None = None) -> Path:
    """Map a repo checkout to the container ``/app`` artifact root."""
    root = (artifact_root or Path(__file__).resolve().parents[2]).resolve()
    if (root / "backend").is_dir():
        return root
    if root.name == "backend" and root.parent.is_dir():
        return root.parent
    raise ValueError("artifact_root_invalid")


def app_container_sys_path_entries(app_root: Path | None = None) -> list[str]:
    """Filesystem paths mirroring deployed ``sys.path`` entries under ``/app``."""
    root = resolve_app_root(app_root)
    return [str(root), str(root / "backend"), str(root / "database")]


@contextmanager
def with_app_container_paths(app_root: Path | None = None) -> Iterator[Path]:
    """Install ``/app``-style ``sys.path`` for the duration of a probe."""
    root = resolve_app_root(app_root)
    entries = app_container_sys_path_entries(root)
    saved = list(sys.path)
    try:
        sys.path[:] = [entry for entry in sys.path if entry not in entries]
        sys.path[:0] = entries
        yield root
    finally:
        sys.path[:] = saved


def verify_session_local_import() -> dict[str, Any]:
    """Regression guard: legacy ``from database import SessionLocal`` fails in ``/app``."""
    try:
        importlib.import_module("database")
    except Exception:  # noqa: BLE001
        pass
    legacy_ok = True
    try:
        database = importlib.import_module("database")
        legacy_ok = hasattr(database, "SessionLocal")
    except Exception:  # noqa: BLE001
        legacy_ok = False

    session_module = importlib.import_module(SESSION_LOCAL_MODULE)
    session_ok = hasattr(session_module, "SessionLocal")

    return {
        "legacy_database_package_import_ok": legacy_ok,
        "session_local_import_ok": session_ok,
        "valid_import": VALID_SESSION_LOCAL_IMPORT,
        "invalid_legacy_import": INVALID_LEGACY_SESSION_LOCAL_IMPORT,
    }


def execute_default_off_probe(
    *,
    message: str | None = None,
    app_root: Path | None = None,
) -> dict[str, Any]:
    """Default-off loader contract: zero DB I/O when the shadow flag is unset."""
    with with_app_container_paths(app_root):
        from modules.ai.brain.truth_surface.customer_conditional_coupon_loader import (
            load_customer_conditional_coupon_facts,
        )
        from modules.ai.brain.truth_surface.flags import (
            is_customer_conditional_coupon_shadow_enabled,
        )

        facts, telemetry = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=1,
            message=message or OBSERVATION_PROBE_MESSAGE,
        )
        zero_io = (
            telemetry.get("gate_skipped_reason") == "shadow_flag_disabled"
            and telemetry.get("order_count_query_count") == 0
            and telemetry.get("usage_evidence_query_count") == 0
            and len(facts) == 0
        )
        return {
            "ok": True,
            "phase": "default_off_verify",
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "shadow_enabled": is_customer_conditional_coupon_shadow_enabled(),
            "facts_count": len(facts),
            "telemetry": telemetry,
            "zero_io_contract": zero_io,
        }


def _compose_flags_enabled() -> bool:
    from modules.ai.brain.truth_surface.flags import (
        is_general_offer_discovery_compose_enabled,
        is_product_sale_offer_compose_enabled,
        is_trusted_context_coupon_offer_compose_enabled,
    )

    return any(
        fn()
        for fn in (
            is_trusted_context_coupon_offer_compose_enabled,
            is_product_sale_offer_compose_enabled,
            is_general_offer_discovery_compose_enabled,
        )
    )


def _resolve_fixture_conversation(db: Any, *, tenant_id: int) -> Any:
    from database.session import SessionLocal  # noqa: F401 — import regression anchor
    from models import Conversation
    from services.customer_conditional_coupon_shadow_fixture_contract import (
        FIXTURE_MARKER_FIELD,
        FIXTURE_NAMESPACE,
    )

    _ = SessionLocal  # exercised for deploy-layout importability
    conversation_id = db.execute(
        text(
            """
            SELECT id FROM conversations
            WHERE tenant_id = :tenant_id
              AND metadata ->> :marker_key = :marker_value
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "marker_key": FIXTURE_MARKER_FIELD,
            "marker_value": FIXTURE_NAMESPACE,
        },
    ).scalar()
    if not conversation_id:
        return None
    return db.get(Conversation, int(conversation_id))


def execute_shadow_observation_probe(
    db: Any,
    *,
    tenant_id: int = 1,
    message: str | None = None,
    conversation: Any | None = None,
    app_root: Path | None = None,
) -> dict[str, Any]:
    """Process-scoped shadow observation — Layer 0 facts only, no materialisation."""
    with with_app_container_paths(app_root):
        from modules.ai.brain.truth_surface.customer_conditional_coupon_loader import (
            load_customer_conditional_coupon_facts,
        )
        from modules.ai.brain.truth_surface.flags import (
            is_customer_conditional_coupon_shadow_enabled,
        )

        if not is_customer_conditional_coupon_shadow_enabled():
            return {"ok": False, "code": CODE_SHADOW_FLAG_NOT_ENABLED}
        if _compose_flags_enabled():
            return {"ok": False, "code": CODE_COMPOSE_FLAG_ENABLED}

        resolved = conversation if conversation is not None else _resolve_fixture_conversation(
            db,
            tenant_id=tenant_id,
        )
        if resolved is None:
            if conversation is None:
                return {"ok": False, "code": CODE_FIXTURE_CONVERSATION_MISSING}
            return {"ok": False, "code": CODE_FIXTURE_CONVERSATION_NOT_FOUND}

        materialise_called = False

        def _track_materialise(*_args: Any, **_kwargs: Any) -> MagicMock:
            nonlocal materialise_called
            materialise_called = True
            return MagicMock()

        with patch(
            "services.promotion_engine.materialise_for_customer",
            side_effect=_track_materialise,
        ):
            facts, telemetry = load_customer_conditional_coupon_facts(
                db=db,
                tenant_id=tenant_id,
                message=message or OBSERVATION_PROBE_MESSAGE,
                conversation=resolved,
            )

        bridge_outcome = "unresolved"
        if facts:
            identity = facts[0].value.get("identity_status")
            if identity == "resolved":
                bridge_outcome = "resolved"
            elif identity == "ambiguous":
                bridge_outcome = "ambiguous"

        return {
            "ok": True,
            "phase": "shadow_observation",
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "facts_count": len(facts),
            "fact_record": facts[0].value if facts else None,
            "telemetry": telemetry,
            "subject_bridge_outcome": bridge_outcome,
            "guards": {
                "materialise_for_customer_called": materialise_called,
                "compose_flags_enabled": False,
            },
        }


def probe_source_uses_valid_session_local_import() -> bool:
    """Static check that this module never executes the legacy SessionLocal import."""
    import re

    source = Path(__file__).read_text(encoding="utf-8")
    legacy = re.compile(r"^\s*from database import SessionLocal\s*$", re.MULTILINE)
    valid = re.compile(r"^\s*from database\.session import SessionLocal", re.MULTILINE)
    return valid.search(source) is not None and legacy.search(source) is None


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["default-off"]:
            if not probe_source_uses_valid_session_local_import():
                _emit({"ok": False, "code": CODE_SESSION_LOCAL_IMPORT_INVALID})
                return 2
            with with_app_container_paths():
                import_check = verify_session_local_import()
                if not import_check["session_local_import_ok"]:
                    _emit({"ok": False, "code": CODE_SESSION_LOCAL_IMPORT_INVALID})
                    return 2
                _emit(execute_default_off_probe())
            return 0
        raise ValueError("command_invalid")
    except ValueError:
        _emit({"ok": False, "code": "command_invalid"})
        return 2
    except BaseException:
        _emit({"ok": False, "code": "probe_failed"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
