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
#
# KB-2 (May 2026 #23): the default is now ``gpt-4.1``. The previous
# ``gpt-4o-mini`` default was the documented cause of:
#   * weak Arabic semantic classification
#   * paraphrase-only "structured" output (almost no transformation)
#   * behavioral text leaking into commerce sections
#   * confused taxonomy assignment under platform-conflict prompts
# Railway env vars (``NAHLA_KB_CLASSIFIER_MODEL`` or ``OPENAI_MODEL``)
# still take precedence so the platform owner can pin a different
# model without a redeploy.
_KB_MODEL = os.environ.get(
    "NAHLA_KB_CLASSIFIER_MODEL",
    os.environ.get("OPENAI_MODEL", "gpt-4.1"),
)
_TIMEOUT = float(os.environ.get("NAHLA_KB_CLASSIFIER_TIMEOUT", "30"))
_MAX_OPS = 8


# ── KB-2 few-shot examples ──────────────────────────────────────────────────
#
# Examples are appended to the system prompt verbatim so the model learns
# the SHAPE of the JSON we want and — critically — the boundary between
# commerce kinds and behavioral kinds. Each example is paired with the
# minimal JSON the classifier should emit; we keep them short so the prompt
# stays under a few KB.
#
# Why 5 examples specifically (not more):
#   * Each example burns ~300 tokens in the system prompt. Five lines up
#     with the empirical sweet spot from the brain's intent classifier
#     (see ``modules/ai/brain/intent/social_classifier.py`` — same scale).
#   * They cover the three axes the previous model regressed on:
#       1. Behavior vs commerce taxonomy boundary (#2, #5).
#       2. Specific-vs-generic kind picking inside commerce (#1, #3).
#       3. Platform-conflict awareness for price/stock claims (#4).
#   * Adding more examples did not move the dial in offline replay; the
#     ``gpt-4.1`` upgrade is what carries the bulk of the quality gain.
_FEW_SHOT_EXAMPLES: List[Dict[str, Any]] = [
    {
        # Specific commerce kind — must NOT collapse to generic "payment_method".
        "input": "باركود الراجحي للتحويل البنكي",
        "expected": {
            "proposed_ops": [{
                "op_id": "op-1",
                "op": "create",
                "kind": "bank_transfer",
                "title": "باركود الراجحي للتحويل البنكي",
                "body": "يمكن للعميل التحويل البنكي عبر باركود الراجحي.",
                "rationale": (
                    "نص يخص حساب/باركود بنكي → kind=bank_transfer "
                    "(أدقّ من payment_method)."
                ),
            }],
            "conflicts": [],
            "confidence": 0.95,
        },
    },
    {
        # CRITICAL behavioral boundary — forbidden phrases must NEVER
        # land in store_info / payment_method / shipping_*.
        "input": "لا تقل حبيبي أو قلبي للعملاء",
        "expected": {
            "proposed_ops": [{
                "op_id": "op-1",
                "op": "create",
                "kind": "forbidden_phrases",
                "title": "كلمات ممنوعة في الرد",
                "body": (
                    "لا يستخدم المساعد كلمات: «حبيبي»، «قلبي»، «يا غالي» "
                    "أو أي عبارة تدليل مفرطة. النبرة محترمة ومحايدة."
                ),
                "rationale": (
                    "نص يصف قاعدة سلوكية للنبرة → kind=forbidden_phrases "
                    "(taxonomy=assistant_behavior، ليس commerce)."
                ),
            }],
            "conflicts": [],
            "confidence": 0.97,
        },
    },
    {
        # Specific shipping kind, not a generic store_info dump.
        "input": "الشحن المبرد مهم بالصيف للعسل",
        "expected": {
            "proposed_ops": [{
                "op_id": "op-1",
                "op": "create",
                "kind": "cold_shipping",
                "title": "الشحن المبرد في الصيف",
                "body": (
                    "في فصل الصيف يُستخدم الشحن المبرّد لمنتجات العسل "
                    "للمحافظة على الجودة وتجنّب الذوبان عند الحرارة العالية."
                ),
                "rationale": (
                    "نص يخص ظرف شحن خاص بالصيف للعسل → kind=cold_shipping "
                    "(أدقّ من shipping_carrier أو shipping_zones)."
                ),
            }],
            "conflicts": [],
            "confidence": 0.93,
        },
    },
    {
        # Platform conflict — must NOT create a knowledge fact for stock
        # when the platform is connected. The conflict block carries the
        # entire signal; ``proposed_ops`` stays empty for the stock claim.
        "input": "بوكس الأرباع نفد مؤقتاً",
        "expected": {
            "proposed_ops": [],
            "conflicts": [{
                "with_section_id": None,
                "with_field": "platform_stock",
                "kind": "platform_stock",
                "explanation": (
                    "النص يدّعي حالة توفر لمنتج «بوكس الأرباع» — والمنصة موصولة، "
                    "فالمخزون يأتي من المنصة وليس من قاعدة المعرفة."
                ),
            }],
            "confidence": 0.90,
        },
        "only_when_platform_connected": True,
    },
    {
        # Another behavioral boundary — tone / dialect / emoji rules
        # must route to assistant_behavior, never to dialect inside
        # store_info (which is reserved for ai_settings.default_language).
        "input": "استخدم لهجة خليجية خفيفة وإيموجي بسيط",
        "expected": {
            "proposed_ops": [{
                "op_id": "op-1",
                "op": "create",
                "kind": "response_tone",
                "title": "نبرة الرد المطلوبة",
                "body": (
                    "يردّ المساعد بلهجة خليجية خفيفة، ودودة، مع استخدام "
                    "محدود للإيموجي (إيموجي واحد كحدّ أقصى عند الحاجة)."
                ),
                "rationale": (
                    "نص يصف النبرة + سياسة الإيموجي → kind=response_tone "
                    "(taxonomy=assistant_behavior). لا تستخدم store_info."
                ),
            }],
            "conflicts": [],
            "confidence": 0.95,
        },
    },
]


