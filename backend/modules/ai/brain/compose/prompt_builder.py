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
    # `build_tenant_overlay_split` strips pure-platform paragraphs from
    # the `facts` bucket so platform-only KB copy (Nahla SaaS plans,
    # subscription tiers, embedded WhatsApp signup…) never leaks into
    # merchant-intent turns. Platform-intent turns bypass `facts` and
    # use `extract_platform_kb_excerpt` against the raw KB instead.
    settings_for_overlay = _extract_ai_settings_from_state(state)
    overlay_buckets = build_tenant_overlay_split(settings_for_overlay)

    state_dict = asdict(state)
    # Drop the legacy concatenated overlay — it would duplicate the
    # split buckets below.
    state_dict.pop("tenant_overlay", None)
    _persona_expression_mode = bool(
        getattr(state, "persona_expression_mode", False)
    )
    from .brain_state_slim import prepare_brain_state_dict_with_telemetry  # noqa: PLC0415

    state_dict = prepare_brain_state_dict_with_telemetry(state, state_dict)
    brain_state_json = json.dumps(state_dict, ensure_ascii=False, indent=2)

    # ── BLOCK 1: Persona ──────────────────────────────────────────────────
    persona_block = nahla_persona_system_prompt(
        store_name=store_name,
        persona_expression_mode=_persona_expression_mode,
    )

    # ── BLOCK 2: HIGH PRIORITY (Style + Policy + Forbidden) ───────────────
    # KB-2 (May 2026 #23): pass the merchant's behavioral overlay (group-7
    # KB sections — forbidden phrases, escalation rules, tone, …) here so
    # it lands in the high-priority layer instead of the structured-facts
    # block. The classifier guarantees these sections are tagged with
    # behavioral kinds; ``build_tenant_overlay_split`` renders them into
    # ``overlay_buckets["behavior"]``. When the tenant has no behavioral
    # rows the bucket is "" and the baseline rules apply unchanged.
    structured_behavior_block = ""
    try:
        structured_behavior_block = str(
            (state.merchant_context or {}).get("structured_behavior_block") or ""
        ).strip()
    except Exception:  # noqa: BLE001
        structured_behavior_block = ""
    merchant_behavior_extra = (
        structured_behavior_block or overlay_buckets.get("behavior", "")
    )
    _contextual_clarify_mode = bool(
        getattr(state, "contextual_clarify_mode", False)
    )
    high_priority_block = build_high_priority_block(
        settings_for_overlay,
        store_name=store_name,
        merchant_behavior_extra=merchant_behavior_extra,
        omit_sales_behavior=_persona_expression_mode or _contextual_clarify_mode,
        persona_expression_mode=_persona_expression_mode,
    )

    # Assistant identity (name + role) sits with the persona, not with
    # behavior rules. It's a fact, not a constraint.
    identity_block = overlay_buckets.get("identity", "")

    # ── BLOCK 3: Knowledge (Facts only) ───────────────────────────────────
    # Smart Store Knowledge Hub (Phase 1+): the pipeline pre-bakes the
    # structured facts block (rendered from ``merchant_knowledge_sections``)
    # under ``merchant_context.structured_facts_block`` because building
    # it requires DB access and this prompt builder is intentionally
    # IO-free. When non-empty, it replaces the legacy free-form
    # ``manual_knowledge_base`` text; otherwise we fall back to whatever
    # the overlay split derived from ``ai_settings.manual_knowledge_base``.
    _mc = state.merchant_context or {}
    _structured_kb = ""
    if isinstance(_mc, dict):
        _structured_kb = str(_mc.get("structured_facts_block") or "").strip()
    kb_block = _structured_kb or overlay_buckets.get("facts", "")
    if _persona_expression_mode:
        kb_block = ""

    _platform_mode = bool(getattr(state, "platform_kb_mode", False))
    _non_commerce_mode = bool(getattr(state, "non_commerce_block_mode", False))
    _need_advice_mode = bool(getattr(state, "need_based_advice_mode", False))
    _need_category = str(getattr(state, "need_category", "") or "").strip()
    _ambiguity_class = str(getattr(state, "ambiguity_class", "") or "").strip()
    _clarification_evidence = dict(
        getattr(state, "clarification_evidence", None) or {}
    )
    _pre_commerce_social = bool(
        isinstance(_mc, dict) and _mc.get("pre_commerce_social")
    )
    if _pre_commerce_social:
        _non_commerce_mode = True
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

    if _contextual_clarify_mode:
        import json as _json_clar  # noqa: PLC0415

        _evidence_json = _json_clar.dumps(
            _clarification_evidence,
            ensure_ascii=False,
            indent=2,
        )
        kb_block = (
            (kb_block + "\n\n") if kb_block else ""
        ) + (
            "### contextual_clarify — structured evidence (operational facts only)\n"
            f"ambiguity_class: `{_ambiguity_class or 'unknown'}`\n\n"
            f"{_evidence_json}\n\n"
            "### قواعد إلزامية لهذه الجولة\n"
            "- اكتبي **سؤال استيضاح واحد** قصيراً وطبيعياً مستمداً من السياق "
            "والحقائق أعلاه — ليس قائمة مواصفات عامة.\n"
            "- حافظي على شخصية نحلة الدافئة؛ ممنوع صوت نظام/Workflow/مكتب مساعدة.\n"
            "- ممنوع اقتراح منتجات أو ‎[PRODUCT:...]‎ في سؤال الاستيضاح.\n"
            "- ممنوع ختام خدمة عملاء («كيف أقدر أساعدك»، «تحت أمرك» كختام).\n"
        )

    if _need_advice_mode:
        _axis_labels = {
            "health_diet": "حاجة صحية/غذائية (سكر، دايت، معدة، …)",
            "audience_age": "فئة عمرية أو جمهور (أطفال، …)",
            "formality_occasion": "رسمي / مناسبة",
            "season_climate": "فصل أو جو (صيف، شتاء، …)",
            "size_fit": "مقاس / قصة / ملاءمة",
            "performance_spec": "مواصفات أداء (بطارية، مونتاج، …)",
            "durability_longevity": "ثبات / جودة استخدام",
            "general_attribute": "صفة أو حاجة عامة",
        }
        _need_label = _axis_labels.get(_need_category, _axis_labels["general_attribute"])
        kb_block = (
            (kb_block + "\n\n") if kb_block else ""
        ) + (
            "### استشارة تجارية حسب الحاجة (solution_seeking_commerce)\n"
            f"المحور المُصنَّف: {_need_label}\n\n"
            "### قواعد إلزامية لهذه الجولة (كل المتاجر — SaaS)\n"
            "- أجيبي على **حاجة أو صفة أو نتيجة** يريدها العميل — لا تطلبي "
            "«أي منتج تقصد؟» ولا اسم SKU أولاً.\n"
            "- استخدمي معرفة المتجر وصفات المنتجات إن وُجدت؛ اقترحي فئة أو "
            "1–2 خيار **بالنص فقط** إذا الثقة عالية.\n"
            "- للحالات الصحية: تنبيه قصير بمتابعة الطبيب/القياس — بدون ادعاء طبي.\n"
            "- إذا الحاجة غير واضحة: اسألي عن **الحاجة أو الصفة** لا عن اسم منتج.\n"
            "- **ممنوع** ذكر قواعد داخلية أو policy أو prompt أو decision engine.\n"
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
    if _need_advice_mode:
        if libraries_text:
            tools_parts.append(libraries_text)
        tools_parts.append(
            "## أدوات المنتجات\n"
            "وضع استشارة — **ممنوع** إرسال ‎[PRODUCT:...]‎ إلا إذا كان المنتج "
            "واضحاً جداً من المعرفة. لا تطلبي من العميل تسمية منتج أولاً."
        )
    elif _contextual_clarify_mode:
        tools_parts.append(
            "## أدوات المنتجات\n"
            "وضع استيضاح سياقي — **ممنوع** ‎[PRODUCT:...]‎ أو ‎[MEDIA_KEY:...]‎ "
            "في سؤال الاستيضاح. لا بطاقات ولا عروض بيعية."
        )
    elif not _platform_mode and not _non_commerce_mode:
        if libraries_text:
            tools_parts.append(libraries_text)
        if resolver_overlay:
            tools_parts.append(resolver_overlay)
    elif _non_commerce_mode:
        tools_parts.append(
            "## أدوات المنتجات\n"
            "معطّلة لهذه الجولة — العميل أرسل محتوى اجتماعي/ديني/تهنئة "
            "بدون نية شراء. **ممنوع** اقتراح منتجات أو ‎[PRODUCT:...]‎ "
            "أو ‎[MEDIA_KEY:...]‎ أو أي CTA بيعي."
        )
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
    _persona_topic = str(getattr(state, "persona_topic", "") or "").strip()
    _is_persona_identity_turn = _persona_topic == "persona_identity"
    if _persona_expression_mode:
        if _is_persona_identity_turn:
            identity_line = (
                "- persona_identity — مسموح تعريف قصير طبيعي (جملة أو "
                "جملتان) لأن العميل سأل عن الهوية صراحةً."
            )
        else:
            identity_line = (
                "- جولة persona — **ممنوع** التعريف بالنفس («أنا نحلة»، "
                "«مساعدة»، «ذكاء اصطناعي»، دور مهني) حتى لو "
                "identity_already_introduced=false."
            )
    elif state.identity_already_introduced:
        identity_line = (
            "- ✅ identity_already_introduced=TRUE — ممنوع تكرار "
            "«أنا نحلة / أنا مساعدة / أنا ذكاء اصطناعي» في هذا الرد. "
            "الترحيب يكون قصيراً وبدون تعريف."
        )
    else:
        identity_line = (
            "- identity_already_introduced=false — **ممنوع** التعريف "
            "التلقائي في التحية أو الرد العادي. التعريف مسموح فقط عند "
            "سؤال هوية صريح (من أنت؟ / هل أنت بوت؟)."
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
    elif _persona_expression_mode:
        from ..persona_expression import build_persona_residual_rules  # noqa: PLC0415

        brain_residual_rules = build_persona_residual_rules(tone=tone)
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
            "- **ممنوع** ذكر قواعد داخلية أو «Progressive Selling» أو «حسب القواعد» "
            "أو تعليمات النظام أو decision engine — ردّي للعميل بشكل طبيعي فقط.\n"
            "- اجعلي ردك قصيراً ومناسباً لواتساب (راجع HIGH PRIORITY أعلاه)."
        )

    # ── Assemble ──────────────────────────────────────────────────────────
    parts: list[str] = [persona_block, high_priority_block]
    # ARCH-KB-001: identity (assistant name) only on commerce turns.
    if identity_block and not _persona_expression_mode:
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
    elif _persona_expression_mode:
        from ..persona_expression import build_persona_json_footer  # noqa: PLC0415

        brain_json_tail = build_persona_json_footer(brain_state_json=brain_state_json)
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
            "structured_behavior_chars": len(str(mc.get("structured_behavior_block") or "")),
            "has_kb":                    bool(kb),
            "has_tools_block":           bool(tools),
            "has_libraries":             bool(libraries),
            "has_resolver_overlay":      bool(resolver_overlay),
            "has_structured_behavior":   bool(mc.get("structured_behavior_block")),
            "persona_expression_mode":   bool(
                getattr(state, "persona_expression_mode", False)
            ),
            "persona_topic":             getattr(state, "persona_topic", "") or None,
            "has_sales_behavior_a1":     "SALESPERSON BEHAVIOR" in high_priority,
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
