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

import json
import logging
import re
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
#
# KB-2 (May 2026 #23): group 7 ("سلوك المساعد") is intentionally
# omitted here — behavioral sections must NEVER appear inside the
# structured-facts block (Block 3 of the prompt). They flow through
# ``build_behavioral_overlay_block`` into the high-priority layer
# instead, so a "لا تقل حبيبي" line cannot leak into the same channel
# that holds payment / shipping facts and contaminate retrieval.
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

_PHONE_HINT_RE = re.compile(r"(?:\+?966|00966|0)?5\d[\d\s\-()]{7,12}")
_URL_HINT_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_MAPS_HINT_RE = re.compile(
    r"(?:maps\.app\.goo\.gl|google\.[^/\s]+/maps|goo\.gl/maps|خرائط|الخرايط|لوكيشن|location)",
    re.IGNORECASE,
)
_PAYMENT_BARCODE_HINT_RE = re.compile(
    r"(?:باركود|كيو\s*آر|qr|رمز\s+الدفع|payment_[\w-]*(?:barcode|qr))",
    re.IGNORECASE,
)
_STAFF_CONTACT_HINT_RE = re.compile(
    r"(?:أمين|امين|بائع|المعرض|موظف|الموظف|مسؤول|المسؤول|الإدارة|الادارة|تواصل|واتساب)",
    re.IGNORECASE,
)


def _compact_section_ref(section: Any) -> Dict[str, Any]:
    title = (getattr(section, "title", None) or "").strip()
    return {
        "id": getattr(section, "id", None),
        "kind": (getattr(section, "kind", "") or "").strip(),
        "title": title[:80],
        "body_chars": len((getattr(section, "body", None) or "").strip()),
    }


def _asset_flags_for_section(section: Any) -> List[str]:
    text = " ".join(
        [
            str(getattr(section, "title", "") or ""),
            str(getattr(section, "body", "") or ""),
            " ".join(
                (getattr(getattr(lk, "media", None), "media_key", "") or "")
                for lk in (getattr(section, "media_links", None) or [])
            ),
        ]
    )
    flags: List[str] = []
    if _PHONE_HINT_RE.search(text) and _STAFF_CONTACT_HINT_RE.search(text):
        flags.append("staff_contact")
    if _PAYMENT_BARCODE_HINT_RE.search(text):
        flags.append("payment_barcode")
    if _MAPS_HINT_RE.search(text):
        flags.append("maps")
    urls = _URL_HINT_RE.findall(text)
    if urls:
        flags.append("url")
    return flags