def _format_few_shot_examples(*, platform_connected: bool) -> str:
    """Render the few-shot examples for the system prompt.

    Examples flagged ``only_when_platform_connected`` are skipped when
    the platform is NOT connected — otherwise the model learns to
    reject stock claims in environments where it should actually
    accept them as the sole source of truth.
    """
    parts: List[str] = ["أمثلة تعليمية (تعلّم الشكل من الحالات الحقيقية):"]
    for idx, ex in enumerate(_FEW_SHOT_EXAMPLES, start=1):
        if ex.get("only_when_platform_connected") and not platform_connected:
            continue
        parts.append("")
        parts.append(f"[{idx}] إدخال التاجر:")
        parts.append(f"    «{ex['input']}»")
        parts.append("    الخرج المتوقع (JSON):")
        rendered = json.dumps(ex["expected"], ensure_ascii=False, indent=2)
        for ln in rendered.splitlines():
            parts.append(f"    {ln}")
    return "\n".join(parts)


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
ثم أنتج كائن JSON بالشكل التالي بالضبط (لا تُضف حقولاً جديدة خارج هذا الشكل):

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
  (سطور قصيرة، بدون رموز ماركدون كثيفة) — إلا إذا كان النص عبارة عن
  أمثلة فعلية لعبارات العملاء، فالاختصار هنا ممنوع.
- إذا أرسل التاجر قائمة صيغ يقولها العملاء (مثل: "أنا قريب"،
  "أنا عند البوابة"، "وين المعرض"، "أرسل اللوكيشن"، "وين رقمه") فلا
  تلخصها إلى عبارة عامة مثل "فهم تنوع تعبيرات العميل". احفظ كل صيغة
  كما هي في body كسطور منفصلة، واجعل metadata تحتوي:
  {"knowledge_mode":"intent_surface_examples" أو "artifact_trigger_examples",
   "preserve_surface_forms":true,
   "examples_to_preserve":[...],
   "intent":"<نية تنظيمية>",
   "artifact_target":"<إن وجد>"}
  التشابه بين هذه الصيغ يعني grouping تنظيمي فقط، وليس حذفاً أو دمجاً
  مدمراً. الأولوية: richness > brevity.

═══ فصل السلوك عن المعرفة التجارية (KB-2) — قاعدة حرجة ═══
إذا كان نص التاجر يتحدث عن أيٍّ من الآتي، فيجب تصنيفه داخل taxonomy
"assistant_behavior" حصراً، وليس داخل أقسام المعرفة التجارية
(payment_method / bank_transfer / shipping_carrier / cold_shipping /
store_story / dialect / working_hours / branches / product_*…):
- طريقة كلام المساعد / أسلوب الرد / النبرة / اللهجة المطلوبة منه
- الكلمات الممنوعة (لا تقل / تجنب كلمة / لا يقول…)
- متى يحوّل المساعد لموظف بشري (escalation)
- شخصية المساعد، اسمه، دوره، كيف يعرّف نفسه
- اسم صاحب المتجر وهل يُذكر للعملاء
- سياسة الإيموجي (متى يستخدم، كم العدد)
- قواعد الامتثال (ادعاءات طبية ممنوعة، تحذيرات شرعية…)

