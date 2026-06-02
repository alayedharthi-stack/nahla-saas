"""
core/ai_libraries.py
─────────────────────
Read-side helpers for the merchant-curated *Manual Coupons* and *AI Media
Library* tables (collectively referred to internally as "AI Assets").

These helpers are used in three places:

* :func:`core.store_knowledge.build_merchant_context` injects the active
  rows into the brain prompt so the LLM knows which coupon codes it may
  cite verbatim and which media files it may attach.

* :func:`format_libraries_for_prompt` produces a human-readable Arabic
  block that goes into the prompt **alongside** the structured JSON, so
  GPT picks the right asset by *meaning* (title / description /
  ``usage_context`` / tags) instead of guessing from a numeric id.

* :func:`extract_media_markers` parses the LLM reply for ``[MEDIA:<id>]``
  tokens, strips them out of the text, and returns the matching media
  rows so the WhatsApp webhook can dispatch image / video / document
  messages alongside the cleaned text reply.

* :func:`validate_media_for_send` re-checks every attachment one final
  time before we hit the WhatsApp Cloud API. The LLM cannot be trusted
  to remember tenant scope or the active flag across turns, and a stale
  id from a deleted/disabled row would otherwise leak through.

Everything here is intentionally *read-only* and tenant-scoped; mutation
goes through ``routers/intelligence_libraries.py``.

Independence (architectural contract — DO NOT regress):

  These helpers are the read-side of *store intelligence*. They MUST
  NEVER gate on or import from the automation/autopilot stack:

    * No reads of ``TenantSettings.extra_metadata['autopilot']``.
    * No imports of ``core.automation_engine`` (one-way arrow only —
      ``store_knowledge`` may consume the autopilot flag for prompt
      priority guidance, but ``ai_libraries`` itself stays clean).
    * No checks against the automatic coupon engine or Salla sync.

  The result: a merchant who sells manually over WhatsApp (no Salla,
  no autopilot, no scheduler) still gets full benefit from manual
  coupons and the AI media library. Autopilot ON only changes
  *which* coupon source GPT prefers, never *which* sources are
  visible. See :func:`core.store_knowledge.build_merchant_context`
  for how the flag is surfaced to the prompt without affecting
  library visibility.

  Tests in ``tests/test_ai_assets_independence.py`` lock this contract.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla-backend")


# ── Prompt-size budget ───────────────────────────────────────────────────────
#
# The LLM context window is a shared resource. Keeping these ceilings low
# means a merchant who has uploaded 200 coupons can still run a cheap turn,
# and the brain is forced to pick the most-relevant asset rather than
# swimming through irrelevant noise.
_MAX_COUPONS_IN_CONTEXT = 10
_MAX_MEDIA_IN_CONTEXT = 15

# Length caps for the human-readable prompt block. Long descriptions get
# truncated so a merchant who pastes a novel into ``description`` doesn't
# blow the prompt budget.
_PROMPT_DESC_MAXLEN = 220
_PROMPT_USAGE_MAXLEN = 220


# ── Marker regex ─────────────────────────────────────────────────────────────
#
# Marker shape: ``[MEDIA:<id>]`` — id is the integer DB primary key. We
# accept optional whitespace and an optional title hint after a pipe
# (``[MEDIA:42|product photo]``) so the LLM can be explicit; the title
# hint is ignored by the parser, only the numeric id is honoured.
_MEDIA_MARKER_RE = re.compile(r"\[MEDIA:\s*(\d+)(?:\s*\|[^\]]*)?\]", re.IGNORECASE)

# Catch-all scrubber for ANY internal-shaped marker that survives past the
# media extraction step. The merchant reported customers literally seeing
# tokens like ``[TRANSFER]`` or ``[TEMPLATE:contact_owner]`` in WhatsApp
# — those are GPT hallucinations (it sees ``[MEDIA:<id>]`` in the prompt
# and extrapolates similar-looking placeholders). The webhook calls
# :func:`scrub_internal_markers` AFTER :func:`extract_media_markers`, so
# legitimate ``[MEDIA:N]`` tokens have already been consumed and only
# leaks remain.
#
# Pattern: ``[`` + one or more uppercase letters / digits / underscore /
# space (so ``[TRANSFER]``, ``[CTA URL]``, ``[ESCALATE_HUMAN]`` all
# match), optionally followed by ``:...`` (so ``[TEMPLATE:contact_owner]``
# and ``[CTA_URL:title=...|url=...]`` also match), then a closing ``]``.
# We deliberately require the leading word to be ALL-CAPS so we don't
# accidentally clobber Arabic content the merchant might have wrapped
# in brackets like "[ملاحظة]".
_INTERNAL_MARKER_RE = re.compile(r"\[[A-Z][A-Z0-9_ ]{0,40}(?::[^\]\n]{0,300})?\]")


# ── WhatsApp Cloud API limits per media type ─────────────────────────────────
#
# These match Meta's public documentation. We enforce the type-specific
# size cap inside :func:`validate_media_for_send` so the brain doesn't
# attempt to send a 50 MB image and have WhatsApp 4xx the message.
_WA_MAX_BYTES = {
    "image":    5 * 1024 * 1024,
    "video":   16 * 1024 * 1024,
    "audio":   16 * 1024 * 1024,
    "document": 100 * 1024 * 1024,
    "pdf":      100 * 1024 * 1024,   # pdf rides on the document channel
}

# Outer "type" key for WhatsApp's interactive media payloads. Mirrors
# :data:`routers.whatsapp_webhook._WA_MEDIA_OUTER_TYPE`. Kept here so
# validation can reject unknown ``media_type`` values without a forward
# import on the heavy webhook module.
_SUPPORTED_MEDIA_TYPES = frozenset({"image", "video", "audio", "document", "pdf"})


# ── Activity window ──────────────────────────────────────────────────────────


def _is_currently_active(
    is_active: bool,
    starts_at: Optional[datetime],
    expires_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> bool:
    if not is_active:
        return False
    moment = now or datetime.now(timezone.utc)
    if starts_at and starts_at > moment:
        return False
    if expires_at and expires_at < moment:
        return False
    return True


# ── Relevance scoring ────────────────────────────────────────────────────────


# ── Arabic-aware normalisation ──────────────────────────────────────────────
#
# Arabic search is famously brittle without normalisation. The merchant
# reported a real customer asking "ارسل حساب الراجحي" while the matching
# media item carried tags like "تحويل / بنك / آيبان" — none of which
# share a single character with "الراجحي" at the byte level. The
# normalisation below collapses common variant forms so a tag of
# "آيبان" still matches a query of "ايبان"; a query of "الراجحي" still
# matches a tag/title containing "راجحي"; etc.
_ARABIC_NORMALISE = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",  # alif variants
    "ى": "ي", "ئ": "ي",                       # alif maqsura / hamza-on-ya
    "ة": "ه",                                 # ta marbuta → ha
    "ؤ": "و",
    # Stripped diacritics (handled via regex below for ranges).
})

_ARABIC_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0670\u0640]")  # tashkeel + tatweel
# Definite article + common single-letter prefixes that should be stripped
# token-by-token so "الراجحي" / "للراجحي" / "بالراجحي" all collapse to
# "راجحي" for matching purposes.
_AR_PREFIXES = ("ال", "لل", "بال", "كال", "فال", "وال", "ول", "فل", "بل", "كل", "ل", "ب", "ك", "ف", "و")


def _normalise_arabic_token(tok: str) -> str:
    s = tok.translate(_ARABIC_NORMALISE)
    s = _ARABIC_DIACRITICS_RE.sub("", s)
    return s


def _strip_definite_prefix(tok: str) -> str:
    """Remove a leading "ال" or related preposition glued onto the word."""
    for pref in _AR_PREFIXES:
        if tok.startswith(pref) and len(tok) > len(pref) + 1:
            return tok[len(pref):]
    return tok


def _normalize_for_match(text: str) -> str:
    """Arabic-aware normalisation for cheap substring/token scoring.

    Steps: lowercase → strip tashkeel & tatweel → fold alif/ya/ta-marbuta
    variants → collapse whitespace. Definite-article stripping happens
    later (token-by-token) inside ``_tokenize_for_match`` so the raw
    string still matches if the merchant tagged it with the article on.
    """
    if not text:
        return ""
    s = text.strip().lower()
    s = _normalise_arabic_token(s)
    return re.sub(r"\s+", " ", s)


_NON_WORD_RE = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)


def _tokenize_for_match(text: str) -> List[str]:
    """Split into normalised tokens with Arabic prefixes removed.

    Returns BOTH the prefix-free form and (when different) the original
    normalised token so a tag like "بنك" matches a query containing
    "البنك" and vice versa.
    """
    norm = _normalize_for_match(text)
    if not norm:
        return []
    out: List[str] = []
    for raw in _NON_WORD_RE.split(norm):
        if not raw or len(raw) < 2:
            continue
        out.append(raw)
        stripped = _strip_definite_prefix(raw)
        if stripped != raw and len(stripped) >= 2:
            out.append(stripped)
    return out


# ── Synonym groups ──────────────────────────────────────────────────────────
#
# Each entry below is a CLUSTER of words that should pull each other into
# scoring matches. When ANY token in the customer's query matches a
# member of a cluster, every other member of the same cluster is treated
# as if it appeared in the query too. Crucially this is one-way (query
# → match) so a media tag of "تحويل" lights up for a query of "راجحي",
# not the other way around (which would over-fire).
_SYNONYM_CLUSTERS: List[Tuple[str, ...]] = [
    # Bank / transfer / IBAN family — the cluster the merchant flagged.
    (
        "راجحي", "الراجحي", "اهلي", "الاهلي", "rajhi", "alrajhi",
        "بنك", "البنك", "بنكي", "البنكي", "bank",
        "حساب", "الحساب", "account",
        "تحويل", "التحويل", "تحويله", "transfer",
        "ايبان", "الايبان", "iban",
        "باركود", "الباركود", "barcode",
        "qr", "كيوار", "كيوآر",
        "دفع", "الدفع", "payment",
        "ايداع", "الايداع", "deposit",
    ),
    # Shipping / tracking — for matching "اين شحنتي" to a media tag of "تتبع".
    (
        "شحن", "الشحن", "شحنه", "شحنة", "shipping",
        "تتبع", "التتبع", "tracking",
        "طلبيه", "طلبية", "طلب", "الطلب", "order",
    ),
]


def _expand_with_synonyms(tokens: List[str]) -> set:
    """Return the union of ``tokens`` with their synonym clusters, all
    normalised (so cluster lookups work regardless of definite article
    or alif variants)."""
    norm_tokens = {t for t in tokens if t}
    if not norm_tokens:
        return set()

    expanded = set(norm_tokens)
    for cluster in _SYNONYM_CLUSTERS:
        cluster_norm = {_strip_definite_prefix(_normalise_arabic_token(w)) for w in cluster}
        # If any query token is in this cluster (or matches one of its
        # members after definite-article stripping), pull the whole
        # cluster into the expansion set.
        hit = False
        for tok in norm_tokens:
            stripped = _strip_definite_prefix(tok)
            if stripped in cluster_norm or tok in cluster_norm:
                hit = True
                break
        if hit:
            expanded |= cluster_norm
    return expanded


def _relevance_score(item: Dict[str, Any], query: str) -> float:
    """Arabic-aware relevance score against the customer's last message.

    Uses three signals, in priority order:

      * Tag overlap (1.5pt per tag hit) — strongest, because tags are
        merchant-curated and represent intent ("تحويل", "بنك", …).
      * Title overlap (1.0pt per token hit, capped at 2.5).
      * Usage-context overlap (0.2pt per long-token hit).

    Synonym expansion (see ``_SYNONYM_CLUSTERS``) means a query of
    "ارسل حساب الراجحي" pulls in "بنك / تحويل / آيبان / باركود / QR"
    so a media item tagged with any of those rises to the top.

    Returns a score in roughly [0.0, 5.0]; absolute magnitude doesn't
    matter — only the ordering does."""
    if not query:
        return 0.0

    q_tokens = _tokenize_for_match(query)
    if not q_tokens:
        return 0.0
    q_set = _expand_with_synonyms(q_tokens)
    if not q_set:
        return 0.0

    score = 0.0

    # Tag overlap — strongest signal; merchant explicitly labelled this.
    for tag in (item.get("tags") or []):
        for tok in _tokenize_for_match(str(tag)):
            stripped = _strip_definite_prefix(tok)
            if stripped in q_set or tok in q_set:
                score += 1.5
                break  # one hit per tag is enough

    # Title overlap — capped so a multi-word title doesn't dominate.
    title_score = 0.0
    for tok in _tokenize_for_match(str(item.get("title") or "")):
        if len(tok) < 3:
            continue
        stripped = _strip_definite_prefix(tok)
        if stripped in q_set or tok in q_set:
            title_score += 1.0
    score += min(title_score, 2.5)

    # Usage context — softest signal, mostly a tiebreaker.
    for tok in _tokenize_for_match(str(item.get("usage_context") or "")):
        if len(tok) < 4:
            continue
        stripped = _strip_definite_prefix(tok)
        if stripped in q_set or tok in q_set:
            score += 0.2
            if score >= 5.0:
                return 5.0

    return score


def _sort_with_relevance(
    items: List[Dict[str, Any]],
    query: Optional[str],
    *,
    cap: int,
) -> List[Dict[str, Any]]:
    """Stable sort by (relevance DESC, priority ASC, id ASC) and cap."""
    if not items:
        return []
    if not query:
        return items[:cap]
    scored = [
        (_relevance_score(item, query), int(item.get("priority", 100)), int(item.get("id", 0)), item)
        for item in items
    ]
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [t[3] for t in scored[:cap]]


# ── List helpers (consumed by build_merchant_context) ────────────────────────


def list_active_manual_coupons(
    db: Session,
    tenant_id: int,
    *,
    relevance_query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return active, in-window manual coupons for the brain to cite.

    Order:
      * relevance score against ``relevance_query`` (the customer's last
        message), then
      * ``priority ASC`` (lower = more important), then
      * ``id ASC`` for determinism.

    The brain MUST NOT invent coupons, so anything that fails the
    is-active / expiry check is filtered out here, not in the prompt.
    """
    from models import ManualCoupon  # noqa: PLC0415 — avoid circular import

    rows = (
        db.query(ManualCoupon)
        .filter(ManualCoupon.tenant_id == tenant_id, ManualCoupon.is_active.is_(True))
        .order_by(ManualCoupon.priority.asc(), ManualCoupon.id.asc())
        .limit(_MAX_COUPONS_IN_CONTEXT * 4)  # over-fetch then filter window/relevance
        .all()
    )
    now = datetime.now(timezone.utc)
    pre: List[Dict[str, Any]] = []
    for r in rows:
        if not _is_currently_active(bool(r.is_active), r.starts_at, r.expires_at, now=now):
            continue
        pre.append({
            "id": int(r.id),
            "code": r.code,
            "title": r.title or "",
            "description": r.description or "",
            "discount_text": r.discount_text or "",
            "usage_context": r.usage_context or "",
            "priority": int(r.priority or 0),
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        })
    return _sort_with_relevance(pre, relevance_query, cap=_MAX_COUPONS_IN_CONTEXT)


