"""
tests/test_knowledge_phase1.py
──────────────────────────────
Smart Store Knowledge Hub — Phase 1 regression tests.

Covers the parts that don't need a live database / HTTP layer:

1. The ``services/knowledge_section_kinds`` registry: every kind in
   the canonical list maps back to a valid group (1..6), and the
   lookup helpers do what they say.

2. The legacy-text splitter (``_split_legacy_text``): well-formed
   Arabic-headed text comes out as one section per heading; free-form
   text falls back to a single ``custom`` block.

3. The structured facts overlay (``build_structured_facts_block``):
   builds correctly from a fake session that returns
   ``MerchantKnowledgeSection`` rows; renders ``[MEDIA_KEY:<slug>]``
   markers for linked media that has a ``media_key``; emits the
   Salla-precedence note unconditionally; group 6 (linked media
   library) is intentionally skipped.

4. The full overlay split path (``build_tenant_overlay_split``):
   prefers structured facts when ``db``+``tenant_id`` resolve to
   non-empty content, otherwise falls back to the legacy
   ``manual_knowledge_base`` text. This pins the source-of-truth
   precedence rule alongside the existing tests in
   ``test_merchant_kb_scope``.

5. The FastAPI router wiring: ``main.py`` must include the new
   ``_knowledge_router`` so a refactor cannot silently drop the
   ``/knowledge/*`` surface.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
_DB_ROOT = _BACKEND_ROOT.parent / "database"
if str(_DB_ROOT) not in sys.path:
    sys.path.insert(0, str(_DB_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Section-kinds registry
# ─────────────────────────────────────────────────────────────────────────────


def test_registry_every_kind_has_valid_group() -> None:
    from services.knowledge_section_kinds import all_kinds, GROUP_LABELS_AR

    for sk in all_kinds():
        assert sk.group in GROUP_LABELS_AR, (
            f"kind={sk.kind!r} has group={sk.group} which is not in GROUP_LABELS_AR"
        )
        assert sk.label_ar.strip(), f"kind={sk.kind!r} has empty label_ar"
        assert sk.placeholder_ar.strip(), f"kind={sk.kind!r} has empty placeholder"


def test_registry_kind_lookup_is_case_insensitive() -> None:
    from services.knowledge_section_kinds import is_valid_kind, get_kind

    assert is_valid_kind("payment_method")
    assert is_valid_kind("PAYMENT_METHOD")
    assert not is_valid_kind("not_a_real_kind")
    assert not is_valid_kind("")
    assert not is_valid_kind(None)

    sk = get_kind("RETURN_POLICY")
    assert sk is not None and sk.kind == "return_policy"


def test_registry_group_for_unknown_falls_back_to_store_info() -> None:
    """Phase-2 classifier may emit an unknown kind during a partial
    deploy. The overlay must not lose those rows — bucket 2 is the
    safe default."""
    from services.knowledge_section_kinds import group_for

    assert group_for("payment_method") == 3
    assert group_for("not_yet_in_registry") == 2


def test_registry_link_role_validation() -> None:
    from services.knowledge_section_kinds import is_valid_link_role

    assert is_valid_link_role("primary")
    assert is_valid_link_role("BARCODE")
    assert not is_valid_link_role("not_a_role")
    assert not is_valid_link_role(None)


def test_classifier_normalizer_marks_surface_examples_for_preservation() -> None:
    """Classifier cleanup must preserve real customer utterances.

    Even if the LLM forgets metadata, deterministic normalization adds
    ``preserve_surface_forms`` so later improvement layers group these
    phrases without compressing them into an abstract summary.
    """
    from modules.ai.knowledge.classifier import (
        AttachedMedia,
        PlatformSignal,
        _normalize_proposal,
    )
    from services.knowledge_section_kinds import all_kinds

    examples = [
        "أنا قريب",
        "أنا بالطريق",
        "أنا عند البوابة",
        "وين المعرض",
        "أرسل اللوكيشن",
    ]
    parsed = {
        "proposed_ops": [{
            "op_id": "op-1",
            "op": "create",
            "kind": "custom",
            "title": "صيغ الوصول",
            "body": "\n".join(f"- {e}" for e in examples),
            "metadata": {},
        }],
        "conflicts": [],
        "confidence": 0.8,
    }
    normalized = _normalize_proposal(
        parsed,
        available_kinds=[k.kind for k in all_kinds()],
        platform_signal=PlatformSignal(False, "", ""),
        attached_media=[],
    )
    op = normalized["proposed_ops"][0]
    assert op["body"].splitlines() == [f"- {e}" for e in examples]
    assert op["metadata"]["preserve_surface_forms"] is True
    assert op["metadata"]["knowledge_mode"] == "artifact_trigger_examples"
    assert op["metadata"]["examples_to_preserve"] == examples
    assert op["metadata"]["intent"] == "ask_location_or_arrival_help"
    assert op["metadata"]["artifact_target"] == "maps_link_or_staff_contact"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Legacy splitter
# ─────────────────────────────────────────────────────────────────────────────


def test_splitter_groups_under_known_headings() -> None:
    from routers.knowledge import _split_legacy_text

    blob = (
        "# الشحن\n"
        "نشحن بسمسا خلال 2-3 أيام عمل.\n"
        "الشحن المجاني للطلبات فوق 200 ريال.\n\n"
        "# الدفع\n"
        "نقبل مدى وفيزا والتحويل البنكي.\n\n"
        "## الضمان\n"
        "ضمان استبدال خلال 14 يوماً."
    )
    blocks = _split_legacy_text(blob)
    kinds = [b["kind"] for b in blocks]
    assert "shipping_zones" in kinds
    assert "payment_method" in kinds
    assert "warranty" in kinds
    # Body of "الشحن" block keeps both shipping lines together.
    shipping = next(b for b in blocks if b["kind"] == "shipping_zones")
    assert "سمسا" in shipping["body"]
    assert "200 ريال" in shipping["body"]


def test_splitter_falls_back_to_custom_for_unstructured_text() -> None:
    from routers.knowledge import _split_legacy_text

    blob = "أهلاً بكم في متجرنا — نبيع العسل البلدي منذ 2010."
    blocks = _split_legacy_text(blob)
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "custom"
    assert "العسل البلدي" in blocks[0]["body"]


def test_splitter_handles_empty_text() -> None:
    from routers.knowledge import _split_legacy_text

    assert _split_legacy_text("") == []
    assert _split_legacy_text("   \n\n") == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. Structured facts overlay — driven by fake rows
# ─────────────────────────────────────────────────────────────────────────────


class _FakeMediaItem:
    """Minimal stand-in for AIMediaItem inside ``media_links``."""

    def __init__(self, media_key: str | None, *, is_active: bool = True) -> None:
        self.media_key = media_key
        self.is_active = is_active


class _FakeLink:
    def __init__(self, media: _FakeMediaItem) -> None:
        self.media = media


class _FakeSection:
    def __init__(
        self,
        *,
        kind: str,
        title: str,
        body: str,
        media_links: List[_FakeLink] | None = None,
        priority: int = 100,
    ) -> None:
        self.kind = kind
        self.title = title
        self.body = body
        self.media_links = media_links or []
        self.priority = priority
        from datetime import datetime, timezone
        self.updated_at = datetime.now(timezone.utc)


class _FakeQuery:
    def __init__(self, rows: List[_FakeSection]) -> None:
        self._rows = rows

    def filter(self, *args: Any, **kwargs: Any) -> "_FakeQuery":
        return self

    def order_by(self, *args: Any) -> "_FakeQuery":
        return self

    def all(self) -> List[_FakeSection]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: List[_FakeSection]) -> None:
        self._rows = rows

    def query(self, model: Any) -> _FakeQuery:
        return _FakeQuery(self._rows)


def test_structured_facts_block_renders_groups_and_media_markers() -> None:
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(
            kind="store_story",
            title="قصتنا",
            body="نشتري العسل مباشرة من المناحل.",
        ),
        _FakeSection(
            kind="payment_method",
            title="طرق الدفع",
            body="مدى وفيزا والتحويل البنكي.",
            media_links=[_FakeLink(_FakeMediaItem("payment_rajhi_barcode"))],
        ),
        _FakeSection(
            kind="shipping_zones",
            title="مناطق الشحن",
            body="نشحن لكل مدن المملكة.",
        ),
    ]
    block = build_structured_facts_block(_FakeSession(rows), tenant_id=42)

    # Heading + body must be present.
    assert "قاعدة المعرفة (معلومات المتجر — Facts فقط)" in block
    assert "## معلومات المتجر" in block
    assert "## سياسات البيع" in block
    assert "## سياسات الشحن" in block
    assert "قصتنا" in block
    assert "[MEDIA_KEY:payment_rajhi_barcode]" in block
    # The Salla-precedence note is unconditional.
    assert "المصدر الرسمي" in block


def test_structured_facts_block_skips_inactive_media() -> None:
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(
            kind="payment_method",
            title="طرق الدفع",
            body="مدى وفيزا.",
            media_links=[
                _FakeLink(_FakeMediaItem("payment_rajhi_barcode", is_active=False)),
            ],
        ),
    ]
    block = build_structured_facts_block(_FakeSession(rows), tenant_id=1)
    assert "MEDIA_KEY" not in block


def test_structured_facts_block_empty_when_no_rows() -> None:
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    assert build_structured_facts_block(_FakeSession([]), tenant_id=1) == ""


# ─────────────────────────────────────────────────────────────────────────────
# 4. Overlay split — structured vs legacy fallback
# ─────────────────────────────────────────────────────────────────────────────


def test_overlay_split_prefers_structured_facts_when_db_provided() -> None:
    from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split

    rows = [
        _FakeSection(
            kind="warranty",
            title="الضمان",
            body="ضمان 14 يوماً للاستبدال.",
        ),
    ]
    settings = {
        "manual_knowledge_base": "ملاحظة قديمة من حقل النص الحر.",
    }
    buckets = build_tenant_overlay_split(
        settings, db=_FakeSession(rows), tenant_id=99,
    )
    assert "ضمان 14 يوماً" in buckets["facts"]
    # The legacy free-form text must NOT be in the structured output
    # — that's the whole point of preferring structured rows.
    assert "ملاحظة قديمة" not in buckets["facts"]


def test_overlay_split_falls_back_to_legacy_when_no_structured_rows() -> None:
    from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split

    settings = {
        "manual_knowledge_base": "كيلو السدر بسعر 200 ريال.",
    }
    buckets = build_tenant_overlay_split(
        settings, db=_FakeSession([]), tenant_id=99,
    )
    assert "كيلو السدر" in buckets["facts"]


def test_overlay_split_without_db_uses_legacy_path() -> None:
    """The IO-free callers (e.g. brain prompt_builder before the pipeline
    pre-bakes the structured block) must still work — they take the
    legacy free-form path."""
    from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split

    settings = {"manual_knowledge_base": "محتوى قديم"}
    buckets = build_tenant_overlay_split(settings)
    assert "محتوى قديم" in buckets["facts"]


# ─────────────────────────────────────────────────────────────────────────────
# 5. Router wiring
# ─────────────────────────────────────────────────────────────────────────────


def test_main_module_registers_knowledge_router() -> None:
    """Pin that ``main.py`` imports and includes the new router so a
    future include_router refactor cannot silently drop /knowledge/*."""
    main_src = (_BACKEND_ROOT / "main.py").read_text(encoding="utf-8")
    assert "from routers.knowledge" in main_src
    assert "_knowledge_router" in main_src
    assert "app.include_router(_knowledge_router)" in main_src


def test_knowledge_router_exposes_expected_paths() -> None:
    """The dashboard hard-codes these paths — if they shift the UI
    breaks silently."""
    from routers.knowledge import router

    paths = {r.path for r in router.routes}
    expected = {
        # Phase 1
        "/knowledge/section-kinds",
        "/knowledge/sections",
        "/knowledge/sections/search",
        "/knowledge/sections/{section_id}",
        "/knowledge/sections/{section_id}/toggle",
        "/knowledge/sections/{section_id}/media",
        "/knowledge/sections/{section_id}/media/{link_id}",
        "/knowledge/sections/migrate-from-legacy",
        "/knowledge/legacy-knowledge-base",
        # Phase 2
        "/knowledge/quick-update/format",
        "/knowledge/drafts",
        "/knowledge/drafts/{draft_id}/approve",
        "/knowledge/drafts/{draft_id}/reject",
    }
    missing = expected - paths
    assert not missing, f"knowledge router missing paths: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Phase 2 — classifier deterministic fallback
# ─────────────────────────────────────────────────────────────────────────────


def test_classifier_falls_back_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without OPENAI_API_KEY the classifier MUST degrade to a
    single ``quick_update`` create-op so the merchant never sees a
    hard error from the AI button."""
    from modules.ai.knowledge import classifier as kbc

    monkeypatch.setattr(kbc, "_API_KEY", "")

    result = kbc.classify_quick_update(
        raw_text="السبت إجازة هذا الأسبوع",
        attached_media=[],
        existing_sections=[],
        platform_signal=kbc.PlatformSignal(
            connected=False, platform=None, warning="",
        ),
        available_kinds=["quick_update", "store_hours", "custom"],
    )

    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "no_api_key"
    ops = result["proposed_ops"]
    assert len(ops) == 1
    assert ops[0]["kind"] == "quick_update"
    assert ops[0]["op"] == "create"
    assert "السبت" in ops[0]["body"]
    assert result["conflicts"] == []


def test_classifier_fallback_attaches_media_as_link_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the merchant attached media but classification failed, the
    fallback still surfaces the media as ``link_media`` ops so they
    aren't silently dropped."""
    from modules.ai.knowledge import classifier as kbc

    monkeypatch.setattr(kbc, "_API_KEY", "")

    result = kbc.classify_quick_update(
        raw_text="باركود الراجحي",
        attached_media=[
            kbc.AttachedMedia(
                id=42, title="باركود الراجحي",
                media_type="image", media_key="payment_rajhi_barcode",
            ),
        ],
        existing_sections=[],
        platform_signal=kbc.PlatformSignal(
            connected=False, platform=None, warning="",
        ),
        available_kinds=["quick_update"],
    )

    link_ops = [op for op in result["proposed_ops"] if op["op"] == "link_media"]
    assert len(link_ops) == 1
    assert link_ops[0]["media_id"] == 42
    assert link_ops[0]["link_role"] == "primary"


def test_classifier_rejects_hallucinated_media_ids() -> None:
    """The model could invent a media_id that the merchant never
    attached. ``_normalize_proposal`` MUST scrub those references —
    otherwise the apply loop in the endpoint would skip the link op
    but the dashboard would still show the proposal in the preview."""
    from modules.ai.knowledge import classifier as kbc

    proposal = {
        "proposed_ops": [
            {
                "op_id": "op-1",
                "op": "link_media",
                "kind": "payment_method",
                "title": None,
                "body": "",
                "metadata": {},
                "target_section_id": 7,
                "link_role": "barcode",
                "media_id": 999,  # never attached
                "rationale": "ربط الباركود",
            },
        ],
        "conflicts": [],
        "confidence": 0.9,
    }
    normalized = kbc._normalize_proposal(
        proposal,
        available_kinds=["payment_method"],
        platform_signal=kbc.PlatformSignal(
            connected=False, platform=None, warning="",
        ),
        attached_media=[
            kbc.AttachedMedia(id=1, title="x", media_type="image", media_key=None),
        ],
    )
    assert normalized["proposed_ops"][0]["media_id"] is None


def test_classifier_post_pends_platform_conflict_when_price_mentioned() -> None:
    from modules.ai.knowledge import classifier as kbc

    proposal = {
        "proposed_ops": [
            {
                "op_id": "op-1",
                "op": "update",
                "kind": "custom",
                "title": "سعر منتج",
                "body": "كيلو السدر بسعر 200 ريال.",
                "metadata": {},
                "target_section_id": None,
                "link_role": None,
                "media_id": None,
                "rationale": "تحديث السعر",
            },
        ],
        "conflicts": [],  # Model forgot to flag — we add it.
        "confidence": 0.8,
    }
    normalized = kbc._normalize_proposal(
        proposal,
        available_kinds=["custom"],
        platform_signal=kbc.PlatformSignal(
            connected=True, platform="salla",
            warning="سعر سلة هو المصدر.",
        ),
        attached_media=[],
    )
    kinds = {c["kind"] for c in normalized["conflicts"]}
    assert "platform_price" in kinds, normalized["conflicts"]


# ─────────────────────────────────────────────────────────────────────────────
# 7. Phase 3 — product-scoped sections
# ─────────────────────────────────────────────────────────────────────────────


class _FakeProductLink:
    def __init__(self, product_id: int) -> None:
        self.product_id = product_id


def test_structured_facts_includes_global_section_unconditionally() -> None:
    """A section with NO product_links must always render — that's
    what makes it a "global" policy."""
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(
            kind="return_policy", title="الإرجاع", body="14 يوماً.",
        ),
    ]
    # No hint at all → render.
    out_no_hint = build_structured_facts_block(_FakeSession(rows), tenant_id=1)
    assert "14 يوماً" in out_no_hint
    # Empty hint (resolver says "no product in context") → still render
    # globals; only product-scoped ones drop.
    out_empty_hint = build_structured_facts_block(
        _FakeSession(rows), tenant_id=1, active_product_ids=set(),
    )
    assert "14 يوماً" in out_empty_hint


def test_structured_facts_filters_product_scoped_when_hint_present() -> None:
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(
            kind="usage_tips", title="استخدام السدر",
            body="ملعقة قبل النوم.",
        ),
    ]
    rows[0].product_links = [_FakeProductLink(7)]  # scoped to product 7

    # Hint matches → render.
    out_match = build_structured_facts_block(
        _FakeSession(rows), tenant_id=1, active_product_ids={7},
    )
    assert "ملعقة قبل النوم" in out_match

    # Hint doesn't match → drop product-scoped section.
    out_miss = build_structured_facts_block(
        _FakeSession(rows), tenant_id=1, active_product_ids={99},
    )
    assert "ملعقة قبل النوم" not in out_miss

    # No hint at all (Phase-3 day-1 fallback) → keep everything.
    out_nohint = build_structured_facts_block(_FakeSession(rows), tenant_id=1)
    assert "ملعقة قبل النوم" in out_nohint


def test_product_matcher_normalises_arabic_diacritics_and_alef() -> None:
    from modules.ai.knowledge.product_matcher import (
        CatalogProductForMatch, match_products, normalize_arabic,
    )

    # Sanity on the normaliser itself.
    assert normalize_arabic("أهلاً") == normalize_arabic("اهلا")
    assert normalize_arabic("السِدر") == normalize_arabic("السدر")

    catalog = [
        CatalogProductForMatch(id=1, title="عسل السدر البلدي", sku=None, external_id=None),
        CatalogProductForMatch(id=2, title="عسل الطلح", sku=None, external_id=None),
        CatalogProductForMatch(id=3, title="شمع طبيعي", sku=None, external_id=None),
    ]
    matches = match_products(
        "نصيحة: استخدم عسل السدر قبل النوم بساعة.", catalog,
    )
    # Sidr should win, طلح / شمع must NOT appear (no token overlap).
    assert [m.product_id for m in matches] == [1]
    assert matches[0].confidence >= 0.5


def test_product_matcher_skips_when_no_token_overlap() -> None:
    from modules.ai.knowledge.product_matcher import (
        CatalogProductForMatch, match_products,
    )

    catalog = [
        CatalogProductForMatch(id=1, title="عسل السدر", sku=None, external_id=None),
    ]
    assert match_products("ساعات العمل من 9 إلى 6.", catalog) == []


def test_product_matcher_boosts_sku_exact_match() -> None:
    from modules.ai.knowledge.product_matcher import (
        CatalogProductForMatch, match_products,
    )

    catalog = [
        CatalogProductForMatch(
            id=42, title="منتج بعنوان طويل لا يتطابق نصياً",
            sku="SDR-100", external_id=None,
        ),
    ]
    matches = match_products("تحقق من SDR-100 المخزون.", catalog)
    assert matches and matches[0].product_id == 42
    assert matches[0].confidence >= 0.9


def test_phase3_router_paths_registered() -> None:
    from routers.knowledge import router

    paths = {r.path for r in router.routes}
    expected = {
        "/knowledge/sections/{section_id}/products",
        "/knowledge/sections/{section_id}/products/{link_id}",
        "/knowledge/products/search",
    }
    missing = expected - paths
    assert not missing, f"phase 3 router paths missing: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Phase 4 — runtime overlay invariants
# ─────────────────────────────────────────────────────────────────────────────


def test_phase4_facts_block_numbers_sections_inside_group() -> None:
    """Each section inside a group must be numbered (``### 1. ...``,
    ``### 2. ...``) so Claude can cite them deterministically."""
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(kind="store_story", title="القصة", body="نحن نبيع منذ 2010."),
        _FakeSection(kind="reply_style", title="النبرة", body="ودودة وقصيرة."),
    ]
    block = build_structured_facts_block(_FakeSession(rows), tenant_id=1)
    # Both numbered headers must be present.
    assert "### 1. القصة" in block
    assert "### 2. النبرة" in block


def test_phase4_facts_block_surfaces_product_scope_inline() -> None:
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(kind="usage_tips", title="استخدام السدر", body="ملعقة قبل النوم."),
    ]
    rows[0].product_links = [_FakeProductLink(7), _FakeProductLink(12)]

    block = build_structured_facts_block(_FakeSession(rows), tenant_id=1)
    # The scope hint must appear on the heading line so the LLM
    # knows the section only applies to those products.
    assert "(منتجات: 7, 12)" in block


def test_phase4_facts_block_renders_media_markers_alongside_body() -> None:
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(
            kind="payment_method", title="طرق الدفع", body="مدى وفيزا.",
            media_links=[
                _FakeLink(_FakeMediaItem("payment_rajhi_barcode")),
                _FakeLink(_FakeMediaItem(None)),  # no media_key → skipped
                _FakeLink(_FakeMediaItem("payment_ahli_qr")),
            ],
        ),
    ]
    block = build_structured_facts_block(_FakeSession(rows), tenant_id=1)
    assert "[MEDIA_KEY:payment_rajhi_barcode]" in block
    assert "[MEDIA_KEY:payment_ahli_qr]" in block
    # The keyless media must be omitted (registry-only contract).
    assert block.count("MEDIA_KEY:") == 2


def test_high_priority_layer_pins_source_of_truth_precedence() -> None:
    """The Phase 4 source-of-truth rule must be in the FORBIDDEN block
    so the LLM treats it as non-overridable. The exact phrasing is
    intentionally pinned — silent removal would let merchant KB notes
    override platform price/stock again."""
    from modules.ai.prompts.high_priority_layer import (
        BASELINE_FORBIDDEN_RULES, build_high_priority_block,
    )

    joined = "\n".join(BASELINE_FORBIDDEN_RULES)
    assert "أولوية مصادر البيانات per-field" in joined
    assert "merchant_context.platform" in joined
    assert "merchant_knowledge_sections" in joined
    assert "[MEDIA_KEY:<slug>]" in joined

    # And it must render through into the final block.
    block = build_high_priority_block({})
    assert "أولوية مصادر البيانات per-field" in block
    assert "FORBIDDEN" in block


# ─────────────────────────────────────────────────────────────────────────────
# 9. Server-side approve guard (stabilization)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# 10. Stabilization scenarios — legacy/empty/idempotence
# ─────────────────────────────────────────────────────────────────────────────


def test_stab_legacy_tenant_path_uses_legacy_text_unchanged() -> None:
    """A merchant still on ``ai_settings.manual_knowledge_base`` (no
    structured rows yet) must keep getting their old free-form text in
    the facts bucket — exactly as before Phase 1. Pinned because this
    is the path 100% of production tenants are on at deploy time."""
    from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split

    settings = {"manual_knowledge_base": "نشحن خلال 2-3 أيام عمل."}
    buckets_no_db = build_tenant_overlay_split(settings)
    buckets_empty_rows = build_tenant_overlay_split(
        settings, db=_FakeSession([]), tenant_id=42,
    )
    assert "نشحن خلال 2-3 أيام عمل." in buckets_no_db["facts"]
    assert "نشحن خلال 2-3 أيام عمل." in buckets_empty_rows["facts"]


def test_stab_empty_tenant_path_renders_empty_facts_without_error() -> None:
    """A brand-new tenant has no rows AND no legacy text. The overlay
    must return an empty facts bucket cleanly (no exceptions, no
    placeholder noise that would confuse the LLM)."""
    from modules.ai.prompts.tenant_overlay import (
        build_structured_facts_block, build_tenant_overlay_split,
    )

    assert build_structured_facts_block(_FakeSession([]), tenant_id=99) == ""
    buckets = build_tenant_overlay_split(
        {}, db=_FakeSession([]), tenant_id=99,
    )
    assert buckets["facts"] == ""


def test_stab_migrate_idempotent_when_called_twice_via_clear_legacy() -> None:
    """The intended idempotency contract: clearing the legacy field on
    success means a second invocation (e.g. double-click recovered by
    a page reload) sees an empty ``manual_knowledge_base`` and
    creates ZERO new rows. The router checks ``if not text`` and
    short-circuits with ``{"created": 0}``."""
    import importlib
    src = importlib.import_module("routers.knowledge")
    # The text-empty short-circuit lives at the top of the function.
    # Pin its presence so a refactor cannot accidentally remove it.
    src_text = (_BACKEND_ROOT / "routers" / "knowledge.py").read_text(encoding="utf-8")
    assert "if not text:" in src_text
    # And the success path stashes the original under _kb_backup_v1.
    assert "_kb_backup_v1" in src_text


def test_stab_link_media_requires_tenant_match_on_both_section_and_media() -> None:
    """Pin the tenant-isolation contract on the media-linking endpoint
    by inspecting the function source — both ORM filters must include
    ``tenant_id == tenant_id``."""
    src_text = (_BACKEND_ROOT / "routers" / "knowledge.py").read_text(encoding="utf-8")
    # We can't run the endpoint without a DB, but we can pin the
    # filter expressions so a refactor would have to keep both.
    section_filter = "MerchantKnowledgeSection.tenant_id == tenant_id"
    media_filter = "AIMediaItem.tenant_id == tenant_id"
    product_filter = "Product.tenant_id == tenant_id"
    for needle in (section_filter, media_filter, product_filter):
        assert needle in src_text, f"missing tenant-isolation filter: {needle!r}"


def test_stab_global_section_not_dropped_by_product_filter() -> None:
    """The shipping / payment policies that apply to the whole store
    must never be hidden because the customer is asking about
    product X. Encoded in the overlay as: empty product_links →
    always include."""
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        # Global policy — no product_links.
        _FakeSection(
            kind="shipping_zones", title="مناطق الشحن",
            body="نشحن لكل المملكة.",
        ),
        # Global payment policy.
        _FakeSection(
            kind="payment_method", title="طرق الدفع",
            body="مدى وفيزا والتحويل.",
        ),
        # Product-scoped tip.
        _FakeSection(
            kind="usage_tips", title="استخدام السدر",
            body="ملعقة قبل النوم.",
        ),
    ]
    rows[-1].product_links = [_FakeProductLink(7)]

    # Customer is asking about a DIFFERENT product (id=99).
    block = build_structured_facts_block(
        _FakeSession(rows), tenant_id=1, active_product_ids={99},
    )
    # Global sections survive.
    assert "نشحن لكل المملكة" in block
    assert "مدى وفيزا" in block
    # Product-scoped section for the wrong product is dropped.
    assert "ملعقة قبل النوم" not in block


def test_stab_platform_precedence_pinned_in_facts_block_note() -> None:
    """The block always carries the precedence note so Claude cannot
    rationalise its way past it."""
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    rows = [
        _FakeSection(
            kind="payment_method", title="طرق الدفع",
            body="مدى وفيزا.",
        ),
    ]
    block = build_structured_facts_block(_FakeSession(rows), tenant_id=1)
    # The exact phrasing the high-priority layer relies on for the
    # cross-reference.
    assert "المصدر الرسمي" in block
    # And the "do not use price/stock from KB" rule.
    assert "السعر" in block and "المخزون" in block


def test_looks_like_platform_field_claim_detects_price_and_stock() -> None:
    """The shared body-claim sniffer must catch the merchant-price /
    stock phrasings the dashboard relies on for its tenant-wide
    conflict block. Used by both the classifier (to post-pend
    conflicts) and the approve endpoint (to block stale clients
    from sneaking those ops through)."""
    from modules.ai.knowledge.classifier import _looks_like_platform_field_claim

    assert _looks_like_platform_field_claim("كيلو السدر 200 ريال")
    assert _looks_like_platform_field_claim("Out of stock")
    assert _looks_like_platform_field_claim("منتج العسل اليمني نفد")
    assert _looks_like_platform_field_claim("price is 50 SAR today")
    assert not _looks_like_platform_field_claim("")
    assert not _looks_like_platform_field_claim("السبت إجازة")


def test_classifier_normalizes_invalid_kind_to_custom() -> None:
    from modules.ai.knowledge import classifier as kbc

    proposal = {
        "proposed_ops": [
            {
                "op_id": "op-1",
                "op": "create",
                "kind": "made_up_kind",
                "title": "x",
                "body": "x",
                "metadata": {},
                "target_section_id": None,
                "link_role": None,
                "media_id": None,
                "rationale": "",
            },
        ],
        "conflicts": [],
        "confidence": 0.5,
    }
    normalized = kbc._normalize_proposal(
        proposal,
        available_kinds=["payment_method", "custom"],
        platform_signal=kbc.PlatformSignal(
            connected=False, platform=None, warning="",
        ),
        attached_media=[],
    )
    assert normalized["proposed_ops"][0]["kind"] == "custom"