الـ kinds المسموحة لهذه الحالة (assistant_behavior فقط):
- forbidden_phrases    — كلمات وعبارات ممنوعة
- allowed_style        — أسلوب الكلام المسموح به
- response_tone        — نبرة الرد / اللهجة / الطول الافتراضي
- emoji_policy         — سياسة الإيموجي
- escalation_rules     — متى يحوّل لبشري
- compliance_rules     — ممنوعات قانونية/طبية/شرعية
- owner_identity       — هوية صاحب المتجر
- assistant_identity   — هوية الذكاء (اسمه، دوره)

أمثلة على الفصل:
- "لا تقل حبيبي للعملاء"           → kind=forbidden_phrases (NOT store_info)
- "ردّ بلهجة خليجية مختصرة"          → kind=response_tone     (NOT dialect)
- "اسم المتجر يكتب كذا في الرد"     → kind=owner_identity   (NOT store_story)
- "حوّل لموظف لو طلب شكوى"          → kind=escalation_rules (NOT faq)
- "ممنوع أي ادعاء علاجي للعسل"      → kind=compliance_rules (NOT product_benefit)
- "إيموجي واحد كحد أقصى لكل رد"     → kind=emoji_policy     (NOT reply_style)

السبب: قواعد السلوك تُحقن في طبقة "HIGH PRIORITY" المنفصلة عن
"قاعدة المعرفة" في برومت كلود — خلطها مع المعرفة التجارية يلوّث
الـ retrieval ويجعل الذكاء يستحضرها وقت سؤال العميل عن الشحن أو الدفع.
"""


# ── Public API ──────────────────────────────────────────────────────────────


def classify_quick_update(
    *,
    raw_text: str,
    attached_media: List[AttachedMedia],
    existing_sections: List[ExistingSection],
    platform_signal: PlatformSignal,
    available_kinds: List[str],
    tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Return a structured proposal for the merchant to review.

    Never raises — failures degrade to a single ``quick_update``
    create-op so the merchant sees the text saved (and can edit/move
    it manually) rather than a hard error.

    Parameters
    ──────────
    tenant_id: Optional[int]
        Only used for structured ``[KB_CLASSIFIER]`` logging — the
        classifier itself is tenant-agnostic. Kept optional so legacy
        callers (e.g. ad-hoc CLI tools) don't break.
    """
    import time  # noqa: PLC0415

    started = time.monotonic()
    raw_text = (raw_text or "").strip()
    if not raw_text:
        _emit_kb_log(
            tenant_id=tenant_id, model=_KB_MODEL, kind=None,
            confidence=0.0, fallback=True, fallback_reason="empty_input",
            conflicts_count=0, ops_count=0, retry_count=0,
            response_length=0, latency_ms=0,
            original_text="",
        )
        return _empty_proposal(model=_KB_MODEL, reason="empty_input")

    if not _API_KEY:
        result = _deterministic_fallback(
            raw_text=raw_text,
            attached_media=attached_media,
            reason="no_api_key",
        )
        _log_result(result, tenant_id=tenant_id, started=started,
                    retry_count=0, raw_text=raw_text, response_length=0)
        return result

    prompt = _build_system_prompt(
        existing_sections=existing_sections,
        attached_media=attached_media,
        platform_signal=platform_signal,
        available_kinds=available_kinds,
    )

    raw_reply, retry_count, call_failed = _call_with_retry(
        prompt=prompt, user_text=raw_text,
    )
    if call_failed:
        result = _deterministic_fallback(
            raw_text=raw_text,
            attached_media=attached_media,
            reason="call_error",
        )
        _log_result(result, tenant_id=tenant_id, started=started,
                    retry_count=retry_count, raw_text=raw_text,
                    response_length=len(raw_reply or ""))
        return result

    parsed = _parse_proposal(raw_reply)
    if parsed is None:
        # ── KB-2 step 8: one-shot retry on parse_error ────────────────
        # The original ``raw_reply`` was unparseable — many production
        # failures come from the model emitting a stray sentence before
        # the JSON object even in ``response_format=json_object`` mode.
        # We retry exactly once with a minimal reminder prompt at
        # ``temperature=0``. A second failure falls back deterministically.
        if retry_count == 0:
            logger.info(
                "[KB.classifier] parse_error on first attempt — retrying once "
                "(reply_len=%d)",
                len(raw_reply or ""),
            )
            try:
                raw_reply = _call_openai_chat(
                    prompt=prompt + "\n\n" + _PARSE_RETRY_REMINDER,
                    user_text=raw_text,
                )
                retry_count = 1
                parsed = _parse_proposal(raw_reply)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[KB.classifier] retry call failed: %s", exc)
                parsed = None

        if parsed is None:
            logger.info(
                "[KB.classifier] could not parse model reply after retry "
                "(len=%d) — falling back",
                len(raw_reply or ""),
            )
            result = _deterministic_fallback(
                raw_text=raw_text,
                attached_media=attached_media,
                reason="parse_error",
                raw_reply=raw_reply,
            )
            _log_result(result, tenant_id=tenant_id, started=started,
                        retry_count=retry_count, raw_text=raw_text,
                        response_length=len(raw_reply or ""))
            return result

    normalized = _normalize_proposal(
        parsed,
        available_kinds=available_kinds,
        platform_signal=platform_signal,
        attached_media=attached_media,
    )
    normalized["model"] = _KB_MODEL
    normalized["fallback_used"] = False
    _log_result(normalized, tenant_id=tenant_id, started=started,
                retry_count=retry_count, raw_text=raw_text,
                response_length=len(raw_reply or ""))
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

    # KB-2: few-shot examples (the bulk of the quality gain for
    # behavioral-boundary detection — see ``_FEW_SHOT_EXAMPLES``).
    parts.append(_format_few_shot_examples(
        platform_connected=platform_signal.connected,
    ))

    parts.append(
        "أعد JSON فقط — لا شرح، لا ماركدون خارج JSON، لا تعليقات. "
        "تأكد أن JSON قابل للتحليل من Python json.loads مباشرة."
    )
    return "\n\n".join(parts)