def _emit_kb_runtime_trace(
    *,
    tenant_id: int,
    channel: str,
    queried_rows: List[Any],
    included_rows: List[Any],
    dropped_behavioral: int = 0,
    dropped_product_scope: int = 0,
) -> None:
    """Log which structured KB rows are visible to a runtime prompt layer."""

    try:
        asset_flags: Dict[str, int] = {}
        for row in included_rows:
            for flag in _asset_flags_for_section(row):
                asset_flags[flag] = asset_flags.get(flag, 0) + 1
        payload = {
            "tenant_id": tenant_id,
            "channel": channel,
            "queried_sections": len(queried_rows),
            "included_sections": len(included_rows),
            "dropped_behavioral": dropped_behavioral,
            "dropped_product_scope": dropped_product_scope,
            "included_kinds": sorted({
                (getattr(r, "kind", "") or "").strip()
                for r in included_rows
                if (getattr(r, "kind", "") or "").strip()
            }),
            "asset_flags": asset_flags,
            "sections": [_compact_section_ref(r) for r in included_rows[:40]],
        }
        logger.info(
            "[KB.RUNTIME_INGESTION] "
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[KB.RUNTIME_INGESTION] trace failed tenant=%s channel=%s err=%s",
            tenant_id,
            channel,
            exc,
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
        from core.knowledge import (  # noqa: PLC0415
            apply_ai_visible_kb_query_filters,
            is_imported_document_section,
            section_has_catalog_active_product,
        )
        from services.knowledge_section_kinds import (  # noqa: PLC0415
            group_for,
            is_behavioral_kind,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB.facts] import failed: %s", exc)
        return ""

    try:
        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(MerchantKnowledgeSection.tenant_id == tenant_id)
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

    # ── Pack A1: imported long-form documents are retrieval-only ────────
    # Salla CMS page bodies must NOT dump into every turn via the always-on
    # facts overlay. They are reachable only through capped relevance retrieval.
    imported_dropped = 0
    pre_import_total = len(rows)
    queried_rows_all = list(rows)
    rows = [r for r in rows if not is_imported_document_section(r)]
    imported_dropped = pre_import_total - len(rows)

    # ── KB-2 behavioral filter ──────────────────────────────────────────
    # Behavioral sections (group 7) are deliberately excluded from the
    # facts block. They are surfaced through ``build_behavioral_overlay_
    # block`` into the high-priority layer instead. Counting drops here
    # gives us a one-glance audit when an overlay change is suspected of
    # silently swallowing knowledge rows.
    behavioral_dropped = 0
    pre_behavioral_total = len(rows)
    rows = [r for r in rows if not is_behavioral_kind(getattr(r, "kind", None))]
    behavioral_dropped = pre_behavioral_total - len(rows)

    if not rows:
        # Only behavioral/imported rows existed — facts bucket is empty by design.
        _emit_kb_runtime_trace(
            tenant_id=tenant_id,
            channel="facts",
            queried_rows=queried_rows_all,
            included_rows=[],
            dropped_behavioral=behavioral_dropped,
        )
        logger.info(
            "[KB.facts] tenant=%s only behavioral/imported rows present "
            "(behavioral_dropped=%d imported_dropped=%d); facts bucket empty.",
            tenant_id, behavioral_dropped, imported_dropped,
        )
        return ""

    # ── Phase 3 product-scope filter ────────────────────────────────────
    filtered_rows: List[Any] = []
    scoped_dropped = 0
    catalog_dropped = 0
    queried_non_behavior_rows = list(rows)
    for r in rows:
        linked_pids = {
            int(lk.product_id)
            for lk in (getattr(r, "product_links", None) or [])
        }
        if linked_pids and not section_has_catalog_active_product(db, tenant_id, r):
            catalog_dropped += 1
            continue
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
        _emit_kb_runtime_trace(
            tenant_id=tenant_id,
            channel="facts",
            queried_rows=queried_non_behavior_rows,
            included_rows=[],
            dropped_behavioral=behavioral_dropped,
            dropped_product_scope=scoped_dropped,
        )
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

    # ── KB_MEDIA_RESOLUTION (May 2026 #29) ──────────────────────────────
    # Per-section media inventory log: makes it possible to diagnose
    # "the AI didn't send the barcode" by checking whether the KB
    # facts block exposed the marker to Claude at all. Emitted once
    # per audit pass with the full per-section breakdown.
    try:
        for r in rows:
            section_links = [
                lk for lk in (getattr(r, "media_links", None) or [])
                if getattr(getattr(lk, "media", None), "is_active", True)
            ]
            if not section_links:
                continue
            media_keys = sorted({
                (getattr(lk.media, "media_key", None) or "").strip()
                for lk in section_links
                if getattr(lk, "media", None) is not None
            })
            selected_keys = sorted({k for k in media_keys if k})
            logger.info(
                "[KB_MEDIA_RESOLUTION] tenant_id=%s section_id=%s kind=%s "
                "linked_media_count=%d media_keys=%s selected_media_keys=%s "
                "reason=%s",
                tenant_id,
                getattr(r, "id", None),
                getattr(r, "kind", "") or "",
                len(section_links),
                ",".join(media_keys) or "-",
                ",".join(selected_keys) or "-",
                "exposed_to_prompt" if selected_keys else "no_media_key_set",
            )
    except Exception as exc:  # noqa: BLE001 — never break facts on logging
        logger.warning(
            "[KB_MEDIA_RESOLUTION] log emit failed tenant=%s err=%s",
            tenant_id, exc,
        )

    logger.info(
        "[KB.facts] tenant=%s sections=%d kinds=%s media_markers=%d "
        "scoped_dropped=%d behavioral_dropped=%d active_pids=%s",
        tenant_id, len(rows), sorted({r.kind for r in rows}), media_marker_count,
        scoped_dropped, behavioral_dropped,
        sorted(active_product_ids) if active_product_ids else None,
    )
    _emit_kb_runtime_trace(
        tenant_id=tenant_id,
        channel="facts",
        queried_rows=queried_non_behavior_rows,
        included_rows=rows,
        dropped_behavioral=behavioral_dropped,
        dropped_product_scope=scoped_dropped,
    )
    return "\n\n".join(parts)


# ── KB-2 behavioral overlay block ────────────────────────────────────────────


# Subtype → Arabic section heading inside the behavioral overlay. Kept
# in lockstep with ``services.knowledge_section_kinds.BEHAVIORAL_KINDS``.
# Order matters: we render the most-impactful rules first (forbidden
# phrases + escalation) so that, if Claude's prompt is truncated under
# a long conversation, the highest-priority behavior is preserved.
_BEHAVIORAL_HEADINGS_AR: Dict[str, str] = {
    "forbidden_phrases":  "كلمات وعبارات ممنوعة",
    "escalation_rules":   "متى تحوّل لموظف بشري",
    "compliance_rules":   "قواعد امتثال إلزامية",
    "response_tone":      "نبرة الرد المطلوبة",
    "allowed_style":      "أسلوب الكلام المسموح",
    "emoji_policy":       "سياسة الإيموجي",
    "owner_identity":     "هوية صاحب المتجر",
    "assistant_identity": "هوية المساعد",
}


def build_behavioral_overlay_block(db: Any, tenant_id: int) -> str:
    """Render the merchant's behavioral KB sections for the high-priority layer.

    Reads ``merchant_knowledge_sections`` rows with ``kind`` in
    ``BEHAVIORAL_KINDS`` (group 7) and emits a compact, Claude-ready
    Arabic block. Returns "" when no behavioral rows exist — the
    high-priority layer then falls back to baseline rules only.

    Why a separate block instead of folding into ``facts``:
      * Behavior is an OVERLAY on platform-wide rules — not facts to
        cite. It needs the "outranks the KB" banner that the high-
        priority layer carries.
      * Keeping them out of ``facts`` prevents the model from quoting
        a tone rule when a customer asks "كيف أحوّل لكم؟" (which used
        to happen with the unified bucket).
      * The retrieval ranker never weighs behavior rows against
        commerce rows — they live in a different channel entirely.
    """
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415
        from services.knowledge_section_kinds import (  # noqa: PLC0415
            BEHAVIORAL_KINDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB.behavior] import failed: %s", exc)
        return ""

    try:
        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.kind.in_(list(BEHAVIORAL_KINDS)),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[KB.behavior] query failed for tenant=%s: %s", tenant_id, exc,
        )
        return ""

    if not rows:
        return ""

    grouped: Dict[str, List[Any]] = {}
    for r in rows:
        grouped.setdefault((r.kind or "").strip().lower(), []).append(r)

    parts: List[str] = []
    # Iterate by canonical order, not by Python dict order.
    for subtype, heading in _BEHAVIORAL_HEADINGS_AR.items():
        group_rows = grouped.get(subtype) or []
        if not group_rows:
            continue
        section_lines: List[str] = [f"• {heading}:"]
        for r in group_rows:
            title = (getattr(r, "title", None) or "").strip()
            body = (getattr(r, "body", None) or "").strip()
            if title and body:
                section_lines.append(f"  - {title}: {body}")
            elif body:
                section_lines.append(f"  - {body}")
            elif title:
                section_lines.append(f"  - {title}")
        parts.append("\n".join(section_lines))

    if not parts:
        return ""

    logger.info(
        "[KB.behavior] tenant=%s behavioral_sections=%d subtypes=%s",
        tenant_id, len(rows), sorted({r.kind for r in rows}),
    )
    _emit_kb_runtime_trace(
        tenant_id=tenant_id,
        channel="behavior",
        queried_rows=rows,
        included_rows=rows,
    )
    return "\n\n".join(parts)


