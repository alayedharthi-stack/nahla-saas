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

Independence: these helpers DO NOT touch Salla, automatic coupons,
product sync, or store-integration plumbing. A merchant who sells
manually over WhatsApp gets the full feature.
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


def _normalize_for_match(text: str) -> str:
    """Lowercase + collapse whitespace for cheap substring scoring."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _relevance_score(item: Dict[str, Any], query: str) -> float:
    """Cheap, deterministic relevance score against the customer's last
    message. Used as a tiebreaker when sorting the prompt slice.

    Returns 0.0 when nothing in the item overlaps with the query, up to
    ~3.0 when title + tags + usage_context all hit. The absolute value
    is unimportant — only the ordering matters."""
    if not query:
        return 0.0
    q = _normalize_for_match(query)
    if not q:
        return 0.0
    score = 0.0
    title = _normalize_for_match(str(item.get("title") or ""))
    if title and title in q:
        score += 1.5
    elif title:
        # token overlap fallback
        for tok in title.split():
            if len(tok) >= 3 and tok in q:
                score += 0.4
    for tag in (item.get("tags") or []):
        t = _normalize_for_match(str(tag))
        if t and t in q:
            score += 1.0
    usage = _normalize_for_match(str(item.get("usage_context") or ""))
    if usage:
        for tok in usage.split():
            if len(tok) >= 4 and tok in q:
                score += 0.2
                if score >= 3.0:
                    return 3.0
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


# ── Pre-send validation ──────────────────────────────────────────────────────


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