# ── HTTP call ──────────────────────────────────────────────────────────────

# KB-2 step 8 — appended on the retry call when the first reply failed
# JSON parsing. We deliberately keep this short: the original prompt is
# unchanged so we don't drift the taxonomy, only nudge the format.
_PARSE_RETRY_REMINDER = (
    "تنبيه: المحاولة السابقة كان فيها نص خارج JSON أو JSON غير صالح. "
    "هذه المحاولة الأخيرة: أعد JSON فقط — كائن JSON واحد قابل لـ "
    "json.loads مباشرة، بدون أي حرف قبل { أو بعد }."
)


def _call_openai_chat(*, prompt: str, user_text: str) -> str:
    """Call the OpenAI-compatible chat completions endpoint in JSON mode.

    KB-2: ``temperature=0`` for deterministic classification — random
    paraphrase variance was a major contributor to the merchant-facing
    "the AI re-wrote my note for nothing" complaint.
    """
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
        "temperature": 0,
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


def _call_with_retry(
    *, prompt: str, user_text: str,
) -> Tuple[str, int, bool]:
    """Single attempt with structured retry-count tracking.

    The retry-on-parse-error path is in ``classify_quick_update`` because
    it depends on whether ``_parse_proposal`` succeeded; here we only
    handle hard call errors (network / 5xx) where retrying with the
    same prompt would just thrash. Returns ``(reply, retry_count, failed)``.
    """
    try:
        reply = _call_openai_chat(prompt=prompt, user_text=user_text)
        return reply, 0, False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB.classifier] call failed: %s", exc)
        return "", 0, True


# ── Structured logging (KB-2 step 7) ──────────────────────────────────────


def _emit_kb_log(
    *,
    tenant_id: Optional[int],
    model: str,
    kind: Optional[str],
    confidence: float,
    fallback: bool,
    fallback_reason: Optional[str],
    conflicts_count: int,
    ops_count: int,
    retry_count: int,
    response_length: int,
    latency_ms: int,
    original_text: str,
) -> None:
    """Emit a single structured ``[KB_CLASSIFIER]`` log line.

    Designed to be ``grep -E '\\[KB_CLASSIFIER\\]'`` friendly on Railway
    so the platform owner can spot:

      * which tenants generate the most fallbacks (fallback=True),
      * latency tails (latency_ms),
      * which kinds dominate (kind=…),
      * whether retries are recovering parse errors (retry_count>0).

    We log a SHA-style truncated input fingerprint (first 80 chars,
    newlines collapsed) — never the full body — so PII / merchant secrets
    don't end up in log aggregators. ``original_text`` is also truncated.
    """
    text_preview = (
        (original_text or "")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()[:80]
    )
    logger.info(
        "[KB_CLASSIFIER] tenant_id=%s model=%s kind=%s confidence=%.2f "
        "fallback=%s fallback_reason=%s conflicts_count=%d ops_count=%d "
        "retry_count=%d response_length=%d latency_ms=%d original_text=%r",
        tenant_id if tenant_id is not None else "-",
        model,
        kind or "-",
        float(confidence or 0.0),
        "true" if fallback else "false",
        fallback_reason or "-",
        conflicts_count,
        ops_count,
        retry_count,
        response_length,
        latency_ms,
        text_preview,
    )


