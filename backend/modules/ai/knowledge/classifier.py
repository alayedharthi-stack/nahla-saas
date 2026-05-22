"""
backend/modules/ai/knowledge/classifier.py
──────────────────────────────────────────
Phase 2 GPT classifier for the Smart Store Knowledge Hub.

Takes a free-form merchant note + (optionally) attached media ids and
proposes a structured split into one or more
``merchant_knowledge_sections`` ops. The result is a JSON document
matching :data:`PROPOSAL_SCHEMA_NOTE` that the dashboard renders as a
diff preview the merchant approves or rejects per-op.

Architecture
────────────
* **Provider**: OpenAI-compatible chat completions endpoint. We use the
  same env vars as the existing
  ``backend/modules/ai/orchestrator/providers/openai_compatible_provider``
  so any model upgrade (gpt-4o, gpt-5, …) is a single ``OPENAI_MODEL``
  env change.
* **Source-of-truth rule**: the system prompt enforces the same
  precedence the runtime overlay enforces — proposed prices / stock /
  product names / direct URLs are flagged as conflicts when the
  platform (Salla / Zid / Shopify) is connected. The classifier never
  decides to overwrite platform fields; it only proposes.
* **Fallback**: when ``OPENAI_API_KEY`` is unset, the classifier
  returns a deterministic single-op proposal that puts the raw text
  into ``quick_update``. This keeps Phase 2 functional in local /
  test environments without surprising failures.
* **Strict JSON**: we ask the model for JSON and parse defensively —
  any malformed reply falls back to the deterministic single-op so the
  merchant always sees something to approve.

Inputs
──────
* ``raw_text``: what the merchant typed.
* ``attached_media``: list of ``{id, title, media_type, media_key}``.
* ``existing_sections``: list of ``{id, kind, title, body_preview}``
  so the model can propose ``update`` / ``merge`` instead of
  duplicating.
* ``platform_signal``: ``{connected: bool, platform: "salla"|...,
  warning: "..."}`` — flagged to the model so it never proposes a
  price that would collide with the storefront.
* ``available_kinds``: list of valid ``kind`` slugs the model is
  allowed to emit.

Output
──────
A dict with shape::

    {
      "proposed_ops": [
        {
          "op_id": "op-1",
          "op": "create" | "update" | "merge" | "link_media",
          "kind": "payment_method",
          "title": "...",
          "body": "...",
          "metadata": {},
          "target_section_id": null,
          "link_role": null,
          "media_id": null,
          "rationale": "..."
        },
        ...
      ],
      "conflicts": [
        {
          "with_section_id": null,
          "with_field": "platform_price",
          "kind": "platform_price",
          "explanation": "هذا السعر مربوط بسلة — لا نسمح بتجاوزه."
        }
      ],
      "confidence": 0.0..1.0,
      "model": "gpt-...",
      "fallback_used": bool
    }
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.ai.knowledge.classifier")


# ── Configuration ───────────────────────────────────────────────────────────
#
# We deliberately read the same env vars as the existing OpenAI-compatible
# provider so the platform owner can swap models in one place. We don't
# import the provider class directly because its ``call()`` signature
# assumes a chat-shaped (message + prompt) call that's slightly different
# from the JSON-shaped tool call we want here.

_API_KEY = os.environ.get("OPENAI_API_KEY", "")
_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
# Knowledge-classifier-specific override (so the platform owner can pin
# a stronger model on this surface without changing the runtime brain).
_KB_MODEL = os.environ.get(
    "NAHLA_KB_CLASSIFIER_MODEL",
    os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
)
_TIMEOUT = float(os.environ.get("NAHLA_KB_CLASSIFIER_TIMEOUT", "30"))
_MAX_OPS = 8


# ── Data shapes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExistingSection:
    """Minimal serialization of a ``MerchantKnowledgeSection`` for the prompt."""

    id: int
    kind: str
    title: Optional[str]
    body_preview: str  # truncated at 240 chars


@dataclass(frozen=True)
class AttachedMedia:
    id: int
    title: str
    media_type: str
    media_key: Optional[str]


@dataclass(frozen=True)
class PlatformSignal:
    """Tells the classifier which fields are platform-owned."""

    connected: bool
    platform: Optional[str]  # 'salla' | 'zid' | 'shopify'
    warning: str  # Arabic copy shown to the model verbatim


PROPOSAL_SCHEMA_NOTE = """\
أنت محلل ذكي. عملك هو تصنيف ملاحظة كتبها التاجر في "مركز معرفة المتجر"
وإنتاج اقتراحات منظمة بشكل JSON صارم فقط — بدون أي نص خارج JSON.