def build_tenant_overlay_split(
    settings: Optional[Dict[str, Any]],
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> Dict[str, str]:
    """
    Split a tenant's ai_settings into the architectural buckets:

        {
          "identity": "<اسم/دور المساعد>",          # neutral, free-form
          "style":    "<style block contents>",      # → consumed by High-Priority layer
          "policy":   "<policy block contents>",     # → consumed by High-Priority layer
          "facts":    "<facts-only KB body>",        # → consumed by KB block
          "behavior": "<merchant behavioral overlay>",  # → KB-2: merged into High-Priority layer
        }

    Keys are always present (empty string when no data). The
    ``behavior`` bucket (KB-2, May 2026 #23) carries the rendered
    behavioral KB sections (group 7) and is routed by the prompt
    builder to ``build_high_priority_block`` — never to the facts
    block. This is the architectural guarantee that "لا تقل حبيبي"
    cannot leak into commerce knowledge retrieval.

    The legacy single-string overlay (`build_tenant_prompt_overlay`)
    concatenates the buckets as separate labelled sections. ``behavior``
    stays outside the facts bucket, but legacy callers still receive it;
    otherwise structured contact/escalation rows disappear from the
    runtime prompt after migration from the old single-text KB.
    """
    buckets = {
        "identity": "", "style": "", "policy": "", "facts": "",
        "behavior": "",
    }
    if not settings:
        return buckets

    # ── identity (ARCH-KB-001: presence name only — no role essay) ───────
    name = str(settings.get("assistant_name") or "").strip()
    if name:
        buckets["identity"] = f"هوية المساعد:\n- اسمك: {name}"

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
    behavioral_overlay = ""
    if db is not None and tenant_id is not None:
        try:
            structured_facts = build_structured_facts_block(db, int(tenant_id))
        except Exception as exc:  # noqa: BLE001 — never break overlay build
            logger.warning(
                "[KB.facts] structured build failed for tenant=%s: %s",
                tenant_id, exc,
            )
        try:
            behavioral_overlay = build_behavioral_overlay_block(db, int(tenant_id))
        except Exception as exc:  # noqa: BLE001 — never break overlay build
            logger.warning(
                "[KB.behavior] build failed for tenant=%s: %s",
                tenant_id, exc,
            )
    if behavioral_overlay:
        buckets["behavior"] = behavioral_overlay
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
    if buckets["behavior"]:
        sections.append(
            "قواعد سلوك ومساندة من قاعدة المعرفة:\n" + buckets["behavior"]
        )
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
