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


def infer_compose_provenance(
    *,
    decision_action: str,
    used_llm: bool,
    used_template: bool = False,
    hybrid_layers: Optional[List[str]] = None,
) -> tuple[OutboundTextSource, str, bool]:
    action = str(decision_action or "").strip().lower()
    layers = list(hybrid_layers or [])
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
    "infer_compose_provenance",
    "log_outbound_text_policy",
    "mark_compose_llm",
    "mark_compose_template",
    "merge_policy_into_extra_metadata",
]