اقرأ نص التاجر، الوسائط المرفقة، الأقسام الحالية، وحالة منصة التجارة،
ثم أنتج كائن JSON بالشكل التالي بالضبط (لا تُضف حقولاً جديدة):

{
  "proposed_ops": [
    {
      "op_id": "op-1",
      "op": "create" | "update" | "merge" | "link_media",
      "kind": "<kind from الأنواع المسموحة>",
      "title": "<عنوان مختصر — عربي>",
      "body": "<النص المنسّق — عربي، جاهز للحقن في برومت كلود>",
      "metadata": {},
      "target_section_id": null | <int>,
      "link_role": null | "primary"|"evidence"|"barcode"|"tutorial_video"|"recipe_video"|"policy_pdf"|"certificate"|"map",
      "media_id": null | <int>,
      "rationale": "<سبب قصير لاختيار kind وهذه العملية>"
    }
  ],
  "conflicts": [
    {
      "with_section_id": null | <int>,
      "with_field": "platform_price" | "platform_stock" | "platform_name" | "platform_url" | "existing_section",
      "kind": "platform_price" | "platform_stock" | "platform_name" | "platform_url" | "existing_section",
      "explanation": "<شرح قصير عربي>"
    }
  ],
  "confidence": <رقم بين 0 و 1>
}

قواعد إلزامية:
- استخدم فقط kind من القائمة المسموحة.
- إذا كان النص يصف منتجاً (سعر، توفر، اسم، رابط) وكانت منصة التجارة موصولة،
  أنتج تعارضاً (conflict) في "conflicts" ولا تقترح create/update لتلك الحقول.
- إذا تشابه النص مع قسم موجود، ضع target_section_id واختر op="merge" أو "update".
- إذا أُرفقت وسائط ولها صلة بقسم مقترح، أنشئ op="link_media" إضافية بنفس
  target_section_id ومُعرّف media_id.
- لا تُنتج أكثر من 8 عمليات.
- ابدأ op_id بـ "op-" يتلوها رقم متسلسل من 1.
- ابقَ على body مختصراً (≤ 600 حرف) وبصياغة جاهزة للحقن في برومت كلود
  (سطور قصيرة، بدون رموز ماركدون كثيفة).
"""


# ── Public API ──────────────────────────────────────────────────────────────


def classify_quick_update(
    *,
    raw_text: str,
    attached_media: List[AttachedMedia],
    existing_sections: List[ExistingSection],
    platform_signal: PlatformSignal,
    available_kinds: List[str],
) -> Dict[str, Any]:
    """Return a structured proposal for the merchant to review.

    Never raises — failures degrade to a single ``quick_update``
    create-op so the merchant sees the text saved (and can edit/move
    it manually) rather than a hard error.
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return _empty_proposal(model=_KB_MODEL, reason="empty_input")

    if not _API_KEY:
        return _deterministic_fallback(
            raw_text=raw_text,
            attached_media=attached_media,
            reason="no_api_key",
        )

    prompt = _build_system_prompt(
        existing_sections=existing_sections,
        attached_media=attached_media,
        platform_signal=platform_signal,
        available_kinds=available_kinds,
    )
    try:
        raw_reply = _call_openai_chat(prompt=prompt, user_text=raw_text)
    except Exception as exc:  # noqa: BLE001 — always degrade
        logger.warning("[KB.classifier] call failed: %s", exc)
        return _deterministic_fallback(
            raw_text=raw_text,
            attached_media=attached_media,
            reason="call_error",
        )

    parsed = _parse_proposal(raw_reply)
    if parsed is None:
        logger.info(
            "[KB.classifier] could not parse model reply (len=%d) — falling back",
            len(raw_reply or ""),
        )
        return _deterministic_fallback(
            raw_text=raw_text,
            attached_media=attached_media,
            reason="parse_error",
            raw_reply=raw_reply,
        )

    normalized = _normalize_proposal(
        parsed,
        available_kinds=available_kinds,
        platform_signal=platform_signal,
        attached_media=attached_media,
    )
    normalized["model"] = _KB_MODEL
    normalized["fallback_used"] = False
    return normalized


# ── Prompt builder ──────────────────────────────────────────────────────────


