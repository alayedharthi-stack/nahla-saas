"""
persona_ownership.py
────────────────────
Measurement-only ownership record for outbound AI replies.

Tracks whether the customer-facing text passed through Nahla Persona
Composer (``persona_expression_mode``) or bypassed via templates,
webhook shortcuts, guards, dedup, or fallbacks.

No reply text is produced here — observability only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class PersonaBypassReason(str, Enum):
    PRE_BRAIN_FAST_PATH = "PRE_BRAIN_FAST_PATH"
    PRE_BRAIN_HANDOFF = "PRE_BRAIN_HANDOFF"
    WEBHOOK_ESCALATION = "WEBHOOK_ESCALATION"
    SOCIAL_TEMPLATE = "SOCIAL_TEMPLATE"
    TEMPLATE_PATH = "TEMPLATE_PATH"
    COMMERCE_LLM = "COMMERCE_LLM"
    CLARIFY_DETERMINISTIC = "CLARIFY_DETERMINISTIC"
    TRUTH_GUARD_REWRITE = "TRUTH_GUARD_REWRITE"
    SAFETY_NET_REWRITE = "SAFETY_NET_REWRITE"
    DEDUP_REPLY = "DEDUP_REPLY"
    FALLBACK_REPLY = "FALLBACK_REPLY"
    LEGACY_ROUTE = "LEGACY_ROUTE"
    BRAIN_SILENT_ACK = "BRAIN_SILENT_ACK"
    LLM_TIMEOUT_STUB = "LLM_TIMEOUT_STUB"
    ETIQUETTE_PREPEND = "ETIQUETTE_PREPEND"
    AUTOMATION_RECOVERY = "AUTOMATION_RECOVERY"
    BILLING_DENIED = "BILLING_DENIED"
    STAFF_CONTACT_RECOVERY = "STAFF_CONTACT_RECOVERY"
    CONSTRAINED_COMPOSE = "CONSTRAINED_COMPOSE"
    UNKNOWN = "UNKNOWN"


# Brain ``Decision.action`` values that always emit template Arabic.
_TEMPLATE_ACTIONS = frozenset({
    "greet",
    "faq_reply",
    "search_products",
    "propose_draft_order",
    "send_payment_link",
    "track_order",
    "suggest_coupon",
    "recommend_addon",
    "web_search",
    "clarify",
    "narrow_choices",
    "handoff_to_human",
    "social_reply",
    "platform_reply",
    "out_of_scope_reply",
    "order_context_update",
    "stash_address_pre_product",
    "variant_pricing",
    "payment_transfer_promise",
    "customer_ledger_reply",
    "payment_continuation_reply",
})


@dataclass
class PersonaOwnershipRecord:
    """Single outbound message ownership snapshot."""

    persona_stamped: Optional[bool] = None
    persona_topic: Optional[str] = None
    persona_kind: Optional[str] = None
    expression_owner: str = ""
    bypass_reason: Optional[str] = None
    compose_pass_count: int = 0
    pre_stamp_layers: List[str] = field(default_factory=list)
    finalized: bool = False

    def stamp_persona(
        self,
        *,
        topic: str,
        kind: str = "",
        owner: str = "persona_composer",
    ) -> None:
        self.persona_stamped = True
        self.persona_topic = str(topic or "").strip() or None
        self.persona_kind = str(kind or "").strip() or None
        self.expression_owner = owner
        self.bypass_reason = None
        if self.compose_pass_count <= 0:
            self.compose_pass_count = 1

    def mark_bypass(self, reason: PersonaBypassReason | str, *, owner: str) -> None:
        reason_str = (
            reason.value if isinstance(reason, PersonaBypassReason) else str(reason)
        )
        self.persona_stamped = False
        self.bypass_reason = reason_str
        self.expression_owner = str(owner or "").strip() or reason_str
        if self.compose_pass_count <= 0:
            self.compose_pass_count = 0

    def note_layer(self, layer: str) -> None:
        layer = str(layer or "").strip()
        if layer and layer not in self.pre_stamp_layers:
            self.pre_stamp_layers.append(layer)

    def invalidate_stamp(
        self,
        reason: PersonaBypassReason | str,
        layer: str,
    ) -> None:
        """Post-compose substitution removed persona ownership."""
        self.note_layer(layer)
        if self.persona_stamped:
            self.mark_bypass(reason, owner=layer)
        elif self.bypass_reason is None:
            self.mark_bypass(reason, owner=layer)

    def on_text_replaced(
        self,
        *,
        layer: str,
        reason: PersonaBypassReason | str,
        before: str,
        after: str,
    ) -> None:
        if (before or "").strip() == (after or "").strip():
            return
        self.invalidate_stamp(reason, layer)

    def merge_from_dict(self, raw: Optional[Dict[str, Any]]) -> None:
        if not isinstance(raw, dict):
            return
        if raw.get("persona_stamped") is not None:
            self.persona_stamped = bool(raw.get("persona_stamped"))
        pt = raw.get("persona_topic")
        if pt is not None:
            self.persona_topic = str(pt).strip() or None
        pk = raw.get("persona_kind")
        if pk is not None:
            self.persona_kind = str(pk).strip() or None
        eo = raw.get("expression_owner")
        if eo:
            self.expression_owner = str(eo)
        br = raw.get("bypass_reason")
        if br:
            self.bypass_reason = str(br)
        try:
            self.compose_pass_count = int(raw.get("compose_pass_count") or 0)
        except (TypeError, ValueError):
            pass
        layers = raw.get("pre_stamp_layers")
        if isinstance(layers, list):
            for item in layers:
                self.note_layer(str(item))

    def finalize(self) -> PersonaOwnershipRecord:
        if self.persona_stamped is None:
            if self.bypass_reason:
                self.persona_stamped = False
            else:
                self.persona_stamped = False
                self.bypass_reason = PersonaBypassReason.UNKNOWN.value
                self.expression_owner = self.expression_owner or "unknown"
        self.finalized = True
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "persona_stamped": self.persona_stamped,
            "persona_topic": self.persona_topic,
            "persona_kind": self.persona_kind,
            "expression_owner": self.expression_owner or None,
            "bypass_reason": self.bypass_reason,
            "compose_pass_count": int(self.compose_pass_count or 0),
            "pre_stamp_layers": list(self.pre_stamp_layers),
            "finalized": bool(self.finalized),
        }

    def to_metadata(self) -> Dict[str, Any]:
        """Persist under ``MessageEvent.extra_metadata['persona_ownership']``."""
        final = self.finalize()
        return {
            "persona_ownership": final.to_dict(),
            "is_ai": True,
        }


def build_brain_persona_ownership(
    *,
    decision_action: str,
    decision_args: Optional[Dict[str, Any]],
    reply_state: Any,
    chosen_path: str,
    guard_replaced: Optional[Dict[str, bool]] = None,
    compose_source: object = "",
    llm_candidate_present: object = None,
    persona_topic_hint: str = "",
    final_customer_text_source: object = "",
    final_text_transformed: object = None,
    compose_reply_candidate: object = "",
    final_reply: object = "",
) -> PersonaOwnershipRecord:
    """
    Classify ownership at the Brain pipeline final boundary (post-finalization).
    """
    from core.outbound_text_policy import (  # noqa: PLC0415
        _final_text_is_llm_derived,
        final_source_supports_llm_ownership,
        is_producer_llm_chosen_path,
    )
    from modules.ai.compose.reply_metadata_export import approved_compose_source  # noqa: PLC0415

    rec = PersonaOwnershipRecord()
    action = str(decision_action or "").strip()
    args = dict(decision_args or {})
    path = str(chosen_path or "").strip()
    src = approved_compose_source(compose_source)
    has_llm_candidate = type(llm_candidate_present) is bool and llm_candidate_present
    topic_hint = str(persona_topic_hint or args.get("question_kind") or "").strip()
    final_src = str(final_customer_text_source or "").strip()
    transformed = type(final_text_transformed) is bool and bool(final_text_transformed)
    candidate = str(compose_reply_candidate or "").strip()
    final = str(final_reply or "").strip()

    persona_mode = bool(getattr(reply_state, "persona_expression_mode", False))
    persona_topic = str(getattr(reply_state, "persona_topic", "") or "").strip()
    persona_kind = str(args.get("persona_kind") or "").strip()

    def _primary_guard_owner() -> str:
        for layer, replaced in (guard_replaced or {}).items():
            if replaced:
                return str(layer or "").strip() or "guard_rewrite"
        return "guard_rewrite"

    def _final_supports_llm() -> bool:
        return final_source_supports_llm_ownership(
            final_customer_text_source=final_src,
            llm_candidate_present=has_llm_candidate,
            compose_reply_candidate=candidate,
            final_text=final,
        )

    if final_src in {"persona_llm", "persona_llm_postprocess"} and _final_supports_llm():
        rec.stamp_persona(
            topic=topic_hint or "catalog_product_answer",
            kind="grounded_persona_compose",
            owner="persona_llm",
        )
    elif final_src in {"llm", "llm_postprocess"} and _final_supports_llm():
        rec.mark_bypass(PersonaBypassReason.COMMERCE_LLM, owner="llm_compose")
    elif final_src in {"persona_llm", "persona_llm_postprocess", "llm", "llm_postprocess"}:
        rec.mark_bypass(
            PersonaBypassReason.TRUTH_GUARD_REWRITE,
            owner=_primary_guard_owner(),
        )
    elif final_src == "fallback_deterministic" or src == "fallback_deterministic":
        rec.mark_bypass(
            PersonaBypassReason.FALLBACK_REPLY,
            owner=path or "fallback_deterministic",
        )
    elif final_src in {"guard_rewrite", "dedup_substitution"}:
        rec.mark_bypass(
            PersonaBypassReason.TRUTH_GUARD_REWRITE,
            owner=_primary_guard_owner(),
        )
    elif (
        transformed
        and not final_src
        and src in {"persona_llm", "llm"}
        and candidate
        and final
        and not _final_text_is_llm_derived(candidate, final)
    ):
        rec.mark_bypass(
            PersonaBypassReason.TRUTH_GUARD_REWRITE,
            owner=_primary_guard_owner(),
        )
    elif (
        src == "persona_llm"
        and has_llm_candidate
        and is_producer_llm_chosen_path(path, decision_action=action)
    ):
        rec.stamp_persona(
            topic=topic_hint or "catalog_product_answer",
            kind="grounded_persona_compose",
            owner="persona_llm",
        )
    elif (
        src == "llm"
        and has_llm_candidate
        and is_producer_llm_chosen_path(path, decision_action=action)
    ):
        rec.mark_bypass(PersonaBypassReason.COMMERCE_LLM, owner="llm_compose")
    elif src in {"merchant_template", "meta_template"}:
        rec.mark_bypass(
            PersonaBypassReason.TEMPLATE_PATH,
            owner=f"template:{src}",
        )
    elif src in {"legal_exact_text", "security_exact_text"}:
        rec.mark_bypass(PersonaBypassReason.TEMPLATE_PATH, owner=src)
    elif persona_mode and persona_topic:
        rec.stamp_persona(topic=persona_topic, kind=persona_kind)
    elif action == "social_reply":
        rec.mark_bypass(PersonaBypassReason.SOCIAL_TEMPLATE, owner="social_template")
    elif action == "llm_reply":
        if "timeout" in path:
            rec.mark_bypass(PersonaBypassReason.LLM_TIMEOUT_STUB, owner=path or "llm_timeout")
        elif "fallback" in path:
            rec.mark_bypass(PersonaBypassReason.FALLBACK_REPLY, owner=path)
        else:
            rec.mark_bypass(PersonaBypassReason.COMMERCE_LLM, owner="llm_compose")
    elif action == "clarify":
        rec.mark_bypass(
            PersonaBypassReason.CLARIFY_DETERMINISTIC,
            owner="clarify_template",
        )
    elif action in _TEMPLATE_ACTIONS or path in {"rule", "action"}:
        rec.mark_bypass(
            PersonaBypassReason.TEMPLATE_PATH,
            owner=f"template:{action or path}",
        )
    else:
        rec.mark_bypass(
            PersonaBypassReason.TEMPLATE_PATH,
            owner=action or path or "brain_compose",
        )

    return rec


def sync_persona_to_turn_trace(trace: Any, record: PersonaOwnershipRecord) -> None:
    """Copy finalized ownership onto ``TurnTrace`` for ``[TURN]`` emission."""
    final = record.finalize()
    trace.persona_stamped = final.persona_stamped
    trace.persona_topic = final.persona_topic or ""
    trace.persona_kind = final.persona_kind or ""
    trace.bypass_reason = final.bypass_reason or ""
    trace.expression_owner = final.expression_owner or ""
    trace.compose_pass_count = int(final.compose_pass_count or 0)
    if not isinstance(getattr(trace, "extra", None), dict):
        trace.extra = {}
    trace.extra["persona_ownership"] = final.to_dict()


__all__ = [
    "PersonaBypassReason",
    "PersonaOwnershipRecord",
    "build_brain_persona_ownership",
    "sync_persona_to_turn_trace",
]
