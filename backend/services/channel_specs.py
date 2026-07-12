"""
services/channel_specs.py
─────────────────────────
Channel Registry — the SINGLE source of truth for "what does each
output channel require from a Nahla Catalog product?".

Architectural role (Phase 1 of Product Studio, May 2026 #15)
─────────────────────────────────────────────────────────────
This module is the abstraction that makes the catalog channel-agnostic
without losing channel-specific quality gates. Every consumer (the
readiness engine, the live-counter UI, future publish jobs) reads from
this registry — there is no other place where "WhatsApp title is
capped at 60 chars" or "Meta requires a 500×500 image" lives.

    PRODUCT  ──┬──→  ChannelSpec (meta_catalog)   ──→  ChannelReadiness
               ├──→  ChannelSpec (whatsapp)       ──→  ChannelReadiness
               ├──→  ChannelSpec (campaigns)      ──→  ChannelReadiness
               ├──→  ChannelSpec (ai)             ──→  ChannelReadiness
               └──→  ChannelSpec (google_merchant) ──→  ChannelReadiness

Adding a new channel = adding one ``ChannelSpec`` instance + one entry
in ``REGISTRY``. Nothing else changes. The readiness engine reflects
the new channel automatically, the dashboard's per-channel badge wall
expands automatically, the live-counter UI picks up the new strictest
limit per field automatically.

Field-extraction contract
─────────────────────────
Each ``FieldConstraint`` names ONE logical field (e.g. ``"title"``,
``"image_url"``, ``"description"``). The readiness engine looks up
the value via :func:`extract_field` — a single resolver that knows
where each logical field lives on a Product / draft dict (some live
as top-level columns, others inside ``extra_metadata`` JSONB). When
the schema migration in Phase 2 promotes JSONB fields to top-level
columns, ONLY this resolver changes — every ChannelSpec stays
identical, every readiness call stays identical.

Why we keep it pure
────────────────────
* No DB session in this module. The registry + spec lookups are
  pure data — tests can lock against the constraints without
  spinning up a session.
* No I/O. Live counters in the frontend POST a draft dict to a
  preview endpoint that calls ``compute_readiness``; the registry
  itself never reaches out.
* No side effects when ``register_channel`` is called — it's idempotent
  (later registrations REPLACE the earlier one for the same channel
  name) so tests can monkey-patch a spec without polluting state.

Source of constraint numbers
────────────────────────────
Numbers are pinned from the public documentation as of May 2026:

  * Meta Catalog product fields:
    https://developers.facebook.com/docs/marketing-api/catalog/reference/product-item/
  * WhatsApp Cloud API product messages (catalog products inherit
    Meta limits; only ``body`` text differs):
    https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
  * Google Merchant product feed spec:
    https://support.google.com/merchants/answer/7052112

Where docs allow longer values than what we enforce, we cap to a
safer number to leave room for emoji / multibyte expansion. Each
constraint carries a short comment on the rationale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Public channel name constants — mirror ``core/catalog.CHANNEL_*``
# ─────────────────────────────────────────────────────────────────────────────

CHANNEL_META_CATALOG    = "meta_catalog"
CHANNEL_WHATSAPP        = "whatsapp"
CHANNEL_CAMPAIGNS       = "campaigns"
CHANNEL_AI              = "ai"
CHANNEL_GOOGLE_MERCHANT = "google_merchant"   # readiness-only in Phase 1


# ─────────────────────────────────────────────────────────────────────────────
# Constraint dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FieldConstraint:
    """One requirement applied by ONE channel to ONE logical field.

    A field that's required by multiple channels appears multiple times
    in the registry (once per channel). The readiness engine aggregates
    across channels at compute time — it never assumes a unified
    "global constraint" because Meta and Google legitimately disagree
    (Meta allows 9999-char descriptions; Google caps at 5000).

    Attributes
    ----------
    field:
        Logical field name. See ``extract_field()`` for the mapping
        to Product columns / JSONB paths.
    required:
        When True a missing/empty value blocks readiness (``error``
        state). When False a missing value is a ``warn``.
    min_length / max_length:
        Inclusive bounds on string length. ``None`` means unbounded.
    allowed_values:
        Closed set for enum-like fields (``availability`` / ``condition``).
    regex:
        Pattern the value must match in full. Used for ISO-4217
        currency codes etc.
    soft_warn_at_pct:
        Fraction of ``max_length`` at which the UI flips to amber.
        Default 0.85 = warn the merchant once they're past 85%.
    label_ar:
        Short Arabic name for the field — used in error messages
        so the merchant sees "العنوان" instead of "title".
    rationale_ar:
        Optional one-line Arabic explainer rendered as a tooltip on
        the live counter / warning. Helps the merchant understand
        WHY a limit exists.
    """
    field:            str
    required:         bool
    min_length:       Optional[int] = None
    max_length:       Optional[int] = None
    allowed_values:   Optional[Tuple[str, ...]] = None
    regex:            Optional[str] = None
    soft_warn_at_pct: float = 0.85
    label_ar:         str = ""
    rationale_ar:     str = ""


@dataclass(frozen=True)
class ChannelSpec:
    """The full constraint surface for one output channel.

    Iterating a spec gives every ``FieldConstraint`` it cares about.
    The readiness engine + the UI's live-limit resolver are the only
    two consumers — keep this DTO small and JSON-friendly.
    """
    channel:        str
    label_ar:       str
    icon_key:       str
    enabled:        bool                  # False = "planned" / readiness-only
    fields:         Tuple[FieldConstraint, ...]
    image_required: bool = False
    description_ar: str = ""

    def get(self, field_name: str) -> Optional[FieldConstraint]:
        for fc in self.fields:
            if fc.field == field_name:
                return fc
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Field-value resolver — single place that maps "logical field name" →
# concrete value off a Product / dict. When Phase 2 promotes JSONB
# columns to top-level, ONLY the body of this function changes.
# ─────────────────────────────────────────────────────────────────────────────

def extract_field(product: Any, field_name: str) -> Any:
    """Look up *field_name* on *product*.

    Handles three shapes uniformly:

    * SQLAlchemy ``Product`` instances — top-level columns +
      ``extra_metadata`` JSONB.
    * Plain dicts — the same shape the readiness preview endpoint
      receives for unsaved drafts.
    * ``SimpleNamespace`` (test fixtures).

    Resolution order per field:

    * Top-level column FIRST (post-migration 0063 names).
    * Then ``extra_metadata.<field>`` (Phase 1 / dual-write window).
    * Then a few field-specific aliases (e.g. ``image_url`` falls
      back to ``thumbnail`` which is what the Salla writer used to
      stamp before we standardised on ``image_url``).

    Returns the raw value (string / number / list / None). The
    readiness engine handles type coercion + normalisation; we
    don't pre-process here so the value matches what the merchant
    actually has in storage.
    """
    if product is None:
        return None

    # Accept both attribute and dict access.
    def _attr(name: str) -> Any:
        if isinstance(product, Mapping):
            return product.get(name)
        return getattr(product, name, None)

    # Direct top-level columns (Phase 2 schema promotes these to
    # real Postgres columns; today they're attribute-on-Product
    # for the resolver to pick up after migration without code change).
    if field_name in {
        "title", "description", "price", "sku", "external_id",
        "meta_retailer_id", "in_stock", "stock_quantity", "source",
        # Phase 2 columns — read OK even when NULL on legacy rows.
        "image_url", "product_url", "sale_price", "currency",
        "availability", "brand", "category", "condition",
        "gtin", "mpn", "item_group_id", "meta_content_id",
        "google_offer_id",
    }:
        v = _attr(field_name)
        if v not in (None, ""):
            return v
        # Fall through to JSONB lookup below for Phase 1 rows.

    # JSONB fallback — meta blob is the only nested store in Phase 1.
    meta = _attr("extra_metadata")
    if meta is None and isinstance(product, Mapping):
        meta = product.get("metadata")
    if isinstance(meta, Mapping):
        # Special-case ``image_url``: also accept ``thumbnail`` as the
        # Salla writer historically stamped there before we standardised.
        if field_name == "image_url":
            return meta.get("image_url") or meta.get("thumbnail") or _attr("image_url")
        if field_name == "product_url":
            return meta.get("product_url") or meta.get("url") or _attr("product_url")
        if field_name == "additional_images":
            return meta.get("additional_images") or _attr("additional_images")
        # Generic — pull from JSONB then fall back to column.
        v = meta.get(field_name)
        if v not in (None, ""):
            return v

    # Synthetic field — effective retailer id (override → external_id).
    if field_name == "retailer_id":
        from core.catalog import effective_retailer_id  # noqa: PLC0415
        eff = effective_retailer_id(product)
        return eff or None

    # Last resort — attribute access (covers anything we missed).
    if field_name == "availability":
        return _availability_from_in_stock(product)
    return _attr(field_name)


def _availability_from_in_stock(product: Any) -> str:
    """Infer Meta-style availability when no explicit value is stored.

    Matches ``meta_catalog_export`` / Meta payload mapping:
    ``in_stock`` True → ``"in stock"``, False → ``"out of stock"``.
  """
    if isinstance(product, Mapping):
        raw = product.get("in_stock")
    else:
        raw = getattr(product, "in_stock", True)
    if raw is None:
        raw = True
    return "in stock" if bool(raw) else "out of stock"


# ─────────────────────────────────────────────────────────────────────────────
# Specs — Meta Catalog
# ─────────────────────────────────────────────────────────────────────────────

# Meta Catalog title limit per public docs is 200. We surface that
# verbatim — the live counter in the UI shows "X/200" exactly like
# Meta Commerce Manager does so merchants who've seen Commerce Manager
# recognise the number instantly.
META_CATALOG_SPEC = ChannelSpec(
    channel        = CHANNEL_META_CATALOG,
    label_ar       = "Meta Catalog",
    icon_key       = "meta",
    enabled        = True,
    image_required = True,
    description_ar = "كرت المنتج الرسمي عبر واتساب الأعمال / Commerce Manager",
    fields=(
        FieldConstraint(
            field="title", required=True,
            min_length=1, max_length=200, soft_warn_at_pct=0.85,
            label_ar="العنوان",
            rationale_ar="Meta يحدّ العنوان بـ 200 حرف. يُفضّل البقاء دون 150 للعرض المتسق على iPhone.",
        ),
        FieldConstraint(
            field="description", required=True,
            min_length=1, max_length=9999, soft_warn_at_pct=0.85,
            label_ar="الوصف",
            rationale_ar="Meta يدعم حتى 9999 حرف. الأوصاف القصيرة (تحت 300) تُظهر معدّل تفاعل أعلى.",
        ),
        FieldConstraint(
            field="price", required=True,
            label_ar="السعر",
            rationale_ar="السعر مطلوب لعرض كرت المنتج. يُحفظ كنص ليدعم تنسيقات العملات المختلفة.",
        ),
        FieldConstraint(
            field="currency", required=True,
            regex=r"^[A-Z]{3}$",
            label_ar="العملة",
            rationale_ar="كود ISO-4217 من ثلاثة أحرف كبيرة (مثل SAR / AED / USD).",
        ),
        FieldConstraint(
            field="image_url", required=True,
            label_ar="الصورة الرئيسية",
            rationale_ar="Meta يرفض المنتجات بدون صورة. الحد الأدنى الموصى به 500×500 بكسل.",
        ),
        FieldConstraint(
            field="product_url", required=True,
            label_ar="رابط المنتج",
            rationale_ar="مطلوب ليتم توجيه العميل من كرت المنتج إلى المتجر.",
        ),
        FieldConstraint(
            field="availability", required=True,
            allowed_values=("in stock", "out of stock", "preorder", "available for order", "discontinued"),
            label_ar="حالة التوفر",
            rationale_ar="قيم معتمدة من Meta. يُستنتج من ``in_stock`` تلقائيًا.",
        ),
        FieldConstraint(
            field="retailer_id", required=True,
            max_length=100,
            label_ar="معرّف retailer_id",
            rationale_ar="المعرّف الفريد للمنتج داخل كتالوج Meta.",
        ),
        FieldConstraint(
            field="condition", required=False,
            allowed_values=("new", "used", "refurbished"),
            label_ar="الحالة",
            rationale_ar="اختياري لـ Meta. ``new`` افتراضيًا للتجار الذين لا يحددون.",
        ),
        FieldConstraint(
            field="brand", required=False,
            max_length=100,
            label_ar="العلامة التجارية",
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Specs — WhatsApp product messages
# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp interactive product messages render from the linked Meta
# Catalog — so the spec is a SUBSET of Meta's. We surface it as a
# separate spec so the readiness UI can answer the merchant's actual
# question ("can I send THIS row over WhatsApp today?") with one badge
# rather than asking them to mentally subset Meta's requirements.

WHATSAPP_SPEC = ChannelSpec(
    channel        = CHANNEL_WHATSAPP,
    label_ar       = "WhatsApp",
    icon_key       = "whatsapp",
    enabled        = True,
    image_required = True,
    description_ar = "كرت المنتج المرسل عبر واتساب الأعمال (يستخدم Meta Catalog كقاعدة).",
    fields=(
        FieldConstraint(
            field="title", required=True,
            min_length=1, max_length=200,
            label_ar="العنوان",
            rationale_ar="نفس حد Meta. عناوين تحت 60 حرف تظهر في سطر واحد على شاشات الهواتف الصغيرة.",
        ),
        FieldConstraint(
            field="description", required=False,
            max_length=1024,
            label_ar="الوصف",
            rationale_ar="وصف الكرت في واتساب يُقتطع بعد 1024 حرف. أبقه قصيرًا لتجربة قراءة أفضل.",
        ),
        FieldConstraint(
            field="price", required=True, label_ar="السعر",
        ),
        FieldConstraint(
            field="image_url", required=True,
            label_ar="الصورة",
            rationale_ar="واتساب يرفض إرسال كرت بدون صورة من الكتالوج.",
        ),
        FieldConstraint(
            field="retailer_id", required=True,
            label_ar="retailer_id",
            rationale_ar="واتساب يرسل المنتج عبر هذا المعرّف لمطابقته في Meta Catalog.",
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Specs — AI (the WhatsApp brain product_resolver)
# ─────────────────────────────────────────────────────────────────────────────
# The AI reads from Nahla Catalog only (cemented contract). The
# "readiness for AI" question reduces to: "does this row have enough
# data for the AI to surface it as a recommendation?". That's a
# softer bar than Meta — title + searchable description is enough.

AI_SPEC = ChannelSpec(
    channel        = CHANNEL_AI,
    label_ar       = "الذكاء (AI)",
    icon_key       = "ai",
    enabled        = True,
    image_required = False,
    description_ar = "الذكاء يقرأ من كتالوج نحلة فقط. هذا الحد الأدنى ليُقترح المنتج تلقائيًا.",
    fields=(
        FieldConstraint(
            field="title", required=True,
            min_length=1, max_length=200,
            label_ar="العنوان",
            rationale_ar="الذكاء يستخدم العنوان للبحث بـ FTS عند طلب العميل.",
        ),
        FieldConstraint(
            field="description", required=False,
            min_length=10,
            label_ar="الوصف",
            rationale_ar="وصف أطول من 10 أحرف يحسّن مطابقة الذكاء لاستفسارات العميل.",
        ),
        FieldConstraint(
            field="price", required=False, label_ar="السعر",
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Specs — Campaigns
# ─────────────────────────────────────────────────────────────────────────────
# Campaign dispatcher reads catalog rows to build product-spotlight
# templates. Without an image the campaign renders as a plain text
# message — still works, but defeats the purpose.

CAMPAIGNS_SPEC = ChannelSpec(
    channel        = CHANNEL_CAMPAIGNS,
    label_ar       = "الحملات",
    icon_key       = "campaigns",
    enabled        = True,
    image_required = False,
    description_ar = "المنتج جاهز للاستخدام في حملات التسويق الموجّهة.",
    fields=(
        FieldConstraint(
            field="title", required=True, min_length=1, max_length=200,
            label_ar="العنوان",
        ),
        FieldConstraint(
            field="image_url", required=False, label_ar="الصورة",
            rationale_ar="بدون صورة، الحملة ترسل نصًا فقط — يقل معدّل النقرة.",
        ),
        FieldConstraint(
            field="product_url", required=False, label_ar="رابط المنتج",
        ),
        FieldConstraint(
            field="price", required=False, label_ar="السعر",
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Specs — Google Merchant (Phase 1: readiness-only, no publish actions)
# ─────────────────────────────────────────────────────────────────────────────
# We register Google with ``enabled=False`` so the UI knows to render
# the badge as "قريباً" — no publish button. But the constraints are
# REAL so merchants can start preparing their catalog for Google
# while the publish path is being built. By Phase 4 (Google publish),
# the merchant should already see green badges; we just flip
# ``enabled=True`` and add the export job.

GOOGLE_MERCHANT_SPEC = ChannelSpec(
    channel        = CHANNEL_GOOGLE_MERCHANT,
    label_ar       = "Google Merchant",
    icon_key       = "google",
    enabled        = False,
    image_required = True,
    description_ar = "متطلبات Google Shopping. النشر إلى Google قادم في مرحلة لاحقة.",
    fields=(
        FieldConstraint(
            field="title", required=True,
            min_length=1, max_length=150,
            label_ar="العنوان",
            rationale_ar="Google Shopping يحدّ بـ 150 حرف. عناوين أقصر من 70 ترفع جودة الإعلان.",
        ),
        FieldConstraint(
            field="description", required=True,
            min_length=1, max_length=5000,
            label_ar="الوصف",
            rationale_ar="Google يحدّ بـ 5000 حرف. وصف بين 500 و 1000 حرف هو نقطة الذهبية.",
        ),
        FieldConstraint(
            field="image_url", required=True,
            label_ar="الصورة الرئيسية",
            rationale_ar="الحد الأدنى 100×100 لـ Google، يُفضّل 800×800 أو أعلى.",
        ),
        FieldConstraint(
            field="product_url", required=True,
            label_ar="رابط المنتج",
            rationale_ar="يجب أن يكون رابطًا مباشرًا (HTTPS) لصفحة المنتج.",
        ),
        FieldConstraint(
            field="price", required=True,
            label_ar="السعر",
            rationale_ar="Google يتطلب السعر مع كود العملة.",
        ),
        FieldConstraint(
            field="currency", required=True,
            regex=r"^[A-Z]{3}$",
            label_ar="العملة",
        ),
        FieldConstraint(
            field="availability", required=True,
            allowed_values=("in stock", "out of stock", "preorder", "backorder"),
            label_ar="حالة التوفر",
        ),
        FieldConstraint(
            field="brand", required=True,
            max_length=70,
            label_ar="العلامة التجارية",
            rationale_ar="Google يتطلب العلامة التجارية للسماح بالظهور في Shopping Ads.",
        ),
        FieldConstraint(
            field="category", required=True,
            label_ar="التصنيف",
            rationale_ar="تصنيف Google Taxonomy (يُختار من قائمة جوجل الرسمية).",
        ),
        FieldConstraint(
            field="condition", required=True,
            allowed_values=("new", "used", "refurbished"),
            label_ar="الحالة",
        ),
        FieldConstraint(
            field="gtin", required=False,
            label_ar="GTIN",
            rationale_ar="رمز المنتج العالمي (UPC/EAN/ISBN). اختياري لكن يرفع جودة الإعلان.",
        ),
        FieldConstraint(
            field="mpn", required=False, label_ar="MPN",
        ),
        FieldConstraint(
            field="retailer_id", required=True, max_length=100,
            label_ar="معرّف العنصر",
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Registry — public API
# ─────────────────────────────────────────────────────────────────────────────

# Mutable on purpose — tests can ``register_channel`` to override
# a spec, and the implementation can flip a channel from
# ``enabled=False`` to ``enabled=True`` at runtime when its publish
# pipeline lands.
_REGISTRY: Dict[str, ChannelSpec] = {}


def register_channel(spec: ChannelSpec) -> None:
    """Add or replace *spec* in the registry. Idempotent."""
    _REGISTRY[spec.channel] = spec


def get_spec(channel: str) -> Optional[ChannelSpec]:
    return _REGISTRY.get(channel)


def all_specs(*, enabled_only: bool = False) -> Tuple[ChannelSpec, ...]:
    """Snapshot of every registered spec. Ordered as registered."""
    specs = tuple(_REGISTRY.values())
    if enabled_only:
        return tuple(s for s in specs if s.enabled)
    return specs


def strictest_max_length(field_name: str) -> Optional[int]:
    """Across every ENABLED channel, return the smallest ``max_length``
    set for *field_name* — or None if no enabled channel constrains it.

    Used by the live-counter UI: the counter for ``title`` shows
    ``"X / strictest_max_length('title')"`` so the merchant sees the
    tightest applicable limit, not the loosest. We deliberately
    EXCLUDE disabled channels (Google in Phase 1) — surfacing
    Google's 150-char title cap as the strictest would confuse
    merchants who haven't opted into Google yet.
    """
    candidates = []
    for spec in _REGISTRY.values():
        if not spec.enabled:
            continue
        fc = spec.get(field_name)
        if fc and fc.max_length is not None:
            candidates.append(fc.max_length)
    if not candidates:
        return None
    return min(candidates)


# Eagerly populate the registry on module import. Order matters only
# for ``all_specs()`` iteration — the UI renders badges in this order.
register_channel(WHATSAPP_SPEC)
register_channel(META_CATALOG_SPEC)
register_channel(AI_SPEC)
register_channel(CAMPAIGNS_SPEC)
register_channel(GOOGLE_MERCHANT_SPEC)


__all__ = [
    "AI_SPEC",
    "CAMPAIGNS_SPEC",
    "CHANNEL_AI",
    "CHANNEL_CAMPAIGNS",
    "CHANNEL_GOOGLE_MERCHANT",
    "CHANNEL_META_CATALOG",
    "CHANNEL_WHATSAPP",
    "ChannelSpec",
    "FieldConstraint",
    "GOOGLE_MERCHANT_SPEC",
    "META_CATALOG_SPEC",
    "WHATSAPP_SPEC",
    "all_specs",
    "extract_field",
    "get_spec",
    "register_channel",
    "strictest_max_length",
]