def _build_system_prompt(
    *,
    existing_sections: List[ExistingSection],
    attached_media: List[AttachedMedia],
    platform_signal: PlatformSignal,
    available_kinds: List[str],
) -> str:
    parts: List[str] = [PROPOSAL_SCHEMA_NOTE]

    parts.append("الأنواع المسموحة (kind):\n- " + "\n- ".join(available_kinds))

    if platform_signal.connected:
        parts.append(
            f"حالة منصة التجارة: متصلة ({platform_signal.platform}).\n"
            f"تنبيه: {platform_signal.warning}"
        )
    else:
        parts.append(
            "حالة منصة التجارة: غير متصلة. يمكنك اقتراح أسعار أو توفر كمصدر "
            "وحيد، لكن نبّه إذا اقترحت ذلك."
        )

    if existing_sections:
        lines = []
        for s in existing_sections[:30]:
            title = (s.title or "").strip() or s.kind
            preview = (s.body_preview or "").strip().replace("\n", " ")[:200]
            lines.append(f"- id={s.id} | kind={s.kind} | title={title} | preview={preview}")
        parts.append("الأقسام الحالية (للمساعدة في update/merge):\n" + "\n".join(lines))
    else:
        parts.append("الأقسام الحالية: (لا يوجد بعد)")

    if attached_media:
        lines = []
        for m in attached_media:
            key_part = f" | media_key={m.media_key}" if m.media_key else ""
            lines.append(f"- id={m.id} | type={m.media_type} | title={m.title}{key_part}")
        parts.append("الوسائط المرفقة:\n" + "\n".join(lines))
    else:
        parts.append("الوسائط المرفقة: (لا يوجد)")

    parts.append(
        "أعد JSON فقط — لا شرح، لا ماركدون خارج JSON، لا تعليقات. "
        "تأكد أن JSON قابل للتحليل من Python json.loads مباشرة."
    )
    return "\n\n".join(parts)


# ── HTTP call ──────────────────────────────────────────────────────────────


def _call_openai_chat(*, prompt: str, user_text: str) -> str:
    """Call the OpenAI-compatible chat completions endpoint in JSON mode."""
    import httpx  # noqa: PLC0415 — deferred import keeps cold start light

    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": _KB_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.2,
        # JSON mode where supported. Older models ignore this field; we
        # still parse defensively below.
        "response_format": {"type": "json_object"},
        "max_tokens": 1500,
    }
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            f"{_API_BASE}/chat/completions",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
    return str(data["choices"][0]["message"]["content"] or "")


# ── Parsing & normalization ────────────────────────────────────────────────


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def _parse_proposal(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return None
    # Fast path: valid JSON.
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fallback: extract the first JSON-looking block.
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _normalize_proposal(
    parsed: Dict[str, Any],
    *,
    available_kinds: List[str],
    platform_signal: PlatformSignal,
    attached_media: List[AttachedMedia],
) -> Dict[str, Any]:
    proposed = parsed.get("proposed_ops") or []
    if not isinstance(proposed, list):
        proposed = []

    kinds_set = set(available_kinds)
    media_id_set = {m.id for m in attached_media}

    clean_ops: List[Dict[str, Any]] = []
    for idx, raw_op in enumerate(proposed[:_MAX_OPS], start=1):
        if not isinstance(raw_op, dict):
            continue
        op = str(raw_op.get("op") or "create").strip().lower()
        if op not in ("create", "update", "merge", "link_media"):
            op = "create"

        kind = str(raw_op.get("kind") or "custom").strip().lower()
        if kind not in kinds_set:
            kind = "custom"

        title = (raw_op.get("title") or "").strip()[:255] or None
        body = (raw_op.get("body") or "").strip()[:8000]
        metadata = raw_op.get("metadata") if isinstance(raw_op.get("metadata"), dict) else {}
        target_id = raw_op.get("target_section_id")
        try:
            target_id_int: Optional[int] = int(target_id) if target_id not in (None, "", "null") else None
        except Exception:
            target_id_int = None
        link_role = raw_op.get("link_role")
        link_role_s = str(link_role).strip().lower() if link_role else None
        media_id_raw = raw_op.get("media_id")
        try:
            media_id_int: Optional[int] = int(media_id_raw) if media_id_raw not in (None, "", "null") else None
        except Exception:
            media_id_int = None
        # Reject media ids the merchant didn't actually attach — we
        # never accept a hallucinated media reference.
        if media_id_int is not None and media_id_int not in media_id_set:
            media_id_int = None
        rationale = (raw_op.get("rationale") or "").strip()[:600]

        clean_ops.append({
            "op_id": str(raw_op.get("op_id") or f"op-{idx}"),
            "op": op,
            "kind": kind,
            "title": title,
            "body": body,
            "metadata": metadata,
            "target_section_id": target_id_int,
            "link_role": link_role_s,
            "media_id": media_id_int,
            "rationale": rationale,
        })

    # Conflicts: re-tag with a stable kind set + bound the list size.
    raw_conflicts = parsed.get("conflicts") or []
    if not isinstance(raw_conflicts, list):
        raw_conflicts = []
    clean_conflicts: List[Dict[str, Any]] = []
    for c in raw_conflicts[:_MAX_OPS]:
        if not isinstance(c, dict):
            continue
        clean_conflicts.append({
            "with_section_id": _safe_int(c.get("with_section_id")),
            "with_field": str(c.get("with_field") or "").strip()[:64],
            "kind": str(c.get("kind") or "").strip().lower()[:32] or "existing_section",
            "explanation": (str(c.get("explanation") or "").strip())[:600],
        })

    # When the platform is connected, post-pend a "soft" conflict for
    # every op the model marked as create/update of product-bound kinds
    # that look like price/stock claims. This is a belt-and-braces check
    # in case the model forgot to flag them itself.
    if platform_signal.connected:
        for op in clean_ops:
            if _looks_like_platform_field_claim(op["body"]) and not any(
                c.get("kind") == "platform_price" for c in clean_conflicts
            ):
                clean_conflicts.append({
                    "with_section_id": None,
                    "with_field": "platform_price",
                    "kind": "platform_price",
                    "explanation": (
                        f"يبدو أن النص يقترح سعراً/توفراً يتعارض مع بيانات "
                        f"{platform_signal.platform or 'منصة التجارة'} الرسمية — "
                        "السعر والمخزون يأتيان من المنصة."
                    ),
                })

    try:
        confidence = float(parsed.get("confidence") or 0)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "proposed_ops": clean_ops,
        "conflicts": clean_conflicts,
        "confidence": confidence,
    }