def list_active_ai_media(
    db: Session,
    tenant_id: int,
    *,
    relevance_query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return active media-library items the brain may attach to a reply.

    Same ordering rules as :func:`list_active_manual_coupons`.
    """
    from models import AIMediaItem  # noqa: PLC0415 — avoid circular import

    rows = (
        db.query(AIMediaItem)
        .filter(AIMediaItem.tenant_id == tenant_id, AIMediaItem.is_active.is_(True))
        .order_by(AIMediaItem.priority.asc(), AIMediaItem.id.asc())
        .limit(_MAX_MEDIA_IN_CONTEXT * 4)
        .all()
    )
    pre: List[Dict[str, Any]] = []
    for r in rows:
        pre.append({
            "id": int(r.id),
            "title": r.title,
            "description": r.description or "",
            "media_type": r.media_type,
            "usage_context": r.usage_context or "",
            "tags": list(r.tags or []),
            "priority": int(r.priority or 0),
        })
    return _sort_with_relevance(pre, relevance_query, cap=_MAX_MEDIA_IN_CONTEXT)


# ── Prompt formatting ────────────────────────────────────────────────────────
#
# The LLM sees ``merchant_context`` as a raw JSON dump in the prompt JSON.
# That works, but ids buried inside arrays don't always survive the
# model's attention. The functions below emit a tight Arabic block that
# the prompt builder appends to the system prompt — the same data, but
# in a shape GPT actually reads.


def _truncate(text: str, limit: int) -> str:
    s = (text or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(1, limit - 1)].rstrip() + "…"


def format_manual_coupons_for_prompt(coupons: List[Dict[str, Any]]) -> str:
    """Render the active manual-coupon list as an Arabic prompt section.

    Empty list → empty string (caller skips the section)."""
    if not coupons:
        return ""
    lines = ["## الكوبونات اليدوية المتاحة (استخدمي الكود حرفياً، لا تخترعي):"]
    for c in coupons:
        head = f"- code={c.get('code')}"
        title = (c.get("title") or "").strip()
        if title:
            head += f' | title="{title}"'
        if c.get("discount_text"):
            head += f' | discount="{_truncate(str(c["discount_text"]), 80)}"'
        if c.get("expires_at"):
            head += f" | expires_at={c['expires_at']}"
        lines.append(head)
        usage = _truncate(str(c.get("usage_context") or ""), _PROMPT_USAGE_MAXLEN)
        if usage:
            lines.append(f"  متى تستخدمينه: {usage}")
        desc = _truncate(str(c.get("description") or ""), _PROMPT_DESC_MAXLEN)
        if desc:
            lines.append(f"  وصف: {desc}")
    return "\n".join(lines)


def format_ai_media_for_prompt(media: List[Dict[str, Any]]) -> str:
    """Render the active media library as an Arabic prompt section.

    Crucial: each item is shown with id + title + type + tags + usage,
    so the LLM can pick the right one by meaning. The id is what gets
    cited via ``[MEDIA:<id>]``, but the LLM should NEVER paste the
    file URL — those aren't surfaced here on purpose.
    """
    if not media:
        return ""
    lines = [
        "## مكتبة وسائط الذكاء (لإرفاقها استخدمي [MEDIA:<id>] فقط — "
        "لا تلصقي الرابط، لا تذكري المسار، لا تخمّني id):",
    ]
    for m in media:
        head = (
            f"- MEDIA_ID={m.get('id')} | type={m.get('media_type')}"
            f' | title="{_truncate(str(m.get("title") or ""), 80)}"'
        )
        tags = [str(t) for t in (m.get("tags") or []) if str(t).strip()]
        if tags:
            head += f" | tags={tags}"
        lines.append(head)
        usage = _truncate(str(m.get("usage_context") or ""), _PROMPT_USAGE_MAXLEN)
        if usage:
            lines.append(f"  متى ترسلينه: {usage}")
        desc = _truncate(str(m.get("description") or ""), _PROMPT_DESC_MAXLEN)
        if desc:
            lines.append(f"  وصف: {desc}")
    return "\n".join(lines)


def format_libraries_for_prompt(merchant_context: Dict[str, Any]) -> str:
    """Combine both sections into one prompt block. Empty when neither
    library has any active rows so the prompt doesn't grow noise.
    """
    coupons = merchant_context.get("manual_coupons") or []
    media = merchant_context.get("ai_media_library") or []
    parts: List[str] = []
    cb = format_manual_coupons_for_prompt(coupons)
    if cb:
        parts.append(cb)
    mb = format_ai_media_for_prompt(media)
    if mb:
        parts.append(mb)
    return "\n\n".join(parts)


# ──────────────────────────────────────────────────────────────────
# Resolver-aware prompt overlay (Product + Media key markers)
# ──────────────────────────────────────────────────────────────────


_MARKER_PROTOCOL_PREAMBLE = (
    "## بروتوكول الوسائط والمنتجات (إلزامي)\n"
    "أنت ترد على عملاء واتساب. عندما يطلب العميل منتجًا، أو باركود، "
    "أو صورة، أو فيديو، أو رمز دفع — يجب أن تُصدر **رمزًا** "
    "(marker) في ردك وسيقوم النظام تلقائيًا باستبدال الرمز بالملف "
    "أو المنتج الحقيقي قبل الإرسال للعميل.\n"
    "\n"
    "## قواعد إلزامية لا يجوز كسرها\n"
    "1. عند طلب منتج → يجب وضع [PRODUCT:...] **قبل** أي شرح نصي.\n"
    "2. عند طلب باركود/QR/صورة دفع → يجب وضع [MEDIA_KEY:...] "
    "**قبل** أي شرح نصي.\n"
    "3. عند مشاركة رقم موظف/مالك/إدارة للتواصل → يجب وضع "
    "[CALL:<phone>|<اسم>] **في نهاية الرد** (النظام يحوّله لكرت "
    "اتصال احترافي).\n"
    "4. ممنوع قول \"هذا الباركود\" أو \"هذي صورة المنتج\" أو "
    "\"تفضل الصورة\" بدون إصدار الـ marker — لأن النظام لن يرسل "
    "أي شيء إذا لم يُصدر الـ marker، والعميل سيظن أنك كاذبة.\n"
    "5. ممنوع كتابة رابط (https://...) أو صورة كنص — النظام "
    "يضع الروابط في زر CTA تلقائيًا بعد الـ marker.\n"
    "6. ممنوع كتابة سعر بنفسك مع [PRODUCT:...] — النظام يضيفه.\n"
    "\n"
    "## (1) منتجات الكتالوج — [PRODUCT:...]\n"
    "الصيغة: [PRODUCT:اسم المنتج كما في الكتالوج] (1 إلى 3 رموز "
    "كحد أقصى لكل رد).\n"
    "\n"
    "أمثلة (Few-shot):\n"
    "- العميل: «أبي أشوف عسل الطلح»\n"
    "  الرد: [PRODUCT:عسل الطلح البلدي]\n"
    "- العميل: «أرسل لي السمر»\n"
    "  الرد: [PRODUCT:عسل سمر الحجاز]\n"
    "- العميل: «أبي عسل مناسب للأطفال»\n"
    "  الرد: [PRODUCT:عسل الضهيان الجبلي]\n"
    "- العميل: «أبي خلطة منتجات النحل»\n"
    "  الرد: [PRODUCT:خلطة منتجات النحل]\n"
    "- العميل: «بكم عسل الضهيان؟»\n"
    "  الرد: [PRODUCT:عسل الضهيان الجبلي]\n"
    "\n"
    "بعد الـ marker تقدرين تكتبين سطر أو اثنين دافئين + سؤال متابعة "
    "(لا تشرحين السعر — النظام يضيفه تلقائيًا).\n"
    "\n"
    "## (2) الوسائط المعتمدة — [MEDIA_KEY:...]\n"
    "الصيغة: [MEDIA_KEY:<مفتاح_من_قائمة_الوسائط_أدناه>]\n"
    "استخدمي فقط المفاتيح المذكورة في قسم \"أدوات الوسائط المتوفرة\" "
    "أدناه. لا تخترعي مفتاحًا.\n"
    "\n"
    "أمثلة (Few-shot):\n"
    "- العميل: «أرسل باركود الراجحي» / «الراجحي» / «ابو دانة»\n"
    "  الرد: [MEDIA_KEY:payment_rajhi_barcode]\n"
    "- العميل: «أرسل الأهلي» / «باركود SNB»\n"
    "  الرد: [MEDIA_KEY:payment_alahli_barcode]\n"
    "- العميل: «برق» / «أرسل برق»\n"
    "  الرد: [MEDIA_KEY:payment_barq_barcode]\n"
    "- العميل: «STC Pay» / «اس تي سي»\n"
    "  الرد: [MEDIA_KEY:payment_stcpay_qr]\n"
    "- العميل: «Mobily Pay» / «موبايلي باي»\n"
    "  الرد: [MEDIA_KEY:payment_mobilypay_qr]\n"
    "- العميل: «وين موقعكم؟»\n"
    "  الرد: [MEDIA_KEY:store_location_image]\n"
    "- العميل: «كيف أستخدم العسل؟»\n"
    "  الرد: [MEDIA_KEY:product_usage_video]\n"
    "\n"
    "إذا قال العميل «الباركود» بدون تحديد البنك:\n"
    "  - استخدمي المفتاح الوحيد المتوفر في قائمة \"أدوات الوسائط\" "
    "أدناه. إذا توفّر أكثر من بنك، اسألي العميل أي بنك يفضّل.\n"
    "\n"
    "## (3) أرقام الجوال — قاعدتان مختلفتان\n"
    "هناك نوعان من الأرقام، ولكلٍّ منهما سلوك مختلف:\n"
    "\n"
    "### أ) رقم تحويل / دفع (بنك، STC Pay، Mobily Pay، برق)\n"
    "هذا الرقم يُكتب **كنص واضح فقط** (بدون marker) ليتمكّن العميل "
    "من نسخه ولصقه في تطبيق البنك.\n"
    "\n"
    "أمثلة:\n"
    "- العميل: «أرسل رقم الراجحي»\n"
    "  الرد:\n"
    "  [MEDIA_KEY:payment_rajhi_barcode]\n"
    "  الراجحي 🌷\n"
    "  0555906901\n"
    "  اسم: نحلة الذهبية\n"
    "\n"
    "- العميل: «STC Pay»\n"
    "  الرد:\n"
    "  [MEDIA_KEY:payment_stcpay_qr]\n"
    "  STC Pay: 0555906901\n"
    "\n"
    "❌ لا تستخدمي [CALL:...] لأرقام التحويل — العميل يحتاج نسخ الرقم "
    "وليس فتح محادثة.\n"
    "\n"
    "### ب) رقم موظف / مالك / إدارة للتواصل\n"
    "هذا الرقم يُحوَّل لكرت اتصال احترافي عبر:\n"
    "  [CALL:<رقم>|<اسم أو وصف الموظف>]\n"
    "النظام يرسله ككرت اتصال WhatsApp (اضغط للاتصال أو محادثة).\n"
    "\n"
    "أمثلة (Few-shot):\n"
    "- العميل: «أبي أكلم أمين»\n"
    "  الرد:\n"
    "  أكيد، تقدري تتواصلي مع أمين مباشرة 🌷\n"
    "  [CALL:0541690226|أمين]\n"
    "\n"
    "- العميل: «أبي رقم المالك»\n"
    "  الرد:\n"
    "  حياك الله، هذا رقم الإدارة:\n"
    "  [CALL:0555906901|الإدارة]\n"
    "\n"
    "- العميل: «أبي بائع المعرض»\n"
    "  الرد:\n"
    "  بائع المعرض جاهز لخدمتك 👍\n"
    "  [CALL:0541690226|بائع المعرض]\n"
    "\n"
    "قاعدة التمييز السريعة:\n"
    "  - رقم بجانبه اسم بنك / STC Pay / Mobily / تحويل → **نص فقط**.\n"
    "  - رقم بجانبه اسم شخص / موظف / مالك / إدارة / بائع → **[CALL:...]**.\n"
    "\n"
    "## السلوك بعد الـ marker\n"
    "النظام يرسل الصورة/المنتج أولًا، ثم يلحقها بنصك. لذلك بعد "
    "الـ marker اكتبي:\n"
    "  - سطر إلى ثلاثة أسطر دافئة فقط (مرحب / توضيح بسيط / سؤال "
    "متابعة).\n"
    "  - لا تكرّري ما سيظهر في كرت المنتج (الاسم/السعر/الرابط).\n"
    "  - لا تقولي \"تفضل الصورة\" — الصورة تظهر تلقائيًا.\n"
    "\n"
    "## مثال كامل — منتج\n"
    "العميل: «أبي عسل الطلح»\n"
    "الرد المثالي:\n"
    "[PRODUCT:عسل الطلح البلدي]\n"
    "حياك الله 🌷\n"
    "الطلح من الأنواع المميّزة عندنا. تبيه بحجم محدد؟\n"
    "\n"
    "## مثال كامل — باركود\n"
    "العميل: «أرسل باركود الراجحي»\n"
    "الرد المثالي:\n"
    "[MEDIA_KEY:payment_rajhi_barcode]\n"
    "الراجحي 🌷\n"
    "0555906901 — اسم: نحلة\n"
    "بعد التحويل أرسلي صورة الإيصال 👍\n"
    "\n"
    "## القاعدة الذهبية\n"
    "- منتج → [PRODUCT:...] (ليس قاعدة المعرفة وحدها).\n"
    "- وسيط (باركود/فيديو/شهادة/موقع/QR) → [MEDIA_KEY:...].\n"
    "- رقم موظف للتواصل → [CALL:<رقم>|<اسم>].\n"
    "- رقم تحويل بنكي → نص قابل للنسخ فقط.\n"
    "- لا تكتبي رابطًا (https://...) كنص — استخدمي الـ marker فقط.\n"
    "- الـ marker (PRODUCT / MEDIA_KEY) في أول الرد، قبل أي نص.\n"
    "- الـ marker (CALL) في آخر الرد بعد سطر التواصل.\n"
)


def format_resolver_overlay_for_prompt(
    *,
    available_media_keys_block: str = "",
    catalog_has_products: bool = False,
) -> str:
    """Build the resolver-marker protocol overlay.

    Inserted into the system prompt by ``whatsapp_webhook.py``
    AFTER the existing ``format_libraries_for_prompt`` block.

    ``available_media_keys_block``:
        the output of
        ``services.media_key_registry.format_keys_for_prompt``
        for THIS tenant. Empty string when no registry-keyed
        assets exist (we still tell Claude about ``[PRODUCT:...]``
        in that case).

    ``catalog_has_products``:
        false when the tenant has zero synced products — we omit
        the product-marker section so Claude doesn't promise an
        attachment it has no chance of resolving.
    """
    if not catalog_has_products and not available_media_keys_block:
        # Tenant has neither catalog nor keyed media. Nothing for
        # the resolver to do — return empty so the prompt stays lean.
        return ""

    parts: List[str] = [_MARKER_PROTOCOL_PREAMBLE]

    if available_media_keys_block:
        parts.append(
            "\nأدوات الوسائط المتوفرة في هذا المتجر:\n"
            + available_media_keys_block
        )
    if not catalog_has_products:
        # Strip the product part: replace the [PRODUCT:...] section
        # with a one-line disabler so Claude doesn't emit unresolvable
        # product markers for catalog-less tenants. We do this by
        # inserting an explicit override after the preamble.
        parts.append(
            "\nملاحظة: هذا المتجر لا يوجد لديه كتالوج منتجات متزامن، "
            "لذا لا تستخدم [PRODUCT:...] في هذه المحادثة."
        )

    return "\n".join(parts)


# ── Marker extraction (called by the WhatsApp webhook) ───────────────────────


def extract_media_markers(
    db: Session,
    tenant_id: int,
    reply_text: str,
    *,
    max_attachments: int = 2,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Strip ``[MEDIA:<id>]`` tokens from ``reply_text`` and resolve them.

    Returns ``(cleaned_text, attachments)`` where each attachment is a
    dict carrying enough metadata for the WhatsApp webhook to dispatch
    the file. Attachments include the fields needed by
    :func:`validate_media_for_send` so the caller can re-check each one
    immediately before hitting the WhatsApp API.

    Behaviour notes:

    * Attachments are deduped (a token referenced twice ships the file
      once) and capped at ``max_attachments`` so the brain can't accidentally
      flood the customer with media.
    * Media rows whose ``is_active`` flag is **false** OR which belong to
      a different tenant are silently dropped — the dashboard could have
      disabled them mid-conversation, and we never trust an id from the
      LLM blindly.
    * The cleaned text has surrounding whitespace and stranded blank
      lines collapsed so the customer doesn't see odd gaps where the
      marker used to be.
    """
    text = reply_text or ""
    if not text or "[MEDIA:" not in text.upper():
        return text, []

    from models import AIMediaItem  # noqa: PLC0415 — avoid circular import

    matches = list(_MEDIA_MARKER_RE.finditer(text))
    if not matches:
        return text, []

    seen_ids: List[int] = []
    for m in matches:
        try:
            mid = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if mid not in seen_ids:
            seen_ids.append(mid)
        if len(seen_ids) >= max_attachments:
            break

    attachments: List[Dict[str, Any]] = []
    if seen_ids:
        rows = (
            db.query(AIMediaItem)
            .filter(
                AIMediaItem.tenant_id == tenant_id,
                AIMediaItem.id.in_(seen_ids),
                AIMediaItem.is_active.is_(True),
            )
            .all()
        )
        # Preserve the order in which the LLM cited them.
        rows_by_id = {int(r.id): r for r in rows}
        for mid in seen_ids:
            r = rows_by_id.get(mid)
            if r is None:
                logger.info(
                    "[AIMedia.attach] dropped marker id=%s tenant=%s reason=not_found_or_disabled",
                    mid, tenant_id,
                )
                continue
            attachments.append({
                "id": int(r.id),
                "tenant_id": int(r.tenant_id),
                "title": r.title,
                "media_key": getattr(r, "media_key", None),
                "media_type": r.media_type,
                "file_url": r.file_url,
                "mime_type": r.mime_type,
                "storage_kind": r.storage_kind,
                "storage_path": r.storage_path,
                "file_size_bytes": int(r.file_size_bytes) if r.file_size_bytes is not None else None,
            })

    cleaned = _MEDIA_MARKER_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, attachments


def scrub_internal_markers(text: str) -> str:
    """Remove any internal ``[MARKER]`` / ``[MARKER:payload]`` token that
    survived past :func:`extract_media_markers`.

    Called by the WhatsApp webhook as the LAST step before sending, so by
    the time we run, every legitimate ``[MEDIA:N]`` marker has already
    been consumed. Anything still bracketed is a leak — most often a GPT
    hallucination of a placeholder it saw in the prompt or in earlier
    turns (the merchant reported customers receiving ``[TRANSFER]``
    literally; this scrubber is what makes that impossible).

    Returns the cleaned text with whitespace tidied. Empty / non-string
    input is returned unchanged.
    """
    if not isinstance(text, str) or not text:
        return text or ""
    cleaned = _INTERNAL_MARKER_RE.sub("", text)
    if cleaned == text:
        return text  # nothing to do — preserve original whitespace
    # Tidy stranded whitespace where the marker used to sit.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


# ── Payment-asset hard override ─────────────────────────────────────────────
#
# When the customer asks for bank/IBAN/transfer/QR info AND a relevant
# media item is active for this tenant, we attach it unconditionally —
# even if GPT's reply didn't include a ``[MEDIA:<id>]`` marker. This
# closes the failure mode reported by the merchant where GPT replied
# "ما عندي بيانات الحساب البنكي" while a "باركود التحويل البنكي
# الراجحي" item was sitting active in the library: GPT can't be trusted
# to always pick the asset, so the webhook double-checks.
_PAYMENT_QUERY_RE = re.compile(
    r"("
    # Banks / accounts
    r"الراجحي|راجحي|الأهلي|اهلي|الرياض|"
    r"حساب\s*(الـ?بنك|بنكي?|الراجحي|الأهلي)|رقم\s*الحساب|"
    r"ارسل(?:وا)?\s*(?:لي\s+)?(?:ال)?حساب|"
    # IBAN
    r"آيبان|الآيبان|ايبان|الايبان|iban|"
    # Transfer / deposit
    r"تحويل\s*بنك|التحويل\s*البنكي|بيانات\s*التحويل|بيانات\s*الدفع|إيداع|"
    # Generic transfer / pay intent (May 2026 #29). Production case:
    # the customer asks "كيف أحول لكم؟" / "كيف أدفع لكم؟" — no bank
    # name, no "باركود" noun, no "بنكي" qualifier. The old regex
    # missed all of them, so the legacy payment-asset hard-override
    # never ran and the AI replied with prose steps instead of the
    # barcode image. The modern media_key registry has the same
    # phrasings as triggers (see ``_GENERIC_PAYMENT_BARCODE_TRIGGERS``)
    # — having both layers catch the intent gives us defense-in-depth.
    # We require ``لكم``/``لكم؟``/``إليكم`` after the verb so we
    # don't false-positive on "كيف أحول الكمبيوتر" / unrelated uses.
    r"كيف\s+[أا]?[ح]ول\s+(?:ل|إل)كم|"
    r"[أا]?بي\s+[أا]?[ح]ول\s+(?:ل|إل)كم|"
    r"ودي\s+[أا]?[ح]ول\s+(?:ل|إل)كم|"
    r"حابب\s+[أا]?[ح]ول\s+(?:ل|إل)كم|"
    r"كيف\s+[أا]?دفع(?:\s+(?:ل|إل)كم)?|"
    r"كيف\s+الدفع|كيف\s+التحويل|"
    r"طريقة\s+(?:الدفع|التحويل|السداد)|"
    r"وش\s+طريقة\s+(?:الدفع|التحويل|السداد)|"
    r"وش\s+طرق\s+الدفع|"
    r"كيف\s+[أا]?سدد|اسلوب\s+الدفع|"
    # Barcode / QR
    # Two-tier match: a bare ``باركود`` / ``الباركود`` on its own is enough
    # (May 2026 Tenant 33 incident: "ابي الباركود" was failing the regex
    # because it lacks a following bank/transfer noun, so the legacy
    # payment-asset hard-override never ran as a safety net for the
    # new media_key resolver). The bank-qualified form is still allowed
    # for explicit phrasings like "باركود التحويل" / "باركود الراجحي".
    r"باركود(\s*(التحويل|الدفع|البنك|الراجحي))?|"
    r"الباركود|qr\s*code|كيوار|كيو\s*ار|رمز\s*(الدفع|التحويل|السداد)|"
    # English mirrors for customers who switch to Latin script.
    r"\bbank\s*(account|details|transfer|info)\b|"
    r"\b(send|share|give|need|want)\s+(me|us|the)?\s*(your\s+)?bank\b|"
    r"\bpayment\s*(barcode|qr|details)\b|"
    r"\biban\b|"
    r"\bhow\s+(?:do\s+I|can\s+I|to)\s+(?:pay|transfer)\b"
    r")",
    re.IGNORECASE,
)


def is_payment_query(message: str) -> bool:
    """Cheap rule-based check used by the webhook to decide whether to
    consult :func:`find_best_payment_asset`. False positives are fine —
    the asset finder filters by tag relevance — but false negatives are
    not, since the whole point is to recover when GPT misses the asset."""
    if not message:
        return False
    raw = str(message)
    if _PAYMENT_QUERY_RE.search(raw):
        return True
    return bool(_PAYMENT_QUERY_RE.search(_normalize_for_match(raw)))


def find_best_payment_asset(
    db: Session,
    tenant_id: int,
    customer_message: str,
) -> Optional[Dict[str, Any]]:
    """Return the highest-relevance active media item that looks like
    payment / bank / barcode / QR, or ``None`` if no item passes the
    score threshold.

    Used as a HARD OVERRIDE: when the customer asks for bank info and
    GPT's reply doesn't include the corresponding ``[MEDIA:<id>]``, the
    webhook attaches the result of this function regardless. Returning
    ``None`` is normal — many merchants don't have a barcode uploaded —
    in which case the webhook just lets GPT's text reply go through.

    The returned dict has the same shape as :func:`extract_media_markers`
    output entries so it can be mixed into the attachments list with no
    extra adapters.
    """
    from models import AIMediaItem  # noqa: PLC0415 — avoid circular import

    if not is_payment_query(customer_message):
        return None

    rows = (
        db.query(AIMediaItem)
        .filter(
            AIMediaItem.tenant_id == int(tenant_id),
            AIMediaItem.is_active.is_(True),
        )
        .all()
    )
    if not rows:
        return None

    # Score each row against the customer message using the public
    # relevance helper (the synonym cluster handles "الراجحي" → "بنك /
    # تحويل / آيبان"). Rows whose description / title hints they are
    # payment-related but whose tags are sparse still score on title.
    scored: List[Tuple[float, AIMediaItem]] = []
    for r in rows:
        item_dict = {
            "title": r.title or "",
            "tags": list(r.tags or []),
            "usage_context": r.usage_context or "",
        }
        score = _relevance_score(item_dict, customer_message)
        # Boost if the title or tags explicitly mention a payment word —
        # the merchant who uploads "باركود التحويل البنكي" deserves to
        # have it picked even if their tags are minimal.
        haystack = (
            f"{r.title or ''} {' '.join(str(t) for t in (r.tags or []))} "
            f"{r.usage_context or ''}"
        ).lower()
        if _PAYMENT_QUERY_RE.search(haystack):
            score += 2.0
        if score > 0:
            scored.append((score, r))

    if not scored:
        return None

    scored.sort(key=lambda t: (t[0], int(t[1].priority or 0)), reverse=True)
    best_score, best_row = scored[0]
    # Threshold: require either a synonym-cluster hit (>=1.5) or a
    # payment-word boost (>=2.0). A bare 0.4 token-overlap match would
    # otherwise pull in unrelated images.
    if best_score < 1.5:
        return None

    logger.info(
        "[PAYMENT_INFO] tenant=%s asset_found=true asset_id=%s "
        "asset_score=%.2f title=%r",
        tenant_id, int(best_row.id), best_score, (best_row.title or "")[:80],
    )
    return {
        "id": int(best_row.id),
        "tenant_id": int(best_row.tenant_id),
        "title": best_row.title,
        "media_type": best_row.media_type,
        "file_url": best_row.file_url,
        "mime_type": best_row.mime_type,
        "storage_kind": best_row.storage_kind,
        "storage_path": best_row.storage_path,
        "file_size_bytes": (
            int(best_row.file_size_bytes)
            if best_row.file_size_bytes is not None
            else None
        ),
        "_relevance_score": best_score,
    }


# ── Pre-send validation ──────────────────────────────────────────────────────


_PROD_HTTPS_HOSTS = (
    "railway.app", "herokuapp.com", "vercel.app", "fly.dev", "render.com",
)


def _maybe_force_https(url: str) -> str:
    """Mirror of :func:`routers.intelligence_libraries._force_https_for_production`
    used at validate-time for rows already persisted with an http:// URL.
    Kept as a private helper here so :func:`validate_media_for_send`
    can run without pulling the router module into scope.
    """
    if not url or not url.lower().startswith("http://"):
        return url
    lower = url.lower()
    for host in _PROD_HTTPS_HOSTS:
        if host in lower:
            return "https://" + url[len("http://"):]
    if os.environ.get("NAHLA_FORCE_HTTPS_MEDIA", "").lower() in ("1", "true", "yes"):
        return "https://" + url[len("http://"):]
    return url


def _canonicalise_managed_host(url: str) -> str:
    """Rewrite a media URL whose host is a managed-platform preview
    domain (Railway/Heroku/Vercel/…) onto the canonical public base
    declared via ``NAHLA_PUBLIC_BASE_URL``.

    Why this is platform-level (not Tenant-33 specific):

    Every media row uploaded via ``POST /intelligence/ai-media``
    persists whatever absolute URL ``request.base_url`` returned at
    upload time. On Railway behind the platform proxy this is
    ``http://<service>-production.up.railway.app``. Two consequences
    that hit ALL tenants who uploaded before ``NAHLA_PUBLIC_BASE_URL``
    was set in the environment:

      1. Meta's WhatsApp Cloud API rejects/blocks plain-HTTP fetches
         even when the upstream cert is valid.
      2. The Railway preview hostname leaks infrastructure to
         end-customers and breaks if the service is renamed.

    :func:`_maybe_force_https` already addresses (1). This helper
    addresses (2): when ``NAHLA_PUBLIC_BASE_URL`` is configured, we
    swap the *scheme + host[:port]* portion for the canonical base
    and keep the URL path intact, so old rows are auto-corrected at
    send time without a backfill migration.

    No-op (returns the URL unchanged) when:
      * ``NAHLA_PUBLIC_BASE_URL`` is unset (e.g. local dev),
      * the URL has no recognisable managed-host substring,
      * or the URL is empty / malformed.

    Never raises.
    """
    if not url:
        return url
    base = (os.environ.get("NAHLA_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host:
            return url
        if not any(h in host for h in _PROD_HTTPS_HOSTS):
            return url

        base_parts = urlsplit(base if "://" in base else "https://" + base)
        new_scheme = base_parts.scheme or "https"
        new_netloc = base_parts.netloc or base.replace("https://", "").replace("http://", "")
        # Preserve the path/query/fragment exactly so file ids survive.
        return urlunsplit((new_scheme, new_netloc, parts.path, parts.query, parts.fragment))
    except Exception:  # noqa: BLE001 — never break a send over a URL parse hiccup
        return url


def _safe_filename(title: str, default: str = "file") -> str:
    """Sanitise a merchant-provided title into a WhatsApp-safe filename.

    WhatsApp tolerates fairly broad filenames but we strip anything that
    could trip a downstream system (slashes, control chars, leading
    dots). Empty / whitespace-only titles fall back to ``default``.
    """
    raw = (title or "").strip()
    if not raw:
        return default
    cleaned = re.sub(r"[\\/\x00-\x1f\x7f]", "", raw)
    cleaned = cleaned.lstrip(".").strip()
    return cleaned[:120] or default


def validate_media_for_send(
    attachment: Dict[str, Any],
    *,
    expected_tenant_id: int,
    db: Optional[Session] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """Final safety gate before we hand a file to WhatsApp.

    Returns ``(ok, error_reason, normalised_attachment)``:

    * ``ok=True`` → caller may dispatch via :func:`whatsapp_webhook._send_media_message`.
      The third element is the original attachment with safe defaults
      filled in (``filename`` for documents, ``media_type`` lower-cased).
    * ``ok=False`` → caller MUST drop the attachment, log the error
      reason, and continue — never crash the conversation.

    When ``db`` is provided we re-fetch the row to confirm the live
    state (active flag, tenant scope) hasn't changed since
    :func:`extract_media_markers` resolved the marker. This is the
    primary defence against stale ids that survive across turns.

    Validation contract:
      * tenant scope match
      * ``is_active`` true (re-check from DB if available)
      * ``media_type`` is one of the supported five
      * either a public HTTPS ``file_url`` (external) OR a readable
        on-disk ``storage_path`` (local upload)
      * size below the per-type WhatsApp ceiling (best-effort: only
        enforced when ``file_size_bytes`` is known)
    """
    if not isinstance(attachment, dict):
        return False, "invalid_payload_shape", None

    item = dict(attachment)
    media_id = item.get("id")
    if not isinstance(media_id, int):
        return False, "missing_or_invalid_id", None

    media_type = (item.get("media_type") or "").strip().lower()
    if media_type not in _SUPPORTED_MEDIA_TYPES:
        return False, f"unsupported_media_type:{media_type}", None
    item["media_type"] = media_type

    # Tenant scope — refuse cross-tenant ids even if the LLM somehow
    # learned of them.
    tenant_id_val = item.get("tenant_id")
    if isinstance(tenant_id_val, int) and tenant_id_val != int(expected_tenant_id):
        return False, "tenant_mismatch", None

    # Re-check live state from the DB if a session was provided.
    if db is not None:
        try:
            from models import AIMediaItem  # noqa: PLC0415

            row = (
                db.query(AIMediaItem)
                .filter(
                    AIMediaItem.id == media_id,
                    AIMediaItem.tenant_id == int(expected_tenant_id),
                )
                .first()
            )
            if row is None:
                return False, "row_missing_or_cross_tenant", None
            if not bool(row.is_active):
                return False, "row_disabled_mid_turn", None
            # Refresh fields with the live row in case they were edited
            # between marker resolution and the actual send.
            item["file_url"] = row.file_url
            item["storage_kind"] = row.storage_kind
            item["storage_path"] = row.storage_path
            item["mime_type"] = row.mime_type
            item["file_size_bytes"] = (
                int(row.file_size_bytes) if row.file_size_bytes is not None else None
            )
        except Exception as exc:  # noqa: BLE001 — DB failure must not crash the chat
            logger.warning(
                "[AIMedia.validate] DB recheck failed id=%s tenant=%s err=%s",
                media_id, expected_tenant_id, exc,
            )
            # Don't fail closed on a transient DB hiccup — fall back to
            # the marker-time fields we already have.

    storage_kind = (item.get("storage_kind") or "external").lower()
    file_url = (item.get("file_url") or "").strip()
    storage_path = (item.get("storage_path") or "").strip()

    # Auto-upgrade http:// → https:// for managed-platform hosts. Many
    # rows in the DB were created when ``request.base_url`` returned an
    # http:// scheme (Railway terminates TLS upstream so the inner app
    # sees plain HTTP), and Meta's WhatsApp Cloud API silently rejects
    # non-HTTPS media URLs in production.
    if file_url:
        upgraded = _maybe_force_https(file_url)
        if upgraded != file_url:
            logger.info(
                "[AIMedia.validate] upgraded url scheme tenant=%s id=%s "
                "from=http -> to=https",
                expected_tenant_id, media_id,
            )
            file_url = upgraded
            item["file_url"] = upgraded

        # Auto-canonicalise the host when NAHLA_PUBLIC_BASE_URL is set —
        # rewrites raw managed-platform preview hostnames (e.g.
        # ``*.up.railway.app``) onto the canonical public base
        # (``https://api.nahlah.ai``) so old rows persisted with the
        # preview URL are corrected at send time instead of leaking
        # infra to merchants/customers. Platform-wide: any tenant whose
        # AIMediaItem.file_url was captured before the env var was set
        # benefits automatically. No-op when the env var is empty or
        # the URL is already on the canonical host.
        canonical = _canonicalise_managed_host(file_url)
        if canonical != file_url:
            logger.info(
                "[AIMedia.validate] canonicalised host tenant=%s id=%s "
                "from=%s to=%s",
                expected_tenant_id, media_id,
                file_url[:80], canonical[:80],
            )
            file_url = canonical
            item["file_url"] = canonical

    if storage_kind == "local":
        # Locally-uploaded asset: the public URL must be HTTPS *and* the
        # underlying file must still be on disk so the streaming
        # endpoint can serve it to Meta.
        if not storage_path:
            return False, "missing_storage_path", None
        try:
            p = Path(storage_path)
            if not (p.exists() and p.is_file()):
                return False, "file_missing_on_disk", None
        except OSError as exc:
            return False, f"storage_io_error:{exc}", None
        if file_url and not file_url.lower().startswith(("http://", "https://")):
            return False, "invalid_public_url", None
        # Extra HTTPS hardening when configured. Meta WILL reject http://
        # in production but we still allow it for local dev.
        if file_url.lower().startswith("http://") and os.environ.get(
            "NAHLA_REQUIRE_HTTPS_MEDIA", ""
        ).lower() in ("1", "true", "yes"):
            return False, "https_required", None
    else:
        # Externally-hosted asset: HTTPS is non-negotiable in production.
        if not file_url:
            return False, "missing_file_url", None
        if not file_url.lower().startswith(("http://", "https://")):
            return False, "invalid_url_scheme", None

    # Size cap — only enforced when we know the size (locally-uploaded).
    size = item.get("file_size_bytes")
    if isinstance(size, int) and size > 0:
        cap = _WA_MAX_BYTES.get(media_type, 100 * 1024 * 1024)
        if size > cap:
            return False, f"size_exceeds_whatsapp_limit:{media_type}:{size}>{cap}", None

    # Documents / PDFs need a filename; derive a safe one if missing.
    if media_type in ("document", "pdf") and not item.get("filename"):
        # Prefer the original on-disk filename if we have one, fall back
        # to the merchant-provided title, fall back to a neutral default.
        derived = ""
        if storage_path:
            derived = Path(storage_path).name
        item["filename"] = _safe_filename(derived or str(item.get("title") or ""), default="document")

    return True, None, item
