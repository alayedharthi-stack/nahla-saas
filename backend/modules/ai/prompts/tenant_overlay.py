"""
prompts/tenant_overlay.py
─────────────────────────
Tenant Assistant Settings → Prompt Overlay.

Converts the merchant's dashboard AI settings (stored in TenantSettings.ai_settings
JSONB) into a stable, structured prompt block that is injected into the existing
system prompt — without replacing or restructuring the base prompt.

Design constraints:
  - Pure normalization layer: maps UI-friendly values to stable model instructions.
  - Safe fallback: returns "" when settings are absent, so the AI behaves exactly
    as before for tenants without customized settings.
  - Non-breaking: injected alongside (not instead of) the base system prompt.
  - No provider selection, routing, or fallback logic changes.

Smart Store Knowledge Hub (Phase 1+):
  - When the tenant has rows in ``merchant_knowledge_sections``, the
    facts bucket is built from those structured rows (grouped by the
    six dashboard buckets, with linked media surfaced as
    ``[MEDIA_KEY:<slug>]`` markers).
  - When no structured rows exist, we fall back to the legacy free-form
    ``ai_settings.manual_knowledge_base`` text — preserving the old
    behaviour for tenants who haven't migrated yet.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.ai.overlay")

# ── Normalization maps ────────────────────────────────────────────────────────
# Keys support both English enum values (stored in DB) and Arabic UI labels
# that merchants may set in future UI iterations.

TONE_MAP: Dict[str, str] = {
    "friendly":  "ودي وطبيعي — تحدث كصديق ينصح بصدق، لا كموظف يبيع بأي ثمن.",
    "formal":    "رسمي ومحترم — استخدم لغة مهنية وألقاب احترام مع كل عميل.",
    "casual":    "عفوي ومرح — تحدث بأسلوب يومي خفيف كأنك تكلّم صاحب.",
    "brief":     "مختصر ومباشر — أقل كلام ممكن لتوصيل المعلومة بوضوح.",
    "neutral":   "متوازن ومهني — ودود لكن بدون مبالغة في الألفة.",
    "ودية وقريبة":    "ودي وطبيعي — تحدث كصديق ينصح بصدق، لا كموظف يبيع بأي ثمن.",
    "رسمية ومحترمة":  "رسمي ومحترم — استخدم لغة مهنية وألقاب احترام مع كل عميل.",
    "مرحة وخفيفة":    "عفوي ومرح — تحدث بأسلوب يومي خفيف كأنك تكلّم صاحب.",
    "مختصرة ومباشرة": "مختصر ومباشر — أقل كلام ممكن لتوصيل المعلومة بوضوح.",
}

LANGUAGE_MAP: Dict[str, str] = {
    "arabic": (
        "تحدث بالعربية واللهجة السعودية العامية الصحيحة دائماً. "
        "إذا بدأ العميل بالإنجليزية أو طلب التحدث بالإنجليزية، انتقل للإنجليزية."
    ),
    "english": (
        "Reply in English. Switch to Arabic only if the customer explicitly "
        "requests it or writes in Arabic."
    ),
    "bilingual": (
        "تحدث بنفس لغة العميل — إذا كتب بالعربية ردّ بالعربية، وإذا كتب "
        "بالإنجليزية ردّ بالإنجليزية. يمكنك مزج اللغتين إذا العميل يفعل ذلك."
    ),
    "عربي":          "تحدث بالعربية واللهجة السعودية العامية دائماً.",
    "إنجليزي":       "Reply in English only.",
    "ثنائي اللغة":   "تحدث بنفس لغة العميل — عربي يردّ عربي، إنجليزي يردّ إنجليزي.",
}

LENGTH_MAP: Dict[str, str] = {
    "short":  "ردودك قصيرة جداً — جملة إلى جملتين كحد أقصى. لا شرح إضافي إلا إذا طُلب صراحةً.",
    "medium": "ردودك متوسطة — 3 إلى 4 أسطر كحد أقصى. اختصر دائماً.",
    "long":   "يمكنك الرد بتفصيل عند الحاجة — لكن لا تتجاوز 6 أسطر في الغالب.",
    "قصير":  "ردودك قصيرة جداً — جملة إلى جملتين كحد أقصى.",
    "متوسط": "ردودك متوسطة — 3 إلى 4 أسطر كحد أقصى. اختصر دائماً.",
    "مفصل":  "يمكنك الرد بتفصيل عند الحاجة — لكن لا تتجاوز 6 أسطر.",
}


# ── Structured facts (Smart Store Knowledge Hub) ────────────────────────────


# Section-group → Arabic heading. Keep in lockstep with
# ``services/knowledge_section_kinds.GROUP_LABELS_AR`` — duplicated here
# so this module stays import-light when ``models`` is unavailable
# (e.g. during prompt unit tests).
_GROUP_HEADINGS_AR: Dict[int, str] = {
    1: "تحديثات سريعة من التاجر",
    2: "معلومات المتجر",
    3: "سياسات البيع",
    4: "سياسات الشحن",
    5: "معلومات إضافية عن المنتجات",
    6: "وسائط مرتبطة",
}

_PRECEDENCE_NOTE_AR = (
    "ملاحظات لاستخدام قاعدة المعرفة:\n"
    "- استخدم هذه المعلومات للإجابة عن السياسات، طريقة الرد، اللهجة، أوقات "
    "العمل، الفروع، الفوائد، الوصفات، طريقة الاستخدام، والأسئلة الشائعة.\n"
    "- إذا كان المتجر مربوطًا بمنصة تجارية (سلة/زد/شوبيفاي): السعر، التوفر، "
    "المخزون، المتغيرات، رابط المنتج المباشر، والصور الأساسية تأتي من بيانات "
    "المنصة في merchant_context وهي المصدر الرسمي — لا تستخدم أي رقم سعر أو "
    "حالة توفر من هذه القاعدة لمخالفتها.\n"
    "- إذا تعارض السعر/المخزون هنا مع بيانات المنصة، اعتمد بيانات المنصة "
    "دائمًا ولا تذكر الرقم اليدوي.\n"
    "- لا تختلق معلومات ليست في القاعدة أو في merchant_context."
)


def _render_section_block(section: Any, *, index: int = 0) -> str:
    """Render a single ``MerchantKnowledgeSection`` row as Arabic prompt text.

    Phase 4 — facts rewrite
    ───────────────────────
    Each block is prefixed with the section number inside its group
    (e.g. ``### 2. الإرجاع والاستبدال``). Product-scoped sections
    surface their scope inline (``(منتجات: 7, 12)``) so Claude knows
    the section only applies to those products. ``[MEDIA_KEY:...]``
    markers are appended for any linked media that has a ``media_key``
    set in the registry, so Claude can reliably emit the marker when
    citing the section. Links without a ``media_key`` are omitted from
    the prompt — the relevance ranker handles them at attach time.
    """
    title = (getattr(section, "title", None) or "").strip()
    body = (getattr(section, "body", None) or "").strip()

    # Phase 3 — surface product scope on the header line.
    product_links = list(getattr(section, "product_links", None) or [])
    scope_tag = ""
    if product_links:
        ids = sorted({int(pl.product_id) for pl in product_links})
        scope_tag = f" (منتجات: {', '.join(str(i) for i in ids)})"

    lines: List[str] = []
    if title:
        prefix = f"### {index}. " if index > 0 else "### "
        lines.append(f"{prefix}{title}{scope_tag}")
    elif scope_tag:
        # No explicit title but we still want the scope hint visible.
        lines.append(f"### {index}.{scope_tag}" if index > 0 else f"### {scope_tag}")
    if body:
        lines.append(body)

    media_links = list(getattr(section, "media_links", None) or [])
    media_markers: List[str] = []
    for lk in media_links:
        media = getattr(lk, "media", None)
        if media is None:
            continue
        if not bool(getattr(media, "is_active", True)):
            continue
        key = (getattr(media, "media_key", None) or "").strip()
        if key:
            media_markers.append(f"[MEDIA_KEY:{key}]")
    if media_markers:
        lines.append("الوسائط: " + " ".join(media_markers))

    return "\n".join(lines).strip()


def build_structured_facts_block(
    db: Any,
    tenant_id: int,
    *,
    active_product_ids: Optional[set] = None,
) -> str:
    """Build the facts bucket from ``merchant_knowledge_sections`` rows.

    Returns "" if the tenant has no active structured sections — the
    caller should then fall back to the legacy ``manual_knowledge_base``
    text. We intentionally swallow all exceptions and log a warning so
    the AI pipeline never breaks because of a KB query failure.

    Phase 3 — product scoping
    ─────────────────────────
    A section can carry rows in ``merchant_knowledge_section_products``;
    those sections are "product-scoped" and only relevant when the
    current conversation is about one of the linked products. The
    pipeline passes ``active_product_ids`` (a set of catalog product
    ids resolved upstream from the conversation context); when it's
    ``None`` (no resolver context yet) we keep ALL product-scoped
    sections, so day-1 deployments stay informative. When it's an
    explicit empty set, we drop product-scoped sections (the caller
    has signalled "no product context here, please hide product-only
    extras to keep the prompt short").
    """
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from services.knowledge_section_kinds import group_for  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB.facts] import failed: %s", exc)
        return ""

    try:
        rows = (
            db.query(MerchantKnowledgeSection)
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.is_active.is_(True),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB.facts] query failed for tenant=%s: %s", tenant_id, exc)
        return ""

    if not rows:
        return ""

    # ── Phase 3 product-scope filter ────────────────────────────────────
    filtered_rows: List[Any] = []
    scoped_dropped = 0
    for r in rows:
        linked_pids = {
            int(lk.product_id)
            for lk in (getattr(r, "product_links", None) or [])
        }
        if not linked_pids:
            # Global section — always include.
            filtered_rows.append(r)
            continue
        # Product-scoped: include only if the caller didn't supply a
        # product hint, OR the hint intersects this section's products.
        if active_product_ids is None:
            filtered_rows.append(r)
        elif active_product_ids & linked_pids:
            filtered_rows.append(r)
        else:
            scoped_dropped += 1
    rows = filtered_rows

    if not rows:
        return ""

    grouped: Dict[int, List[Any]] = {}
    for r in rows:
        grouped.setdefault(group_for(r.kind), []).append(r)

    parts: List[str] = ["قاعدة المعرفة (معلومات المتجر — Facts فقط):"]
    for gid in sorted(grouped.keys()):
        heading = _GROUP_HEADINGS_AR.get(gid, "معلومات إضافية")
        # Group 6 in the dashboard is "linked media library" — purely
        # navigational, not its own facts. Skip it for the prompt.
        if gid == 6:
            continue
        section_blocks = [
            _render_section_block(r, index=idx)
            for idx, r in enumerate(grouped[gid], start=1)
        ]
        section_blocks = [b for b in section_blocks if b]
        if not section_blocks:
            continue
        parts.append(f"## {heading}\n\n" + "\n\n".join(section_blocks))

    parts.append(_PRECEDENCE_NOTE_AR)

    media_marker_count = sum(
        1
        for r in rows
        for lk in (getattr(r, "media_links", None) or [])
        if (getattr(lk, "media", None) is not None)
        and (getattr(lk.media, "media_key", None) or "").strip()
    )
    logger.info(
        "[KB.facts] tenant=%s sections=%d kinds=%s media_markers=%d "
        "scoped_dropped=%d active_pids=%s",
        tenant_id, len(rows), sorted({r.kind for r in rows}), media_marker_count,
        scoped_dropped,
        sorted(active_product_ids) if active_product_ids else None,
    )
    return "\n\n".join(parts)


def build_tenant_overlay_split(
    settings: Optional[Dict[str, Any]],
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> Dict[str, str]:
    """
    Split a tenant's ai_settings into the three architectural buckets:

        {
          "identity": "<اسم/دور المساعد>",          # neutral, free-form
          "style":    "<style block contents>",      # → consumed by High-Priority layer
          "policy":   "<policy block contents>",     # → consumed by High-Priority layer
          "facts":    "<facts-only KB body>",        # → consumed by KB block
        }

    Keys are always present (empty string when no data). This is the
    primary feed for the new 3-layer prompt structure introduced in
    Phase 1 of the prompt-pipeline refactor. The legacy single-string
    overlay (`build_tenant_prompt_overlay`) is now a thin wrapper that
    concatenates these buckets for callers that haven't migrated yet.
    """
    buckets = {"identity": "", "style": "", "policy": "", "facts": ""}
    if not settings:
        return buckets

    # ── identity ──────────────────────────────────────────────────────────
    name = str(settings.get("assistant_name") or "").strip()
    role = str(settings.get("assistant_role") or "").strip()
    if name or role:
        identity_lines: list[str] = []
        if name:
            identity_lines.append(f"- اسمك: {name}")
        if role:
            identity_lines.append(f"- دورك: {role}")
        buckets["identity"] = "هوية المساعد:\n" + "\n".join(identity_lines)

    # ── style (tone + language + length) ──────────────────────────────────
    style_parts: list[str] = []
    tone_key = str(settings.get("reply_tone") or "").strip()
    tone_instruction = TONE_MAP.get(tone_key)
    if tone_instruction:
        style_parts.append(f"النبرة المطلوبة: {tone_instruction}")
    lang_key = str(settings.get("default_language") or "").strip()
    lang_instruction = LANGUAGE_MAP.get(lang_key)
    if lang_instruction:
        style_parts.append(f"لغة الرد: {lang_instruction}")
    length_key = str(settings.get("reply_length") or "").strip()
    length_instruction = LENGTH_MAP.get(length_key)
    if length_instruction:
        style_parts.append(f"طول الرد: {length_instruction}")
    if style_parts:
        buckets["style"] = "\n\n".join(style_parts)

    # ── policy (owner_instructions + coupons + escalation) ────────────────
    policy_parts: list[str] = []
    owner_instructions = str(settings.get("owner_instructions") or "").strip()
    if owner_instructions:
        policy_parts.append(f"تعليمات صاحب المتجر:\n{owner_instructions}")
    coupon_rules = str(settings.get("coupon_rules") or "").strip()
    allowed_discount = str(settings.get("allowed_discount_levels") or "").strip()
    if coupon_rules or allowed_discount:
        disc_lines: list[str] = []
        if coupon_rules:
            disc_lines.append(coupon_rules)
        if allowed_discount:
            disc_lines.append(f"- الحد الأقصى المسموح للخصم: {allowed_discount}%")
        policy_parts.append(
            "قواعد الخصومات والكوبونات:\n" + "\n".join(disc_lines)
        )
    escalation_rules = str(settings.get("escalation_rules") or "").strip()
    if escalation_rules:
        policy_parts.append(f"قواعد التحويل والتصعيد:\n{escalation_rules}")
    if policy_parts:
        buckets["policy"] = "\n\n".join(policy_parts)

    # ── facts ────────────────────────────────────────────────────────────
    # Smart Store Knowledge Hub (Phase 1+):
    #   * If the tenant has structured rows in
    #     ``merchant_knowledge_sections``, build the facts block from
    #     those — grouped by dashboard bucket, with linked media keys
    #     surfaced as ``[MEDIA_KEY:<slug>]`` markers.
    #   * Otherwise fall back to the legacy free-form
    #     ``manual_knowledge_base`` text. This keeps every existing
    #     tenant working until they migrate.
    #
    # CRITICAL DESIGN RULE — do NOT collapse this into owner_instructions:
    #   * owner_instructions       = how the assistant *behaves*  (→ policy)
    #   * structured KB / legacy text = facts the assistant can cite (→ facts)
    # The block is tagged as a non-authoritative source for prices/inventory
    # so that Salla-synced data (loaded via core.store_knowledge.build_
    # merchant_context) always wins on those fields, even if the merchant
    # accidentally pasted stale prices in here.
    #
    # MERCHANT-MODE PLATFORM SCOPE (May 2026 #20):
    # Nahla SaaS is built on top of آل عايد للعسل البلدي and merchants are
    # encouraged to keep a short Nahla-platform brief inside their KB so
    # platform-curious customers / peers can be answered. That info is
    # great for platform-intent turns and a footgun for product-intent
    # turns — without filtering, the model sees "باقات نحلة 899 ريال"
    # next to honey copy and quotes the SaaS plans when the customer
    # asks "اسعار الباقات" of the store. The platform-intent path
    # (`extract_platform_kb_excerpt`) reads the raw KB directly from
    # `merchant_context.ai_settings.manual_knowledge_base`, so stripping
    # pure-platform paragraphs from the `facts` bucket here does NOT
    # affect that path — it only protects the default merchant flow.
    structured_facts = ""
    if db is not None and tenant_id is not None:
        try:
            structured_facts = build_structured_facts_block(db, int(tenant_id))
        except Exception as exc:  # noqa: BLE001 — never break overlay build
            logger.warning(
                "[KB.facts] structured build failed for tenant=%s: %s",
                tenant_id, exc,
            )
    if structured_facts:
        buckets["facts"] = structured_facts
        return buckets

    knowledge_base_raw = str(settings.get("manual_knowledge_base") or "").strip()
    knowledge_base = knowledge_base_raw
    if knowledge_base_raw:
        try:
            from modules.ai.brain.knowledge_platform_slice import (  # noqa: PLC0415
                extract_merchant_kb_excerpt,
            )
            filtered_kb, dropped = extract_merchant_kb_excerpt(knowledge_base_raw)
            if filtered_kb:
                knowledge_base = filtered_kb
            if dropped > 0:
                logger.info(
                    "[MERCHANT_KB_SCOPE] dropped_platform_chunks=%d "
                    "(kept_chars=%d / raw_chars=%d)",
                    dropped, len(knowledge_base), len(knowledge_base_raw),
                )
        except Exception as exc:  # noqa: BLE001 — never break overlay build
            logger.warning("[MERCHANT_KB_SCOPE] filter failed: %s", exc)

    if knowledge_base:
        buckets["facts"] = (
            "قاعدة المعرفة (معلومات المتجر — Facts فقط):\n"
            f"{knowledge_base}\n\n"
            "ملاحظات لاستخدام قاعدة المعرفة:\n"
            "- استخدم هذه المعلومات للإجابة على أسئلة العملاء عن المنتجات "
            "والشحن والضمان والأسئلة الشائعة وأي تفاصيل أضافها التاجر هنا.\n"
            "- إذا كان المتجر مربوطًا بسلة فإن السعر، التوفر، المخزون، "
            "المتغيرات، ورابط المنتج المباشر تأتي من بيانات سلة في "
            "merchant_context وهي المصدر الرسمي — لا تستخدم أي رقم سعر أو "
            "حالة توفر من هذه القاعدة لمخالفة بيانات سلة.\n"
            "- إذا تعارض السعر هنا مع سعر سلة، اعتمد سعر سلة دائمًا ولا "
            "تذكر السعر اليدوي.\n"
            "- لا تختلق معلومات ليست في القاعدة أو في merchant_context."
        )

    return buckets


def build_tenant_prompt_overlay(
    settings: Optional[Dict[str, Any]],
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> str:
    """
    Backward-compatible wrapper.

    Returns the legacy single-string overlay by concatenating the
    output of `build_tenant_overlay_split`. New callers should consume
    the split dict directly so the High-Priority layer can pull style +
    policy out of the system prompt and into the priority banner.

    When ``db`` and ``tenant_id`` are passed, the facts bucket is
    sourced from the structured ``merchant_knowledge_sections`` table
    (Smart Store Knowledge Hub). Without them, the legacy free-form
    ``manual_knowledge_base`` text is used.

    Returns "" if settings is None/empty — this preserves current AI behavior
    for tenants that have not customized their assistant.
    """
    if not settings:
        return ""

    buckets = build_tenant_overlay_split(settings, db=db, tenant_id=tenant_id)
    sections: list[str] = []
    if buckets["identity"]:
        sections.append(buckets["identity"])
    if buckets["style"]:
        sections.append(buckets["style"])
    if buckets["policy"]:
        sections.append(buckets["policy"])
    if buckets["facts"]:
        sections.append(buckets["facts"])

    if not sections:
        return ""

    return "\n\n".join([
        "═══ إعدادات مساعد المتجر (تُطبّق بأولوية عالية) ═══",
        *sections,
        "═══ نهاية إعدادات المتجر ═══",
    ])


def load_tenant_ai_overlay(db: Any, tenant_id: int) -> str:
    """
    Load tenant AI settings from DB, merge with defaults, and return
    the rendered prompt overlay string.

    Safe: returns "" on any error so the AI pipeline never breaks.
    """
    try:
        from models import TenantSettings  # noqa: PLC0415
        from core.tenant import merge_ai_defaults  # noqa: PLC0415

        ts = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if not ts:
            return ""

        settings = merge_ai_defaults(ts.ai_settings)
        return build_tenant_prompt_overlay(settings, db=db, tenant_id=tenant_id)
    except Exception as exc:
        logger.warning(
            "[overlay] Failed to load AI settings for tenant=%s: %s",
            tenant_id, exc,
        )
        return ""


def get_tenant_tone(db: Any, tenant_id: int) -> str:
    """
    Return the normalized tone key for the Brain prompt builder.

    Falls back to "neutral" on any failure.
    """
    try:
        from models import TenantSettings  # noqa: PLC0415
        from core.tenant import merge_ai_defaults  # noqa: PLC0415

        ts = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if ts:
            settings = merge_ai_defaults(ts.ai_settings)
            return str(settings.get("reply_tone") or "neutral")
    except Exception:
        pass
    return "neutral"