_PRICE_HINT_RE = re.compile(
    r"(\d+[\.,]?\d*)\s*(ريال|ر\.س|ر\\.?س|SAR|sar|درهم|aed|usd|\$)",
    re.IGNORECASE,
)
_STOCK_HINT_RE = re.compile(
    r"(متوفر|غير متوفر|نفد|نفذ|stock|out of stock|in stock)",
    re.IGNORECASE,
)


def _looks_like_platform_field_claim(body: str) -> bool:
    if not body:
        return False
    return bool(_PRICE_HINT_RE.search(body) or _STOCK_HINT_RE.search(body))


def _safe_int(val: Any) -> Optional[int]:
    if val in (None, "", "null"):
        return None
    try:
        return int(val)
    except Exception:
        return None


# ── Deterministic fallback ─────────────────────────────────────────────────


def _empty_proposal(*, model: str, reason: str) -> Dict[str, Any]:
    return {
        "proposed_ops": [],
        "conflicts": [],
        "confidence": 0.0,
        "model": model,
        "fallback_used": True,
        "fallback_reason": reason,
    }


def _deterministic_fallback(
    *,
    raw_text: str,
    attached_media: List[AttachedMedia],
    reason: str,
    raw_reply: Optional[str] = None,
) -> Dict[str, Any]:
    """Always return *something* the merchant can approve.

    The user's text becomes a single ``quick_update`` op (which lives
    in the visible "Quick Updates" bucket so they immediately see
    where it landed); any attached media become ``link_media`` ops
    targeting the same op as ``op-1``.
    """
    ops: List[Dict[str, Any]] = [
        {
            "op_id": "op-1",
            "op": "create",
            "kind": "quick_update",
            "title": "ملاحظة سريعة",
            "body": raw_text[:8000],
            "metadata": {"fallback_reason": reason},
            "target_section_id": None,
            "link_role": None,
            "media_id": None,
            "rationale": "تعذّر تصنيف النص آلياً — حفظناه كملاحظة سريعة لتنقله يدوياً.",
        }
    ]
    for idx, m in enumerate(attached_media, start=2):
        ops.append({
            "op_id": f"op-{idx}",
            "op": "link_media",
            "kind": "quick_update",
            "title": None,
            "body": "",
            "metadata": {},
            "target_section_id": None,
            "link_role": "primary",
            "media_id": m.id,
            "rationale": f"إرفاق الوسيط {m.title}",
        })
    return {
        "proposed_ops": ops,
        "conflicts": [],
        "confidence": 0.0,
        "model": _KB_MODEL,
        "fallback_used": True,
        "fallback_reason": reason,
        "raw_reply": (raw_reply or "")[:500] if raw_reply else None,
    }


# ── Test seam ──────────────────────────────────────────────────────────────


def _make_op_id() -> str:  # pragma: no cover — exposed for tests
    return f"op-{uuid.uuid4().hex[:8]}"
