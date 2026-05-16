"""
brain/compose/prompt_builder.py
───────────────────────────────
Short prompt builder for MerchantBrain LLM fallback.

Phase 1 of the prompt-pipeline refactor (see the architecture note
"افصل System Prompt عن Knowledge Base"). The prompt is now assembled
in four clearly-separated, priority-ordered blocks:

    1. PERSONA               — who Nahla is (voice, identity)
    2. ⚠️ HIGH PRIORITY      — STYLE + POLICY + FORBIDDEN
                                (always rendered, overrides everything else)
    3. KNOWLEDGE             — facts the assistant can cite (NO behavior)
    4. TOOLS                 — Product Resolver + Media Library vocabulary
    5. DECISION CONTEXT      — this-turn intent / stage / goal
    6. BRAIN STATE JSON      — full structured world model

The behavior contract (length, escalation, contact-release, etc.) lives
in block #2 only. The knowledge base is reduced to facts. This is the
architectural fix for the long-standing leak where merchants pasted
behavior rules into the KB textarea and shifted Nahla's voice.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, Dict

from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt
from modules.ai.prompts.high_priority_layer import build_high_priority_block
from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split

from core.store_display import clean_store_name

from ..types import BrainReplyState

_log = logging.getLogger("nahla.ai.prompt")


def _approx_tokens(text: str) -> int:
    """Cheap char/4 token estimator — close enough for log gauges.

    We intentionally don't pull in tiktoken/anthropic-tokenizer here:
    the goal is to log relative block sizes for visibility, not to
    enforce a hard limit. Char-per-token ratios are ~3.5-4 for Arabic
    in BPE tokenizers, so this floors slightly low which is fine.
    """
    return max(0, len(text or "") // 4)


def build_brain_reply_prompt(state: BrainReplyState) -> str:
    """Compose the LLM prompt for Brain's `compose_reply` fallback.

    Layering (top → bottom) — strict priority order:

      1. Nahla persona            — base identity / voice
      2. High-Priority Style+Policy banner
                                  — platform-wide baseline + merchant
                                    overrides extracted from ai_settings.
                                    Always present; explicitly outranks
                                    the knowledge base when they conflict.
      3. Knowledge base (Facts)   — merchant-typed facts, with the
                                    Salla-wins-on-price reminder.
                                    Behavior rules are NOT here anymore;
                                    they were lifted into block #2.
      4. Tools (libraries)        — Product Resolver + Media Library
                                    vocabulary so the LLM emits
                                    [PRODUCT:...] / [MEDIA_KEY:...]
                                    markers instead of guessing URLs.
      5. Brain-this-turn rules    — slim residual operating rules
                                    (stage discipline, coupon
                                    discipline, anti-repeat). Most of
                                    the old block moved up into #2.
      6. BrainStateJSON           — the structured world model.
    """
    store_name = clean_store_name((state.store_name or "").strip()) or "المتجر"
    tone = _tone_instruction(state.tone)

    # Tenant overlay is now split into structured buckets. The legacy
    # `state.tenant_overlay` string is parsed via the merchant_context
    # ai_settings on the way in, so the pipeline doesn't have to change.
    settings_for_overlay = _extract_ai_settings_from_state(state)
    overlay_buckets = build_tenant_overlay_split(settings_for_overlay)

    state_dict = asdict(state)
    # Drop the legacy concatenated overlay — it would duplicate the
    # split buckets below.
    state_dict.pop("tenant_overlay", None)
    brain_state_json = json.dumps(state_dict, ensure_ascii=False, indent=2)

    # ── BLOCK 1: Persona ──────────────────────────────────────────────────
    persona_block = nahla_persona_system_prompt(store_name=store_name)

    # ── BLOCK 2: HIGH PRIORITY (Style + Policy + Forbidden) ───────────────
    high_priority_block = build_high_priority_block(
        settings_for_overlay,
        store_name=store_name,
    )

    # Assistant identity (name + role) sits with the persona, not with
    # behavior rules. It's a fact, not a constraint.
    identity_block = overlay_buckets.get("identity", "")

    # ── BLOCK 3: Knowledge (Facts only) ───────────────────────────────────
    kb_block = overlay_buckets.get("facts", "")

    _platform_mode = bool(getattr(state, "platform_kb_mode", False))
    if _platform_mode:
        excerpt = str(getattr(state, "platform_kb_excerpt", "") or "").strip()
        _ptopic = str(getattr(state, "platform_topic", "") or "").strip()
        if excerpt:
            kb_block = (
                "### مقتطف قاعدة المعرفة (استفسار عن منصّة نحلة / التقنية / الاشتراك)\n"
                f"الموضوع المُصنَّف آلياً: `{_ptopic}`\n\n"
                f"{excerpt}\n\n"
                "### قواعد إلزامية لهذه الجولة\n"
                "- أجيبي بلهجة خليجية طبيعية ودافئة وباختصار (2–5 أسطر) ما لم يطلب العميل تفصيلاً.\n"
                "- استخدمي **فقط** معلومات المقتطف أعلاه + نص سؤال العميل — لا اختراع ولا تخمين.\n"
                "- **ممنوع** اقتراح منتجات العسل أو الكتالوج أو التوفر أو الأسعار من المتجر.\n"
                "- **ممنوع** استخدام ‎[PRODUCT:...]‎ أو ‎[MEDIA_KEY:...]‎ أو أي أداة بيع.\n"
                "- إذا نقص شيء في المقتطف، قولي ذلك بلطف ووجّهي لقناة دعم نحلة إن وُجدت في النص؛ "
                "ولا تُنشئي روابط أو أرقاماً وهمية.\n"
            )
        else:
            kb_block = (
                "### قاعدة المعرفة (منصّة نحلة)\n"
                "لم يُعثر على مقطع ذي صلة في المعرفة اليدوية للمتجر لهذا الموضوع.\n\n"
                "### قواعد إلزامية لهذه الجولة\n"
                "- ردّي باختصار وبلطف: السؤال يخصّ منصّة نحلة أو إعداداتها وليس منتجات المتجر.\n"
                "- لا تخترعي روابط أو أسعار اشتراك أو خطوات تقنية غير مذكورة في سياق المحادثة.\n"
                "- **ممنوع** عروض كتالوج أو ‎[PRODUCT:...]‎.\n"
            )

    # ── BLOCK 4: Tools — libraries vocabulary + marker protocol ──────────
    # Two layered sub-blocks:
    #   (a) format_libraries_for_prompt   — concrete coupons + ai_media items
    #   (b) resolver_overlay              — the [PRODUCT:...] + [MEDIA_KEY:...]
    #                                       protocol and the per-tenant list
    #                                       of available media keys
    # The resolver overlay is pre-computed in pipeline.py (it needs the DB)
    # and shipped through slim_merchant_ctx["resolver_overlay"]. The
    # prompt builder stays IO-free.
    libraries_text = ""
    try:
        from core.ai_libraries import format_libraries_for_prompt  # noqa: PLC0415
        libraries_text = format_libraries_for_prompt(state.merchant_context or {}) or ""
    except Exception:  # noqa: BLE001 — never let formatting crash the prompt
        libraries_text = ""

    resolver_overlay = ""
    try:
        resolver_overlay = str(
            (state.merchant_context or {}).get("resolver_overlay") or ""
        )
    except Exception:  # noqa: BLE001
        resolver_overlay = ""

    tools_parts: list[str] = []
    if not _platform_mode:
        if libraries_text:
            tools_parts.append(libraries_text)
        if resolver_overlay:
            tools_parts.append(resolver_overlay)
    else:
        # Hide product/media marker vocabulary so the model cannot drift
        # into catalogue tooling on a platform-intent turn.
        tools_parts.append(
            "## أدوات المنتجات\n"
            "معطّلة لهذه الجولة — العميل يستفسر عن منصّة نحلة وليس عن مخزون المتجر."
        )
    tools_block = "\n\n".join(tools_parts)

    # ── BLOCK 5: This-turn decision context + slim residual rules ────────
    selected_title = ""
    if isinstance(state.selected_product, dict):
        selected_title = str(state.selected_product.get("title") or "")
    identity_line = (
        "- ✅ identity_already_introduced=TRUE — ممنوع تكرار "
        "«أنا نحلة / أنا مساعدة / أنا ذكاء اصطناعي» في هذا الرد. "
        "الترحيب يكون قصيراً وبدون تعريف."
        if state.identity_already_introduced
        else "- identity_already_introduced=false — يمكن تعريف النفس "
             "مرة واحدة فقط (مرة في هذه الجولة)."
    )
    # May 2026 #7 — surface the relational frame in the decision block so
    # the LLM sees it BEFORE the response_goal text. Hidden when the
    # detector returned "unknown" so the prompt shape stays identical for
    # the ambiguous turns we don't want to force a frame on.
    _rf = (getattr(state, "relational_frame", "") or "").strip()
    _rf_ev = (getattr(state, "relational_evidence", "") or "").strip()
    relational_line = (
        f"- relational_frame: {_rf}"
        + (f" — {_rf_ev}" if _rf_ev else "")
        + "\n"
        if _rf
        else ""
    )
    decision_block = (
        "## سياق القرار لهذه الجولة (مصدر واحد فقط)\n"
        f"- الـ intent الحالي: {state.intent_name or 'unknown'}\n"
        f"- المرحلة (stage): {state.stage}\n"
        f"- المنتج الحالي: {selected_title or '—'}\n"
        f"{relational_line}"
        f"- هدف الرد (response_goal): {state.response_goal or '—'}\n"
        f"{identity_line}\n"
        "هذا السياق هو الحقيقة الرسمية للجولة — لا تتجاوزيه ولا تعيدي "
        "تشخيص نية العميل من نص الرسالة وحدها."
    )
    if _platform_mode:
        decision_block += (
            "\n- ⚠️ **platform_kb_mode** — الجولة لاستفسار عن **منصّة نحلة** "
            "وليست لطلب منتج من المتجر. تجاهلي أي إشارات JSON للمرحلة التجارية "
            "عند صياغة الرد."
        )

    if _platform_mode:
        brain_residual_rules = (
            "## قواعد تشغيل Brain — وضع استفسار المنصّة\n"
            f"- النبرة المطلوبة: {tone} — دافئة ومختصرة كمحادثة واتساب.\n"
            "- هذه الجولة **ليست** مسار طلب أو دفع؛ لا كوبونات ولا خصومات "
            "ولا متابعة بيعية ولا طلب عنوان.\n"
            "- لا تكرّري التعريف بنفسك.\n"
            "- إذا كان BrainStateJSON يظهر منتجاً محدداً فتجاهلي ذلك لأغراض "
            "الرد — العميل يسأل عن إعدادات/خدمة نحلة.\n"
        )
    else:
        autopilot_on = bool(
            (state.merchant_context or {})
            .get("brain_profile", {})
            .get("autopilot_enabled", False)
        )
        if autopilot_on:
            coupon_priority_rule = (
                "- ❗ الكوبونات: المتجر يعمل بالطيار الآلي (autopilot ON)، "
                "لذا الأولوية للكوبونات التلقائية المعتمدة في النظام. "
                "إذا لم يقدّم BrainState كوبوناً تلقائياً مناسباً ولزم الأمر، "
                "يمكنك استخدام كود من merchant_context.manual_coupons "
                "(فقط من القائمة، بدون اختراع، وبما يطابق usage_context). "
                "اختاري الأنسب بحسب usage_context أو الأقل priority."
            )
        else:
            coupon_priority_rule = (
                "- ❗ الكوبونات اليدوية (الطيار الآلي مغلق): عند الحاجة "
                "لإرسال كوبون أو خصم، استخدمي فقط الأكواد المذكورة في "
                "merchant_context.manual_coupons — هذه هي المصدر الوحيد "
                "للكوبونات في هذا الوضع. اختاري الأنسب بحسب usage_context "
                "أو الأقل priority. لا تخترعي كوبونات ولا تعدّلي على الكود "
                "ولا ترسلي كوبوناً غير موجود في القائمة."
            )

        # The residual rules here are the few that depend on per-turn
        # context (stage, autopilot mode, BrainStateJSON keys). Everything
        # else moved up into the High-Priority block.
        brain_residual_rules = (
            "## قواعد تشغيل Brain لهذه الجولة\n"
            f"- النبرة المطلوبة لهذا المتجر: {tone}.\n"
            "- اتبعي المرحلة (stage) والخطوة المقترحة التالية (recommended_next_step) "
            "وهدف الرد (response_goal) أعلاه — حتى لو شعرتِ أن الرسالة تستحق رداً مختلفاً.\n"
            "- إذا كانت المرحلة ordering/checkout فلا ترحبي ولا تعيدي التعريف بنفسك "
            "ولا تعرضي قائمة منتجات جديدة — أكملي الطلب الحالي بسؤال واحد قصير.\n"
            "- لا تكرّري نفس السؤال إذا كان قد طُرح بالفعل وكان الجواب معروفاً.\n"
            "- لا تعرضي خصماً أو كوبوناً إلا إذا أظهر BrainState أن الوقت "
            "مناسب أو طلب العميل خصماً بوضوح. (عند ذكر الخصم استخدمي 🎁 "
            "بحد أقصى مرة واحدة في الرسالة.)\n"
            f"{coupon_priority_rule}\n"
            "- إذا كانت المعلومة ناقصة، اسألي سؤال متابعة واحداً فقط، قصيراً وواضحاً.\n"
            "- لا تخترعي حقائق غير موجودة في known_facts أو selected_product.\n"
            "- اجعلي ردك قصيراً ومناسباً لواتساب (راجع HIGH PRIORITY أعلاه)."
        )

    # ── Assemble ──────────────────────────────────────────────────────────
    parts: list[str] = [persona_block, high_priority_block]
    if identity_block:
        parts.append(identity_block)
    if kb_block:
        parts.append(kb_block)
    if tools_block:
        parts.append(tools_block)
    parts.append(decision_block)
    parts.append(brain_residual_rules)
    if _platform_mode:
        brain_json_tail = (
            f"BrainStateJSON:\n{brain_state_json}\n\n"
            "في وضع استفسار المنصّة: استخدمي الحقول لفهم المراسلة فقط — "
            "**ممنوع** اقتراح خطوة تجارية أو طلب منتج بناءً على JSON."
        )
    else:
        brain_json_tail = (
            f"BrainStateJSON:\n{brain_state_json}\n\n"
            "إذا كانت conversation_summary أو customer_memory أو store_knowledge "
            "موجودة فاستخدميها لفهم السياق واقتراح الخطوة التجارية التالية، "
            "لكن لا تذكري أي معلومة غير موجودة فيها."
        )

    parts.append(brain_json_tail)

    final_prompt = "\n\n".join(parts)

    # ── Structured log ────────────────────────────────────────────────────
    # Emits per-block sizes so we can see (a) when the KB grows huge,
    # (b) when style overrides are missing for a merchant, (c) the
    # rough token budget consumed before the LLM call. Cheap (char
    # len + division), single INFO line per turn — opt-in to extract
    # via `[PROMPT_LAYERS]` filter.
    _emit_prompt_log(
        state=state,
        persona=persona_block,
        high_priority=high_priority_block,
        identity=identity_block,
        kb=kb_block,
        tools=tools_block,
        libraries=libraries_text,
        resolver_overlay=resolver_overlay,
        decision=decision_block,
        residual=brain_residual_rules,
        json_block=brain_state_json,
        total=final_prompt,
    )

    return final_prompt


def _extract_ai_settings_from_state(state: BrainReplyState) -> Dict[str, Any]:
    """
    Pull the merchant's ai_settings out of the BrainReplyState.

    The Brain pipeline already loads ai_settings into merchant_context
    (see `core.store_knowledge.build_merchant_context`). We re-use that
    dict here instead of going back to the DB so the prompt builder
    stays IO-free.

    Falls back to an empty dict — the High-Priority layer still renders
    the platform-wide baseline rules in that case.
    """
    mc = state.merchant_context or {}
    raw = mc.get("ai_settings") or mc.get("tenant_ai_settings") or {}
    if isinstance(raw, dict):
        return raw
    return {}


def _emit_prompt_log(
    *,
    state: BrainReplyState,
    persona: str,
    high_priority: str,
    identity: str,
    kb: str,
    tools: str,
    libraries: str,
    resolver_overlay: str,
    decision: str,
    residual: str,
    json_block: str,
    total: str,
) -> None:
    """Emit `[PROMPT_LAYERS]` structured log for the assembled prompt."""
    try:
        mc = state.merchant_context or {}
        tenant_id = mc.get("tenant_id") or mc.get("brain_profile", {}).get("tenant_id")
        payload = {
            "event":                     "brain_prompt_built",
            "tenant_id":                 tenant_id,
            "intent":                    state.intent_name or None,
            "stage":                     state.stage,
            "persona_chars":             len(persona),
            "high_priority_chars":       len(high_priority),
            "identity_chars":            len(identity),
            "kb_chars":                  len(kb),
            "tools_chars":               len(tools),
            "libraries_chars":           len(libraries),
            "resolver_overlay_chars":    len(resolver_overlay),
            "decision_chars":            len(decision),
            "residual_rules_chars":      len(residual),
            "brain_state_json_chars":    len(json_block),
            "total_prompt_chars":        len(total),
            "approx_tokens_total":       _approx_tokens(total),
            "approx_tokens_kb":          _approx_tokens(kb),
            "approx_tokens_high_pri":    _approx_tokens(high_priority),
            "has_kb":                    bool(kb),
            "has_tools_block":           bool(tools),
            "has_libraries":             bool(libraries),
            "has_resolver_overlay":      bool(resolver_overlay),
        }
        _log.info("[PROMPT_LAYERS] " + json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — logging must never break a turn
        pass


def _tone_instruction(tone: str) -> str:
    tone_map = {
        "formal": "رسمي ومحترم",
        "casual": "ودي ومريح",
        "brief": "مختصر جداً",
        "neutral": "ودود ومهني وواضح",
    }
    return tone_map.get(tone or "neutral", tone_map["neutral"])
