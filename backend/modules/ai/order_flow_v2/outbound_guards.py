"""Post-process deterministic OrderFlowV2 outbound replies before WhatsApp send."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def apply_order_flow_v2_outbound_guards(
    reply: str,
    *,
    db: Any,
    tenant_id: int,
    conversation_id: Optional[int] = None,
    order_prep: Optional[Dict[str, Any]] = None,
    known_facts: Optional[Dict[str, Any]] = None,
    missing_fields: Optional[List[str]] = None,
) -> str:
    text = str(reply or "")
    if not text.strip():
        return text

    try:
        from modules.ai.brain.postprocess.payment_credential_guard import (  # noqa: PLC0415
            apply_payment_credential_guard,
        )

        pcg = apply_payment_credential_guard(
            text,
            db=db,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        if pcg.replaced:
            text = pcg.reply
    except Exception:  # noqa: BLE001  # noqa: silent-ok — outbound guard belt must not block V2 send
        pass

    try:
        from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
            sanitize_forbidden_catalog_name_question,
        )

        text = sanitize_forbidden_catalog_name_question(
            text,
            known_facts=known_facts,
            missing_fields=missing_fields,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — outbound guard belt must not block V2 send
        pass

    try:
        from modules.ai.brain.postprocess.saudi_dialect_guard import apply_saudi_dialect_guard  # noqa: PLC0415

        sdg = apply_saudi_dialect_guard(
            text,
            locale="ar",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )
        if sdg.replaced:
            text = sdg.reply
    except Exception:  # noqa: BLE001  # noqa: silent-ok — outbound guard belt must not block V2 send
        pass

    _ = order_prep  # reserved for future checkout-scoped guards
    return text


__all__ = ["apply_order_flow_v2_outbound_guards"]
