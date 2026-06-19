"""
order_flow_evidence.py
Phase 1 — discover where customer-provided information was lost.

Emits structured log lines (no DB, no behavior change):
  [SLOT_CONSUME]      capture vs persistence
  [CONVERSATION_FOCUS] product / variant / qty / focus drift
  [ACK_DECISION]      important input vs outbound acknowledgement
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.observability.order_flow")

_GENERIC_ACK_MARKERS = (
    "تمام 🌷 وصلت رسالتك",
    "وصلت رسالتك",
    "حياك الله، وصلت رسالتك",
)

_INPUT_TYPE_NAME = "name"
_INPUT_TYPE_ADDRESS = "address"
_INPUT_TYPE_QUANTITY = "quantity"
_INPUT_TYPE_VARIANT = "variant"
_INPUT_TYPE_PAYMENT = "payment"
_INPUT_TYPE_LOCATION = "location"
_INPUT_TYPE_PRODUCT = "product"

_NAME_HINT_RE = re.compile(
    r"(?:اسمي|معك|أنا|انا)\s+\S+|"
    r"[\u0623\u0627\u0628]\u0648\s+\S+|"
    r"[\u0627\u0644\u0633\u064a\u062f]\s+\S+",
    re.UNICODE,
)
_QTY_HINT_RE = re.compile(
    r"\d+|"
    r"\u0643\u064a\u0644\u0648|\u0643\u064a\u0644\u064a|"
    r"\u0646\u0635|\u062d\u0628\u0629|\u062d\u0628\u062a\u064a\u0646|"
    r"\u0642\u0637\u0639\u0629|\u0631\u0628\u0639",
    re.UNICODE,
)
_ADDRESS_HINT_RE = re.compile(
    r"\b[A-Za-z]{4}\d{4}\b|"
    r"maps\.(?:google|app\.goo)|goo\.gl/maps|"
    r"\u0627\u0644\u0639\u0646\u0648\u0627\u0646|\u0627\u0644\u0631\u0645\u0632\s*\u0627\u0644\u0645\u062e\u062a\u0635\u0631|"
    r"\u062d\u064a\s+\S+|\u0645\u062f\u064a\u0646\u0629|\u0627\u0644\u0631\u064a\u0627\u0636",
    re.UNICODE | re.IGNORECASE,
)
_PAYMENT_HINT_RE = re.compile(
    r"\u062a\u0645\s*\u0627\u0644\u062f\u0641\u0639|\u062a\u0645\s*\u0627\u0644\u062a\u062d\u0648\u064a\u0644|"
    r"\u0625\u064a\u0635\u0627\u0644|\u0627\u0644\u062d\u0633\u0627\u0628|\u0627\u0644\u0625\u064a\u0635\u0627\u0644",
    re.UNICODE,
)
_STAFF_ROUTE_RE = re.compile(
    r"\u062a\u0648\u0627\u0635\u0644\s+\u0645\u0639|\u0623?\u0645\u064a\u0646|\u0628\u0627\u0626\u0639|\u0645\u0648\u0638\u0641",
    re.UNICODE,
)
_ORDER_CONTINUE_RE = re.compile(
    r"\u0623\u0643\u0645\u0644|\u0627\u0643\u0645\u0644|\u0627\u0644\u0637\u0644\u0628|\u0627\u0644\u0639\u0646\u0648\u0627\u0646|"
    r"\u0627\u0644\u0627\u0633\u0645|\u0627\u0644\u0643\u0645\u064a\u0629|\u0627\u0644\u0633\u0639\u0631|\u062a\u0645\s*\u062a\u0633\u062c\u064a\u0644|"
    r"\u062a\u0645\s*\u0627\u0644\u0627\u0633\u062a\u0644\u0627\u0645|\u064a\u0627\s+\S+|\u0627\u0644\u0644\u0647\s+\u064a\u062d\u064a\u064a\u0643",
    re.UNICODE,
)


@dataclass(frozen=True)
class FocusSnapshot:
    focus_title: str = ""
    product: str = ""
    variant: str = ""
    quantity: str = ""
    resolved_product: str = ""
    line_item_summary: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "focus_title": self.focus_title,
            "product": self.product,
            "variant": self.variant,
            "quantity": self.quantity,
            "resolved_product": self.resolved_product,
            "line_item_summary": self.line_item_summary,
        }


def _order_prep(state: Any) -> Any:
    if state is None:
        return None
    if isinstance(state, dict):
        return state.get("order_prep") or {}
    return getattr(state, "order_prep", None)


def _focus_dict(state: Any) -> Dict[str, Any]:
    if state is None:
        return {}
    if isinstance(state, dict):
        raw = state.get("current_product_focus")
    else:
        raw = getattr(state, "current_product_focus", None)
    return dict(raw or {}) if isinstance(raw, dict) else {}


def _line_items_summary(prep: Any) -> str:
    items: List[Any] = []
    if isinstance(prep, dict):
        items = list(prep.get("line_items") or prep.get("cart_items") or [])
    elif prep is not None:
        items = list(getattr(prep, "line_items", None) or getattr(prep, "cart_items", None) or [])
    parts: List[str] = []
    for it in items[:3]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("product_name") or it.get("name") or "").strip()
        var = str(it.get("variant") or it.get("variant_label") or "").strip()
        qty = it.get("quantity")
        chunk = name or "?"
        if var:
            chunk += f"/{var}"
        if qty is not None:
            chunk += f"×{qty}"
        parts.append(chunk)
    return "; ".join(parts)


def snapshot_focus(state: Any) -> FocusSnapshot:
    focus = _focus_dict(state)
    prep = _order_prep(state)
    product = str(focus.get("title") or focus.get("name") or "").strip()
    resolved = product
    variant = ""
    quantity = ""
    if prep is not None:
        if isinstance(prep, dict):
            if not resolved:
                resolved = str(prep.get("product_name") or prep.get("product_id") or "").strip()
            variant = str(prep.get("product_options") or prep.get("selected_variant") or "").strip()
            if not variant:
                opts = prep.get("product_options")
                if isinstance(opts, dict) and opts:
                    variant = json.dumps(opts, ensure_ascii=False)[:80]
            quantity = str(prep.get("quantity") or "")
        else:
            if not resolved:
                resolved = str(getattr(prep, "product_name", "") or getattr(prep, "product_id", "") or "").strip()
            quantity = str(getattr(prep, "quantity", "") or "")
            opts = dict(getattr(prep, "product_options", None) or {})
            if opts:
                variant = json.dumps(opts, ensure_ascii=False)[:80]
    line_summary = _line_items_summary(prep)
    if not resolved and line_summary:
        resolved = line_summary.split(";")[0].split("×")[0].split("/")[0].strip()
    return FocusSnapshot(
        focus_title=product,
        product=product or resolved,
        variant=variant,
        quantity=quantity,
        resolved_product=resolved,
        line_item_summary=line_summary,
    )


def infer_expected_slot(state: Any, intent: Any = None) -> Optional[str]:
    """Log-only mirror of checkout next-slot heuristics."""
    prep = _order_prep(state)
    if prep is None:
        return None
    if isinstance(prep, dict):
        missing = list(prep.get("missing_fields") or [])
        awaiting_variant = bool(prep.get("awaiting_variant_choice"))
        qty = int(prep.get("quantity") or 0)
    else:
        missing = list(getattr(prep, "missing_fields", None) or [])
        awaiting_variant = bool(getattr(prep, "awaiting_variant_choice", False))
        qty = int(getattr(prep, "quantity", 0) or 0)
    if awaiting_variant:
        return "variant"
    slots = dict(getattr(intent, "slots", None) or {}) if intent is not None else {}
    if qty <= 1 and not slots.get("quantity"):
        if snapshot_focus(state).line_item_summary:
            pass
        else:
            return "quantity"
    if missing:
        field = str(missing[0]).strip().lower()
        if field in {"customer_first_name", "customer_last_name", "customer_name"}:
            return "customer_name"
        if field in {"address", "address_location", "address_line", "city", "short_address_code", "google_maps_url"}:
            return "address"
        if field in {"payment", "payment_method"}:
            return "payment"
        return field
    return None


def detect_input_types(
    *,
    message: str = "",
    intent: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> List[str]:
    text = (message or "").strip()
    slots = dict(getattr(intent, "slots", None) or {}) if intent is not None else {}
    md = inbound_metadata or {}
    types: List[str] = []

    if slots.get("customer_name") or slots.get("customer_first_name") or slots.get("full_name"):
        types.append(_INPUT_TYPE_NAME)
    elif _NAME_HINT_RE.search(text) and len(text.split()) <= 6:
        types.append(_INPUT_TYPE_NAME)

    if (
        slots.get("short_address_code")
        or slots.get("google_maps_url")
        or slots.get("city")
        or slots.get("address_line")
    ):
        types.append(_INPUT_TYPE_ADDRESS)
    elif _ADDRESS_HINT_RE.search(text):
        types.append(_INPUT_TYPE_ADDRESS)

    if md.get("latitude") or md.get("longitude") or str(md.get("normalized_type") or "") == "location":
        types.append(_INPUT_TYPE_LOCATION)
    if str(md.get("image_kind") or "") in {"map_screenshot", "national_address_card"}:
        types.append(_INPUT_TYPE_LOCATION)
        types.append(_INPUT_TYPE_ADDRESS)

    if slots.get("quantity") or slots.get("product_query"):
        if slots.get("quantity"):
            types.append(_INPUT_TYPE_QUANTITY)
        if slots.get("product_query"):
            types.append(_INPUT_TYPE_PRODUCT)
    elif _QTY_HINT_RE.search(text) and not _ADDRESS_HINT_RE.search(text):
        types.append(_INPUT_TYPE_QUANTITY)

    if _PAYMENT_HINT_RE.search(text):
        types.append(_INPUT_TYPE_PAYMENT)

    # dedupe preserve order
    seen: set[str] = set()
    out: List[str] = []
    for t in types:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def is_generic_ack_stub(reply: str) -> bool:
    norm = (reply or "").strip()
    if not norm:
        return False
    for marker in _GENERIC_ACK_MARKERS:
        if marker in norm and len(norm) <= len(marker) + 24:
            return True
    if norm in _GENERIC_ACK_MARKERS:
        return True
    return False


def reply_acknowledges_important_input(
    *,
    reply: str,
    input_types: Sequence[str],
    state: Any = None,
    missing_fields: Optional[Sequence[str]] = None,
) -> bool:
    text = (reply or "").strip()
    if not text or is_generic_ack_stub(text):
        return False
    if _ORDER_CONTINUE_RE.search(text):
        return True
    prep = _order_prep(state)
    first = ""
    if prep is not None:
        if isinstance(prep, dict):
            first = str(prep.get("customer_first_name") or "").strip()
        else:
            first = str(getattr(prep, "customer_first_name", "") or "").strip()
    if _INPUT_TYPE_NAME in input_types and first and first in text:
        return True
    if missing_fields:
        mf = {str(x).strip().lower() for x in missing_fields}
        if _INPUT_TYPE_ADDRESS in input_types and mf & {
            "city", "address_location", "short_address_code", "google_maps_url",
        }:
            if re.search(r"\u0627\u0644\u0639\u0646\u0648\u0627\u0646|\u0627\u0644\u0631\u0645\u0632|\u0645\u0648\u0642\u0639|\u0627\u0644\u0645\u062f\u064a\u0646\u0629", text):
                return True
    if len(text) > 40 and not is_generic_ack_stub(text):
        return True
    return False


def infer_why_focus_changed(before: FocusSnapshot, after: FocusSnapshot) -> str:
    if before.as_dict() == after.as_dict():
        return "unchanged"
    if before.focus_title and not after.focus_title:
        return "focus_cleared"
    if not before.focus_title and after.focus_title:
        return "focus_set"
    if before.line_item_summary != after.line_item_summary:
        return "line_items_changed"
    if before.variant != after.variant:
        return "variant_changed"
    if before.quantity != after.quantity:
        return "quantity_changed"
    if before.resolved_product != after.resolved_product:
        return "resolved_product_changed"
    return "focus_drift"


def emit_slot_consume(
    *,
    tenant_id: Optional[int] = None,
    phone_tail: str = "",
    turn: Optional[int] = None,
    expected_slot: Optional[str] = None,
    received: Optional[Dict[str, Any]] = None,
    consumed: bool = False,
    reason: str = "",
    source: str = "",
    decision_action: str = "",
) -> None:
    try:
        logger.info(
            "[SLOT_CONSUME] tenant=%s phone=*%s turn=%s expected_slot=%s "
            "received=%s consumed=%s reason=%s source=%s decision_action=%s",
            tenant_id,
            phone_tail,
            turn if turn is not None else "-",
            expected_slot or "-",
            json.dumps(received or {}, ensure_ascii=False)[:240],
            consumed,
            reason or "-",
            source or "-",
            decision_action or "-",
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — evidence emit must not break turn
        pass


def emit_conversation_focus(
    *,
    tenant_id: Optional[int] = None,
    phone_tail: str = "",
    turn: Optional[int] = None,
    focus_before: Optional[FocusSnapshot] = None,
    focus_after: Optional[FocusSnapshot] = None,
    why_changed: str = "",
    decision_action: str = "",
    route_reason: str = "",
) -> None:
    try:
        before = (focus_before or FocusSnapshot()).as_dict()
        after = (focus_after or FocusSnapshot()).as_dict()
        logger.info(
            "[CONVERSATION_FOCUS] tenant=%s phone=*%s turn=%s "
            "focus_before=%s focus_after=%s product=%s variant=%s quantity=%s "
            "resolved_product=%s line_items=%s why_changed=%s decision_action=%s route_reason=%s",
            tenant_id,
            phone_tail,
            turn if turn is not None else "-",
            before.get("focus_title") or "-",
            after.get("focus_title") or "-",
            after.get("product") or "-",
            after.get("variant") or "-",
            after.get("quantity") or "-",
            after.get("resolved_product") or "-",
            after.get("line_item_summary") or "-",
            why_changed or "-",
            decision_action or "-",
            route_reason or "-",
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — evidence emit must not break turn
        pass


def emit_ack_decision(
    *,
    tenant_id: Optional[int] = None,
    phone_tail: str = "",
    turn: Optional[int] = None,
    important_customer_input: bool = False,
    input_types: Optional[Sequence[str]] = None,
    acknowledged: bool = False,
    reason: str = "",
    outbound_preview: str = "",
    decision_action: str = "",
    chosen_path: str = "",
    generic_ack_stub: bool = False,
    generic_ack_violation: bool = False,
    staff_route_detected: bool = False,
    fulfillment_locked: bool = False,
) -> None:
    try:
        logger.info(
            "[ACK_DECISION] tenant=%s phone=*%s turn=%s "
            "important_customer_input=%s input_type=%s acknowledged=%s reason=%s "
            "generic_ack_stub=%s generic_ack_violation=%s "
            "decision_action=%s chosen_path=%s staff_route_detected=%s "
            "fulfillment_locked=%s outbound_preview=%r",
            tenant_id,
            phone_tail,
            turn if turn is not None else "-",
            important_customer_input,
            ",".join(input_types or []) or "-",
            acknowledged,
            reason or "-",
            generic_ack_stub,
            generic_ack_violation,
            decision_action or "-",
            chosen_path or "-",
            staff_route_detected,
            fulfillment_locked,
            (outbound_preview or "")[:120],
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — evidence emit must not break turn
        pass


def emit_pipeline_turn_evidence(
    *,
    tenant_id: Optional[int],
    phone: str,
    turn: Optional[int],
    message: str,
    intent: Any,
    inbound_metadata: Optional[Dict[str, Any]],
    state_before: Any,
    state_after: Any,
    reply: str,
    decision_action: str,
    chosen_path: str,
    decision_reason: str = "",
) -> None:
    """Single choke-point at end of brain pipeline."""
    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            has_active_commerce_from_state,
        )

        focus_before = snapshot_focus(state_before)
        focus_after = snapshot_focus(state_after)
        why = infer_why_focus_changed(focus_before, focus_after)

        emit_conversation_focus(
            tenant_id=tenant_id,
            phone_tail=(phone or "")[-4:],
            turn=turn,
            focus_before=focus_before,
            focus_after=focus_after,
            why_changed=why,
            decision_action=decision_action,
            route_reason=decision_reason,
        )

        input_types = detect_input_types(
            message=message,
            intent=intent,
            inbound_metadata=inbound_metadata,
        )
        important = bool(input_types)
        prep = _order_prep(state_after)
        missing: List[str] = []
        if prep is not None:
            if isinstance(prep, dict):
                missing = list(prep.get("missing_fields") or [])
            else:
                missing = list(getattr(prep, "missing_fields", None) or [])

        ack = reply_acknowledges_important_input(
            reply=reply,
            input_types=input_types,
            state=state_after,
            missing_fields=missing,
        )
        stub = is_generic_ack_stub(reply)
        violation = important and stub and not ack

        locked = has_active_commerce_from_state(state_after)

        reason = "ok"
        if violation:
            reason = "generic_ack_without_consumption_or_next_step"
        elif important and not ack:
            reason = "important_input_not_reflected_in_reply"
        elif not important:
            reason = "no_important_input_detected"

        emit_ack_decision(
            tenant_id=tenant_id,
            phone_tail=(phone or "")[-4:],
            turn=turn,
            important_customer_input=important,
            input_types=input_types,
            acknowledged=ack,
            reason=reason,
            outbound_preview=reply or "",
            decision_action=decision_action,
            chosen_path=chosen_path,
            generic_ack_stub=stub,
            generic_ack_violation=violation,
            staff_route_detected=bool(_STAFF_ROUTE_RE.search(reply or "")),
            fulfillment_locked=locked,
        )

        expected = infer_expected_slot(state_after, intent)
        if important and input_types:
            emit_slot_consume(
                tenant_id=tenant_id,
                phone_tail=(phone or "")[-4:],
                turn=turn,
                expected_slot=expected,
                received={t: True for t in input_types},
                consumed=ack and not stub,
                reason=reason,
                source="pipeline_turn_evidence",
                decision_action=decision_action,
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — evidence emit must not break turn
        pass


__all__ = [
    "FocusSnapshot",
    "detect_input_types",
    "emit_ack_decision",
    "emit_conversation_focus",
    "emit_pipeline_turn_evidence",
    "emit_slot_consume",
    "infer_expected_slot",
    "infer_why_focus_changed",
    "is_generic_ack_stub",
    "reply_acknowledges_important_input",
    "snapshot_focus",
]
