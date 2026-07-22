"""
Outbound text policy — Phase 1 instrumentation.

Classifies every customer-facing outbound message by text source,
records postprocess mutations, and persists audit metadata on
``MessageEvent.extra_metadata``.

Goal (later phases): eliminate deterministic customer prose; deterministic
layers return facts/contracts only and LLM composes the final wording.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.outbound_text_policy")


class OutboundTextSource(str, Enum):
    LLM = "llm"
    DETERMINISTIC = "deterministic"
    TECHNICAL = "technical"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class OutboundDeliveryType(str, Enum):
    TEXT = "text"
    CTA_URL = "cta_url"
    NATIVE_CATALOG = "native_catalog"
    VCARD = "vcard"
    MEDIA = "media"
    OTHER = "other"


# Actions whose default compose path is template-based (debt until Phase 3).
_TEMPLATE_COMPOSE_ACTIONS = frozenset({
    "faq_reply",
    "greet",
    "search_products",
    "create_order",
    "send_payment_link",
    "track_order",
    "suggest_coupon",
    "recommend_addon",
    "web_search",
    "clarify",
    "narrow",
    "handoff",
    "catalog_navigate",
})


@dataclass
class PostprocessMutation:
    layer: str
    op: str  # append | replace | strip | reconcile | block | noop
    text_written: bool
    len_before: int = 0
    len_after: int = 0
    preview_before: str = ""
    preview_after: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer": self.layer,
            "op": self.op,
            "text_written": self.text_written,
            "len_before": self.len_before,
            "len_after": self.len_after,
            "preview_before": self.preview_before[:80],
            "preview_after": self.preview_after[:80],
        }


@dataclass
class OutboundTextTracker:
    """Accumulates outbound text policy metadata for one turn."""

    text_source: OutboundTextSource = OutboundTextSource.UNKNOWN
    policy_path: str = ""
    final_delivery_type: OutboundDeliveryType = OutboundDeliveryType.TEXT
    customer_facing_text_debt: bool = False
    deterministic_text_detected: bool = False
    technical_body_reason: str = ""
    intent: str = ""
    decision_action: str = ""
    audit_notes: List[str] = field(default_factory=list)
    postprocess_mutations: List[PostprocessMutation] = field(default_factory=list)
    pre_postprocess_body: str = ""
    postprocess_body: str = ""
    pre_cta_body: str = ""
    body_after_cta: str = ""
    cta_url: str = ""
    cta_label: str = ""
    catalog_sent: bool = False
    vcard_sent: bool = False
    contact_gate: Dict[str, Any] = field(default_factory=dict)
    fallback_reason: str = ""
    fallback_kind: str = ""

    def mark_fallback(
        self,
        *,
        reason: str,
        kind: str,
        intent: str = "",
        decision_action: str = "",
    ) -> None:
        self.fallback_reason = str(reason or "")
        self.fallback_kind = str(kind or "")
        if intent:
            self.intent = intent
        if decision_action:
            self.decision_action = decision_action
        self.note(f"fallback:{self.fallback_reason}:{self.fallback_kind}")

    def note(self, msg: str) -> None:
        if msg and msg not in self.audit_notes:
            self.audit_notes.append(msg)

    def set_compose_provenance(
        self,
        *,
        source: OutboundTextSource,
        policy_path: str,
        debt: bool = False,
        intent: str = "",
        decision_action: str = "",
    ) -> None:
        self.text_source = source
        self.policy_path = policy_path or self.policy_path
        self.intent = intent or self.intent
        self.decision_action = decision_action or self.decision_action
        if debt:
            self.customer_facing_text_debt = True
            self.deterministic_text_detected = True
        if source == OutboundTextSource.DETERMINISTIC:
            self.deterministic_text_detected = True
            self.customer_facing_text_debt = True
        elif source == OutboundTextSource.HYBRID:
            self.deterministic_text_detected = True
            self.customer_facing_text_debt = True

    def record_mutation(
        self,
        *,
        layer: str,
        op: str,
        before: str,
        after: str,
        text_written: Optional[bool] = None,
    ) -> None:
        b = str(before or "")
        a = str(after or "")
        if text_written is None:
            text_written = (a != b) and bool(a.strip())
        mut = PostprocessMutation(
            layer=layer,
            op=op,
            text_written=bool(text_written),
            len_before=len(b),
            len_after=len(a),
            preview_before=b[:80],
            preview_after=a[:80],
        )
        self.postprocess_mutations.append(mut)
        if mut.text_written and self.text_source == OutboundTextSource.LLM:
            self.text_source = OutboundTextSource.HYBRID
            self.customer_facing_text_debt = True
            self.deterministic_text_detected = True
        elif mut.text_written and self.text_source == OutboundTextSource.UNKNOWN:
            self.text_source = OutboundTextSource.DETERMINISTIC
            self.customer_facing_text_debt = True
            self.deterministic_text_detected = True
        self.postprocess_body = a

    def set_cta_delivery(
        self,
        *,
        pre_cta_body: str,
        body_after_cta: str,
        cta_url: str,
        cta_label: str,
        technical_reason: str = "whatsapp_cta_url_requires_body",
    ) -> None:
        self.final_delivery_type = OutboundDeliveryType.CTA_URL
        self.pre_cta_body = pre_cta_body or ""
        self.body_after_cta = body_after_cta or ""
        self.cta_url = cta_url or ""
        self.cta_label = cta_label or ""
        if not self.technical_body_reason:
            self.technical_body_reason = technical_reason
        # CTA label is technical; body may still be LLM/hybrid.
        if (body_after_cta or "").strip() in (".",):
            self.text_source = OutboundTextSource.TECHNICAL
            self.technical_body_reason = "catalog_or_cta_minimal_body"

    def set_native_catalog(
        self,
        *,
        body: str,
        technical_reason: str = "meta_native_catalog_interactive_body",
    ) -> None:
        self.final_delivery_type = OutboundDeliveryType.NATIVE_CATALOG
        self.catalog_sent = True
        self.technical_body_reason = technical_reason
        try:
            from modules.ai.brain.commerce.catalog_body_policy import (  # noqa: PLC0415
                is_minimal_catalog_body,
            )

            if is_minimal_catalog_body(body):
                self.text_source = OutboundTextSource.TECHNICAL
                self.technical_body_reason = "meta_native_catalog_minimal_body"
                self.note("native_catalog_minimal_body")
        except Exception:  # noqa: BLE001  # noqa: silent-ok — policy annotation must not block send
            if (body or "").strip() in (".", ""):
                self.note("native_catalog_minimal_body")

    def set_vcard_delivery(self, *, gate: Optional[Dict[str, Any]] = None) -> None:
        self.final_delivery_type = OutboundDeliveryType.VCARD
        self.vcard_sent = True
        if gate:
            self.contact_gate = dict(gate)

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "text_source": self.text_source.value,
            "policy_path": self.policy_path,
            "final_delivery_type": self.final_delivery_type.value,
            "postprocess_mutations": [m.to_dict() for m in self.postprocess_mutations],
            "deterministic_text_detected": self.deterministic_text_detected,
            "customer_facing_text_debt": self.customer_facing_text_debt,
            "technical_body_reason": self.technical_body_reason,
            "cta_url": self.cta_url,
            "cta_label": self.cta_label,
            "catalog_sent": self.catalog_sent,
            "vcard_sent": self.vcard_sent,
            "pre_postprocess_body_preview": (self.pre_postprocess_body or "")[:120],
            "postprocess_body_preview": (self.postprocess_body or "")[:120],
            "pre_cta_body_preview": (self.pre_cta_body or "")[:120],
            "body_after_cta_preview": (self.body_after_cta or "")[:120],
            "intent": self.intent,
            "decision_action": self.decision_action,
            "audit_notes": list(self.audit_notes),
            "contact_gate": dict(self.contact_gate) if self.contact_gate else {},
            "fallback_reason": self.fallback_reason,
            "fallback_kind": self.fallback_kind,
        }

    @classmethod
    def from_brain_result(cls, brain_result: Optional[Dict[str, Any]]) -> "OutboundTextTracker":
        tracker = cls()
        if not isinstance(brain_result, dict):
            return tracker
        raw = brain_result.get("outbound_text_policy") or {}
        if not isinstance(raw, dict):
            return tracker
        src = str(raw.get("text_source") or OutboundTextSource.UNKNOWN.value)
        try:
            tracker.text_source = OutboundTextSource(src)
        except ValueError:
            tracker.text_source = OutboundTextSource.UNKNOWN
        tracker.policy_path = str(raw.get("policy_path") or "")
        tracker.intent = str(raw.get("intent") or brain_result.get("intent") or "")
        tracker.decision_action = str(
            raw.get("decision_action") or brain_result.get("decision_action") or ""
        )
        tracker.customer_facing_text_debt = bool(raw.get("customer_facing_text_debt"))
        tracker.deterministic_text_detected = bool(raw.get("deterministic_text_detected"))
        return tracker


_DETERMINISTIC_COMPOSE_SOURCES = frozenset({
    "fallback_deterministic",
    "merchant_template",
    "meta_template",
    "legal_exact_text",
    "security_exact_text",
})
_LLM_COMPOSE_SOURCES = frozenset({"llm", "persona_llm"})
_LLM_OWNED_FINAL_SOURCES = frozenset({
    "llm",
    "persona_llm",
    "llm_postprocess",
    "persona_llm_postprocess",
})
_NON_LLM_FINAL_SOURCES = frozenset({
    "guard_rewrite",
    "dedup_substitution",
})
_GROUNDED_PERSONA_CHOSEN_PATHS = frozenset({
    "fact_bound_persona_compose",
    "catalog_miss_resolved_subject",
    "catalog_navigation_top_products_fallback",
})
_LLM_COMPOSE_ROUTE_PATHS = frozenset({
    "track_order_need_order_number",
    "track_order_not_found",
})
_ACTION_FALLBACK_CHOSEN_PATHS = frozenset({"", "action", "rule"})


def _approved_compose_source(value: object) -> str:
    from modules.ai.compose.reply_metadata_export import approved_compose_source  # noqa: PLC0415

    return approved_compose_source(value)


def _is_llm_candidate_flag(value: object) -> bool:
    return type(value) is bool and bool(value)


def is_producer_llm_chosen_path(
    chosen_path: str,
    *,
    decision_action: str = "",
) -> bool:
    """True when ``chosen_path`` names an approved LLM/persona compose route."""
    path = str(chosen_path or "").strip()
    action = str(decision_action or "").strip().lower()
    if not path or path in _ACTION_FALLBACK_CHOSEN_PATHS:
        return False
    if action and path == action and path in _TEMPLATE_COMPOSE_ACTIONS:
        return False
    if path in _GROUNDED_PERSONA_CHOSEN_PATHS:
        return True
    if path in _LLM_COMPOSE_ROUTE_PATHS:
        return True
    if path.startswith("llm"):
        return True
    if path.endswith("_compose"):
        return True
    return False


def _final_text_is_llm_derived(candidate: str, final_text: str) -> bool:
    candidate_norm = " ".join(str(candidate or "").split())
    final_norm = " ".join(str(final_text or "").split())
    return bool(
        candidate_norm
        and final_norm
        and (
            final_norm in candidate_norm
            or candidate_norm in final_norm
        )
    )


def final_source_supports_llm_ownership(
    *,
    final_customer_text_source: str,
    llm_candidate_present: bool,
    compose_reply_candidate: str,
    final_text: str = "",
) -> bool:
    """Fail-closed: LLM-owned final labels require candidate evidence."""
    final_src = str(final_customer_text_source or "").strip()
    if final_src not in _LLM_OWNED_FINAL_SOURCES:
        return False
    if not llm_candidate_present:
        return False
    candidate = str(compose_reply_candidate or "").strip()
    if not candidate:
        return False
    final = str(final_text or "").strip()
    if final_src.endswith("_postprocess"):
        return _final_text_is_llm_derived(candidate, final)
    if not final:
        return True
    return _final_text_is_llm_derived(candidate, final)


def _infer_from_producer_metadata(
    *,
    decision_action: str,
    compose_source: str,
    chosen_path: str,
    llm_candidate_present: bool,
    final_customer_text_source: str,
    compose_reply_candidate: str = "",
    final_text: str = "",
) -> Optional[tuple[OutboundTextSource, str, bool]]:
    """Classify from closed producer metadata; return None when metadata is inconclusive."""
    action = str(decision_action or "").strip().lower()
    final_src = str(final_customer_text_source or "").strip()

    if final_src in _NON_LLM_FINAL_SOURCES:
        return (
            OutboundTextSource.DETERMINISTIC,
            f"brain.compose.final.{final_src}",
            True,
        )

    if final_src in _LLM_OWNED_FINAL_SOURCES:
        if final_source_supports_llm_ownership(
            final_customer_text_source=final_src,
            llm_candidate_present=llm_candidate_present,
            compose_reply_candidate=compose_reply_candidate,
            final_text=final_text,
        ):
            return (
                OutboundTextSource.LLM,
                f"brain.compose.final.{final_src}",
                False,
            )
        return None

    if compose_source in _LLM_COMPOSE_SOURCES:
        if not llm_candidate_present:
            return None
        llm_path = is_producer_llm_chosen_path(
            chosen_path,
            decision_action=action,
        )
        if not llm_path and not final_source_supports_llm_ownership(
            final_customer_text_source=final_src,
            llm_candidate_present=llm_candidate_present,
            compose_reply_candidate=compose_reply_candidate,
            final_text=final_text,
        ):
            return None
        if compose_source == "persona_llm":
            policy_path = f"brain.compose.persona.{chosen_path or 'persona_llm'}"
        else:
            policy_path = f"brain.compose.llm.{chosen_path or 'llm'}"
        return OutboundTextSource.LLM, policy_path, False

    if compose_source in _DETERMINISTIC_COMPOSE_SOURCES:
        route = chosen_path or action or "unknown"
        return (
            OutboundTextSource.DETERMINISTIC,
            f"brain.compose.{compose_source}.{route}",
            True,
        )

    return None


def infer_compose_provenance(
    *,
    decision_action: str,
    used_llm: bool,
    used_template: bool = False,
    hybrid_layers: Optional[List[str]] = None,
    compose_source: str = "",
    chosen_path: str = "",
    llm_candidate_present: bool = False,
    final_customer_text_source: str = "",
    compose_reply_candidate: str = "",
    final_text: str = "",
) -> tuple[OutboundTextSource, str, bool]:
    action = str(decision_action or "").strip().lower()
    layers = list(hybrid_layers or [])
    approved_source = _approved_compose_source(compose_source)
    raw_final = str(final_customer_text_source or "").strip()
    final_source = (
        raw_final
        if raw_final in _LLM_OWNED_FINAL_SOURCES
        else _approved_compose_source(raw_final)
    )

    producer = _infer_from_producer_metadata(
        decision_action=action,
        compose_source=approved_source,
        chosen_path=str(chosen_path or "").strip(),
        llm_candidate_present=bool(llm_candidate_present),
        final_customer_text_source=final_source,
        compose_reply_candidate=str(compose_reply_candidate or "").strip(),
        final_text=str(final_text or "").strip(),
    )
    if producer is not None:
        return producer

    if used_llm and (used_template or layers):
        return OutboundTextSource.HYBRID, "brain.compose.hybrid", True
    if used_llm:
        return OutboundTextSource.LLM, "brain.compose._llm_compose", False
    if action in _TEMPLATE_COMPOSE_ACTIONS or used_template:
        path = f"brain.compose.templates.{action or 'unknown'}"
        return OutboundTextSource.DETERMINISTIC, path, True
    if action == "llm_reply":
        return OutboundTextSource.LLM, "brain.compose._llm_compose", False
    return OutboundTextSource.UNKNOWN, f"brain.compose.{action or 'unknown'}", False


def attach_compose_provenance(
    result: Any,
    *,
    decision: Any,
    ctx: Any,
    text: str,
) -> Dict[str, Any]:
    """Tag ``result.data`` with compose-phase outbound text policy."""
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        data = {}
        try:
            result.data = data
        except Exception:
            return {}

    compose_source = _approved_compose_source(data.get("compose_source"))
    chosen_path = str(data.get("chosen_path") or "").strip()
    llm_candidate_present = _is_llm_candidate_flag(data.get("llm_candidate_present"))
    raw_final_source = str(data.get("final_customer_text_source") or "").strip()
    compose_reply_candidate = str(data.get("compose_reply_candidate") or text or "").strip()

    used_llm = bool(data.pop("_compose_via_llm", False))
    used_template = bool(data.pop("_compose_via_template", False))
    hybrid_layers = list(data.pop("_compose_hybrid_layers", []) or [])

    action = str(getattr(decision, "action", "") or "")
    intent = str(getattr(getattr(ctx, "intent", None), "name", "") or "")

    # Native catalog navigate returns empty — wire layer owns body.
    if action == "catalog_navigate" and not (text or "").strip():
        source = OutboundTextSource.TECHNICAL
        policy_path = "brain.compose.catalog_navigate.deferred"
        debt = False
    else:
        source, policy_path, debt = infer_compose_provenance(
            decision_action=action,
            used_llm=used_llm,
            used_template=used_template,
            hybrid_layers=hybrid_layers,
            compose_source=compose_source,
            chosen_path=chosen_path,
            llm_candidate_present=llm_candidate_present,
            final_customer_text_source=raw_final_source,
            compose_reply_candidate=compose_reply_candidate,
            final_text=str(text or "").strip(),
        )

    if hybrid_layers:
        debt = True

    policy = {
        "text_source": source.value,
        "policy_path": policy_path,
        "customer_facing_text_debt": debt,
        "deterministic_text_detected": debt or source == OutboundTextSource.DETERMINISTIC,
        "intent": intent,
        "decision_action": action,
        "compose_hybrid_layers": hybrid_layers,
    }
    data["outbound_text_policy"] = policy
    return policy


def reconcile_outbound_compose_provenance(
    result_data: Dict[str, Any],
    *,
    decision_action: str,
    intent: str = "",
    final_text: str,
) -> Dict[str, Any]:
    """Reconcile outbound policy at the final post-guard boundary."""
    existing = dict(result_data.get("outbound_text_policy") or {})
    hybrid_layers = list(existing.get("compose_hybrid_layers") or [])

    compose_source = _approved_compose_source(result_data.get("compose_source"))
    llm_candidate_present = _is_llm_candidate_flag(result_data.get("llm_candidate_present"))
    raw_final = str(result_data.get("final_customer_text_source") or "").strip()
    chosen_path = str(result_data.get("chosen_path") or "").strip()
    transformed = (
        type(result_data.get("final_text_transformed")) is bool
        and bool(result_data.get("final_text_transformed"))
    )
    candidate = str(result_data.get("compose_reply_candidate") or "").strip()
    final = str(final_text or "").strip()

    if raw_final in _NON_LLM_FINAL_SOURCES:
        compose_source = ""
        llm_candidate_present = False
    elif raw_final in _LLM_OWNED_FINAL_SOURCES and not final_source_supports_llm_ownership(
        final_customer_text_source=raw_final,
        llm_candidate_present=llm_candidate_present,
        compose_reply_candidate=candidate,
        final_text=final,
    ):
        raw_final = ""
        compose_source = ""
        llm_candidate_present = False
    elif (
        transformed
        and compose_source in _LLM_COMPOSE_SOURCES
        and raw_final not in _LLM_OWNED_FINAL_SOURCES
        and candidate
        and final
        and not _final_text_is_llm_derived(candidate, final)
    ):
        compose_source = ""
        llm_candidate_present = False

    source, policy_path, debt = infer_compose_provenance(
        decision_action=decision_action,
        used_llm=False,
        used_template=compose_source in _DETERMINISTIC_COMPOSE_SOURCES,
        compose_source=compose_source,
        chosen_path=chosen_path,
        llm_candidate_present=llm_candidate_present,
        final_customer_text_source=raw_final,
        compose_reply_candidate=candidate,
        final_text=final,
    )

    policy = {
        "text_source": source.value,
        "policy_path": policy_path,
        "customer_facing_text_debt": debt,
        "deterministic_text_detected": debt or source == OutboundTextSource.DETERMINISTIC,
        "intent": str(intent or existing.get("intent") or ""),
        "decision_action": str(decision_action or existing.get("decision_action") or ""),
        "compose_hybrid_layers": hybrid_layers,
    }
    result_data["outbound_text_policy"] = policy
    return policy


def mark_compose_llm(result: Any) -> None:
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        data["_compose_via_llm"] = True


def mark_compose_template(result: Any, *, layer: str = "") -> None:
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        data["_compose_via_template"] = True
        if layer:
            layers = list(data.get("_compose_hybrid_layers") or [])
            if layer not in layers:
                layers.append(layer)
            data["_compose_hybrid_layers"] = layers


def mark_compose_metadata(result: Any, *, layer: str = "") -> None:
    """Tag compose turn with metadata-only operational facts (no customer prose debt)."""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        data["_compose_metadata_only"] = True
        if layer:
            layers = list(data.get("_compose_metadata_layers") or [])
            if layer not in layers:
                layers.append(layer)
            data["_compose_metadata_layers"] = layers


def merge_policy_into_extra_metadata(
    extra: Optional[Dict[str, Any]],
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    merged = dict(extra or {})
    merged["outbound_text_policy"] = dict(policy)
    return merged


def log_outbound_text_policy(tracker: OutboundTextTracker, *, tenant_id: Any = None, to: str = "") -> None:
    try:
        logger.info(
            "[OUTBOUND_TEXT_POLICY] tenant=%s to=%s source=%s debt=%s "
            "delivery=%s mutations=%d path=%s",
            tenant_id,
            to,
            tracker.text_source.value,
            tracker.customer_facing_text_debt,
            tracker.final_delivery_type.value,
            len(tracker.postprocess_mutations),
            tracker.policy_path,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — policy log must not block send
        pass


__all__ = [
    "OutboundDeliveryType",
    "OutboundTextSource",
    "OutboundTextTracker",
    "PostprocessMutation",
    "attach_compose_provenance",
    "final_source_supports_llm_ownership",
    "infer_compose_provenance",
    "is_producer_llm_chosen_path",
    "log_outbound_text_policy",
    "mark_compose_llm",
    "mark_compose_metadata",
    "mark_compose_template",
    "merge_policy_into_extra_metadata",
    "reconcile_outbound_compose_provenance",
]