def _log_result(
    result: Dict[str, Any],
    *,
    tenant_id: Optional[int],
    started: float,
    retry_count: int,
    raw_text: str,
    response_length: int,
) -> None:
    import time  # noqa: PLC0415

    latency_ms = int((time.monotonic() - started) * 1000)
    ops = list(result.get("proposed_ops") or [])
    conflicts = list(result.get("conflicts") or [])
    chosen_kind = (ops[0].get("kind") if ops else None) or None
    _emit_kb_log(
        tenant_id=tenant_id,
        model=str(result.get("model") or _KB_MODEL),
        kind=chosen_kind,
        confidence=float(result.get("confidence") or 0.0),
        fallback=bool(result.get("fallback_used")),
        fallback_reason=result.get("fallback_reason"),
        conflicts_count=len(conflicts),
        ops_count=len(ops),
        retry_count=retry_count,
        response_length=response_length,
        latency_ms=latency_ms,
        original_text=raw_text,
    )


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
        # Stage 1-3 Behavioral Expansion contract: if the model kept
        # customer utterance examples in the body but forgot the
        # preservation metadata, add it deterministically. This keeps
        # "أنا عند البوابة / وين المعرض / أرسل اللوكيشن" as runtime
        # signals instead of letting later UI/advisor layers treat them
        # as summarizable prose.
        if _looks_like_surface_examples(body):
            meta_surface = _surface_metadata_from_body(body)
            metadata = {**metadata, **meta_surface}
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


_SURFACE_LINE_PREFIX_RE = re.compile(
    r"^\s*(?:[-*•]+|\d+[\).:-]?|[،,؛;:\-–—]+)?\s*"
)
_SURFACE_HINTS = (
    "قريب", "بالطريق", "البوابة", "بوابة", "وين المعرض",
    "وين المحل", "وين الفرع", "وين موقع", "المدخل", "مواقف",
    "المواقف", "قدام", "جنبكم", "لوكيشن", "اللوكيشن",
    "الخريطة", "الخرايط", "وين رقمه", "رقم", "باركود",
)


def _clean_surface_example(line: str) -> str:
    if not line:
        return ""
    s = _SURFACE_LINE_PREFIX_RE.sub("", str(line)).strip()
    s = s.strip(" \t\r\n\"'“”«»")
    return re.sub(r"\s+", " ", s).strip()


def _extract_surface_examples(text: str) -> List[str]:
    if not text:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[\n\r]+", text):
        ex = _clean_surface_example(raw)
        if not ex or len(ex) > 90:
            continue
        if ex in seen:
            continue
        seen.add(ex)
        out.append(ex)
    if len(out) <= 1:
        inline = _clean_surface_example(text)
        parts = [
            _clean_surface_example(p)
            for p in re.split(r"\s*[،,؛;]\s*", inline)
        ]
        out = []
        seen = set()
        for ex in parts:
            if not ex or len(ex) > 90 or ex in seen:
                continue
            seen.add(ex)
            out.append(ex)
    return out


def _looks_like_surface_examples(text: str) -> bool:
    examples = _extract_surface_examples(text)
    if len(examples) < 4:
        return False
    joined = "\n".join(examples).lower()
    return any(h in joined for h in _SURFACE_HINTS)


def _surface_metadata_from_body(body: str) -> Dict[str, Any]:
    examples = _extract_surface_examples(body)
    joined = "\n".join(examples).lower()
    if any(h in joined for h in ("رقم", "اكلم", "أكلم", "وين رقمه")):
        intent = "ask_staff_contact"
        artifact_target = "staff_phone"
        mode = "artifact_trigger_examples"
    elif any(h in joined for h in ("باركود", "تحويل", "الراجحي", "qr")):
        intent = "ask_payment_barcode_or_transfer"
        artifact_target = "payment_barcode"
        mode = "artifact_trigger_examples"
    else:
        intent = "ask_location_or_arrival_help"
        artifact_target = "maps_link_or_staff_contact"
        mode = "artifact_trigger_examples"
    return {
        "knowledge_mode": mode,
        "preserve_surface_forms": True,
        "examples_to_preserve": examples,
        "intent": intent,
        "artifact_target": artifact_target,
    }


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
