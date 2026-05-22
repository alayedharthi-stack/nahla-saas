"""
tests/test_payment_media_autolink.py
────────────────────────────────────
Regression tests for the payment-media autolink layer.

Two pieces of behaviour are pinned here:

1. :func:`services.payment_media_autolink.detect_payment_media_key`
   — the pure inferrer that maps
   ``(section kind / title / body, media title, link role)`` →
   canonical registry key (or ``None`` on ambiguity / wrong gate).

2. :mod:`services.media_key_registry` trigger expansion (May 2026 #20)
   — the expanded trigger lists for Rajhi / Alahli / Barq / STC Pay /
   Mobily Pay must actually resolve via ``find_key_for_query`` for
   the realistic Saudi-customer phrasings the merchant told us about.

3. ``backend/routers/knowledge.py`` wiring — both the manual
   ``link_media`` endpoint and the AI ``_apply_op_to_db`` ``link_media``
   branch must call the autolink helper, otherwise the runtime safety
   net keeps missing the asset.

None of these tests touch a live database. The autolink module is a
pure function; the registry helpers are pure; and the router wiring
is verified by inspecting the source the same way the existing
tenant-isolation regression test does (see
``test_stab_link_media_requires_tenant_match_on_both_section_and_media``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services import media_key_registry as registry
from services.payment_media_autolink import (
    _BANK_KEY_MAP,
    _NORMALISED_PATTERNS,
    _VALID_PAYMENT_KINDS,
    detect_payment_media_key,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


# ── 1. Pure inferrer — happy paths ─────────────────────────────────────


def test_detect_rajhi_from_section_title_only():
    """Merchant linked an unnamed image into «التحويل البنكي — الراجحي»
    with role barcode → autobind to Rajhi key."""
    key = detect_payment_media_key(
        section_kind="bank_transfer",
        section_title="التحويل البنكي — الراجحي",
        section_body="",
        media_title="",
        link_role="barcode",
    )
    assert key == "payment_rajhi_barcode"


def test_detect_alahli_from_media_title_only():
    """Section is generic «طرق الدفع»; bank name lives in the media
    title — the inferrer must still hit because we combine all three
    text sources before matching."""
    key = detect_payment_media_key(
        section_kind="payment_method",
        section_title="طرق الدفع",
        section_body="نقبل عدة وسائل دفع.",
        media_title="Al Ahli QR — بنك الأهلي",
        link_role="barcode",
    )
    assert key == "payment_alahli_barcode"


def test_detect_barq_from_section_body():
    key = detect_payment_media_key(
        section_kind="payment_method",
        section_title="التحويل",
        section_body="نستقبل تحويل على حساب برق التابع لنا.",
        media_title="QR",
        link_role="barcode",
    )
    assert key == "payment_barq_barcode"


def test_detect_stcpay_from_combined_text():
    key = detect_payment_media_key(
        section_kind="payment_method",
        section_title="محفظة STC Pay",
        section_body="",
        media_title="",
        link_role="barcode",
    )
    assert key == "payment_stcpay_qr"


def test_detect_mobilypay_from_arabic_phrasing():
    key = detect_payment_media_key(
        section_kind="payment_method",
        section_title="موبايلي باي",
        section_body="",
        media_title="",
        link_role="barcode",
    )
    assert key == "payment_mobilypay_qr"


def test_detect_iban_only_when_no_specific_bank():
    """A generic «بيانات الآيبان» upload with no specific bank goes
    to the generic IBAN key. This is the only path that lands on
    ``payment_bank_transfer_image`` automatically."""
    key = detect_payment_media_key(
        section_kind="bank_transfer",
        section_title="بيانات الحساب البنكي",
        section_body="آيبان فقط — للعملاء اللي ما يقدرون يستخدمون QR.",
        media_title="IBAN screenshot",
        link_role="barcode",
    )
    assert key == "payment_bank_transfer_image"


def test_detect_prefers_specific_bank_over_iban_when_both_appear():
    """Merchant wrote «تحويل بنكي — الراجحي» which technically
    matches both the IBAN keyword («تحويل بنكي») and Rajhi.
    The autolink must prefer the specific bank — picking IBAN would
    send the wrong asset for «أبي باركود الراجحي»."""
    key = detect_payment_media_key(
        section_kind="bank_transfer",
        section_title="تحويل بنكي — الراجحي",
        section_body="آيبان الراجحي محفوظ في QR.",
        media_title="QR الراجحي",
        link_role="barcode",
    )
    assert key == "payment_rajhi_barcode"


# ── 2. Pure inferrer — gating + ambiguity ──────────────────────────────


def test_detect_returns_none_for_wrong_link_role():
    """Only ``role='barcode'`` qualifies. ``primary`` / ``evidence`` /
    ``policy_pdf`` etc. describe non-canonical assets we don't auto-
    bind to a payment slug."""
    for bad_role in ("primary", "evidence", "policy_pdf", "tutorial_video", ""):
        assert detect_payment_media_key(
            section_kind="bank_transfer",
            section_title="الراجحي",
            section_body="",
            media_title="",
            link_role=bad_role,
        ) is None, f"role={bad_role!r} must not auto-bind"


def test_detect_returns_none_for_wrong_section_kind():
    """Even a barcode-role image into a non-payment section must NOT
    auto-bind. A merchant attaching a Rajhi QR to «قصة المتجر» is
    almost certainly demoing the asset, not declaring a payment
    rail."""
    for bad_kind in ("store_story", "shipping_zone", "usage_tips", "custom", ""):
        assert detect_payment_media_key(
            section_kind=bad_kind,
            section_title="باركود الراجحي",
            section_body="",
            media_title="",
            link_role="barcode",
        ) is None, f"kind={bad_kind!r} must not auto-bind"


def test_detect_returns_none_when_no_bank_pattern_matches():
    """Plain payment section with no recognisable bank → leave
    media_key NULL so the merchant can pick the right slug from the
    dropdown manually."""
    key = detect_payment_media_key(
        section_kind="payment_method",
        section_title="طرق الدفع المتاحة",
        section_body="نقبل وسائل دفع متعددة.",
        media_title="QR generic",
        link_role="barcode",
    )
    assert key is None


def test_detect_returns_none_when_multiple_specific_banks_match():
    """If two SPECIFIC banks are detected we must bail — binding to
    either one risks sending the wrong QR. The dashboard will then
    show «بدون مفتاح» so the merchant fixes it manually."""
    key = detect_payment_media_key(
        section_kind="bank_transfer",
        section_title="حسابات التحويل",
        section_body="الراجحي والأهلي مع برق.",
        media_title="QR متعدد",
        link_role="barcode",
    )
    assert key is None


def test_detect_returns_none_for_empty_text():
    """An empty link (no title, no body, no media title) cannot be
    classified — guard against the future case where a merchant
    creates the asset with no metadata."""
    assert detect_payment_media_key(
        section_kind="payment_method",
        section_title="",
        section_body="",
        media_title="",
        link_role="barcode",
    ) is None


def test_detect_normalises_alef_variants():
    """Customer / merchant may write «الأهلي» / «الاهلي» / «إلأهلي»
    — Arabic alef normalisation in the registry must collapse them
    all so the inferrer hits regardless of orthography."""
    for spelling in ("الأهلي", "الاهلي", "إلاهلي", "Al-Ahli"):
        key = detect_payment_media_key(
            section_kind="bank_transfer",
            section_title=f"تحويل {spelling}",
            section_body="",
            media_title="",
            link_role="barcode",
        )
        assert key == "payment_alahli_barcode", f"{spelling!r} did not resolve"


# ── 3. Registry consistency (internal invariants) ─────────────────────


def test_every_bank_key_in_map_exists_in_registry():
    """Static check: every value in ``_BANK_KEY_MAP`` must be a
    registered key in :data:`media_key_registry.REGISTRY`. Without
    this, a typo in the autolink module would silently fail the
    safety-net lookup with no obvious error in production."""
    for bank_id, slug in _BANK_KEY_MAP.items():
        assert registry.is_valid_key(slug), (
            f"bank {bank_id!r} maps to {slug!r} which is not a "
            "registered MediaKey — the autolink would set a key the "
            "resolver cannot find"
        )


def test_every_bank_pattern_normalises_to_nonempty():
    """All pre-normalised patterns must be non-empty after the
    normalisation pass. An empty entry would never match anything
    and indicates a bad source-list edit."""
    for bank_id, patterns in _NORMALISED_PATTERNS.items():
        assert patterns, f"bank {bank_id!r} has zero usable patterns"
        for p in patterns:
            assert p.strip(), (
                f"bank {bank_id!r} has an empty pattern — "
                "did a source string normalise to whitespace?"
            )


def test_valid_payment_kinds_only_contains_payment_groups():
    """Defensive: the gate must only allow kinds that are in
    group 3 (sales policies) in the section-kinds registry. Adding
    a non-payment kind here would let merchants accidentally
    autobind unrelated assets."""
    from services.knowledge_section_kinds import REGISTRY as kinds_registry
    payment_groups = {3}
    for kind in _VALID_PAYMENT_KINDS:
        match = next((sk for sk in kinds_registry if sk.kind == kind), None)
        assert match is not None, f"unknown payment kind: {kind!r}"
        assert match.group in payment_groups, (
            f"kind {kind!r} is in group {match.group}, not a sales-"
            "policy group — it shouldn't qualify for payment autolink"
        )


# ── 4. Trigger expansion (find_key_for_query realism) ─────────────────


@pytest.mark.parametrize(
    ("query", "expected_key"),
    [
        # Rajhi — the original gap from the screenshot.
        ("أبي باركود الراجحي",                "payment_rajhi_barcode"),
        ("أبي أحول للراجحي",                  "payment_rajhi_barcode"),
        ("تحويل للراجحي اليوم",               "payment_rajhi_barcode"),
        ("ابي حساب الراجحي",                  "payment_rajhi_barcode"),
        ("QR الراجحي لو سمحت",                "payment_rajhi_barcode"),
        ("Alrajhi QR please",                "payment_rajhi_barcode"),
        # Alahli.
        ("أبي تحويل للأهلي",                  "payment_alahli_barcode"),
        ("باركود الأهلي يعمل؟",               "payment_alahli_barcode"),
        ("Saudi National Bank transfer",     "payment_alahli_barcode"),
        ("SNB qr code",                      "payment_alahli_barcode"),
        # Barq.
        ("ابغى احول لحساب برق",               "payment_barq_barcode"),
        ("Barq transfer",                    "payment_barq_barcode"),
        # STC Pay.
        ("ابي ادفع stc pay",                  "payment_stcpay_qr"),
        ("محفظة STC",                         "payment_stcpay_qr"),
        ("ابي اس تي سي باي",                  "payment_stcpay_qr"),
        # Mobily Pay.
        ("Mobily Pay يقبل التحويل؟",          "payment_mobilypay_qr"),
        ("محفظة موبايلي",                     "payment_mobilypay_qr"),
        # Generic IBAN.
        ("أبي الآيبان",                       "payment_bank_transfer_image"),
        ("IBAN please",                      "payment_bank_transfer_image"),
    ],
)
def test_find_key_for_query_resolves_expanded_triggers(query, expected_key):
    """Each realistic Saudi-customer phrasing must hit the right
    registry key after the May 2026 #20 trigger expansion. This is
    the test that catches a regression that drops a trigger from
    the registry while still passing the rest of the suite."""
    assert registry.find_key_for_query(query) == expected_key, (
        f"query={query!r} did not resolve to {expected_key!r} — "
        f"got {registry.find_key_for_query(query)!r}"
    )


@pytest.mark.parametrize(
    "ambiguous_query",
    [
        "ابي اعرف وسائل الدفع المتاحة",   # no specific bank
        "متى توصل الطلبية؟",              # shipping ask, not payment
        "السلام عليكم",                   # greeting
    ],
)
def test_find_key_for_query_returns_none_for_non_payment_queries(ambiguous_query):
    """Trigger expansion must not start matching general questions —
    those would attach a payment QR to every greeting otherwise."""
    # Note: shipping_instruction_image has "الشحن" as a trigger, so a
    # shipping ask LEGITIMATELY resolves to that key — we just check
    # it doesn't resolve to a payment key.
    hit = registry.find_key_for_query(ambiguous_query)
    if hit is not None:
        assert not hit.startswith("payment_"), (
            f"non-payment query {ambiguous_query!r} resolved to a payment "
            f"key {hit!r} — trigger leak"
        )


# ── 5. Router wiring (source-text guard) ──────────────────────────────


def test_link_media_endpoint_calls_autolink_helper():
    """Pin the wiring so a future refactor cannot drop the autolink
    call from the manual link_media endpoint without flipping this
    test red."""
    src = (_BACKEND_ROOT / "routers" / "knowledge.py").read_text(encoding="utf-8")
    # The helper exists and is called at least twice (manual endpoint
    # + draft-approval branch).
    assert "_maybe_autolink_payment_media_key" in src
    assert src.count("_maybe_autolink_payment_media_key(") >= 3, (
        "expected the autolink helper to be invoked from BOTH the "
        "manual link_media endpoint and the AI _apply_op_to_db "
        "link_media branch (and the idempotent re-link path)"
    )


def test_autolink_helper_imports_from_pure_module():
    """The helper in the router must import the pure inferrer from
    ``services.payment_media_autolink`` — not redefine bank-detection
    logic inline. Keeps the bank-pattern source of truth in ONE
    place so trigger updates are testable in isolation."""
    src = (_BACKEND_ROOT / "routers" / "knowledge.py").read_text(encoding="utf-8")
    assert "from services.payment_media_autolink import" in src
    assert "detect_payment_media_key" in src


def test_autolink_helper_never_overwrites_existing_media_key():
    """Pin the contract on the router helper itself — once a media
    has a media_key (set manually or by an earlier autolink call),
    the helper MUST be a no-op."""
    src = (_BACKEND_ROOT / "routers" / "knowledge.py").read_text(encoding="utf-8")
    # The helper body must guard on media.media_key BEFORE inferring.
    helper_start = src.index("def _maybe_autolink_payment_media_key")
    helper_end = src.index("\n\ndef ", helper_start)
    helper_body = src[helper_start:helper_end]
    assert "media.media_key" in helper_body, (
        "helper must read media.media_key as part of its skip guard"
    )
    assert "return None" in helper_body, (
        "helper must return None when the skip guard fires"
    )


# ── 6. Generic payment-barcode noun classifier ───────────────────────


@pytest.mark.parametrize(
    "query",
    [
        # Bare Latin "QR"
        "QR لو سمحت",
        "ابي qr",
        # Arabic transliterations
        "كيو ار",
        "كيو آر",
        "كيوار من فضلك",
        # "باركود" alone (the most common phrasing)
        "باركود",
        "ابي الباركود",
        "تعطيني بار كود",
        # "رمز X" combinations — only the ones we whitelisted
        "رمز الدفع",
        "ابي رمز التحويل",
        "ممكن رمز السداد",
    ],
)
def test_is_generic_payment_barcode_query_matches_bare_nouns(query):
    """The classifier must catch every bare-noun phrasing the
    merchant told us customers use most often."""
    assert registry.is_generic_payment_barcode_query(query) is True, (
        f"bare generic query {query!r} was not classified as generic"
    )


@pytest.mark.parametrize(
    "query",
    [
        # Specific bank → must DEFER to find_key_for_query path
        "qr الراجحي",
        "باركود الراجحي",
        "رمز الأهلي",
        "ابي ادفع stc pay",
        "Mobily Pay يقبل التحويل؟",
        # Empty / whitespace
        "",
        "   ",
        # Greeting / unrelated
        "السلام عليكم",
        "متى توصل الطلبية؟",
        # The word "رمز" alone — must NOT fire (we require
        # "رمز الدفع/التحويل/السداد" pairings)
        "ابي الرمز",
        "رمز ترقيتي",
    ],
)
def test_is_generic_payment_barcode_query_skips_non_generic(query):
    """Defer to the specific-bank path whenever a bank is named,
    and don't fire on the bare word ``رمز`` or unrelated greetings.
    Without these guards the generic fallback would over-fire on
    every greeting that happens to contain a payment noun."""
    assert registry.is_generic_payment_barcode_query(query) is False, (
        f"non-generic query {query!r} was incorrectly classified as generic"
    )


# ── 7. resolve_generic_payment_barcode — DB-aware fallback ───────────


class _FakeMediaItem:
    """Minimal stand-in for AIMediaItem rows used by the resolver.

    We only populate the fields the resolver reads. Booleans for
    ``is_active`` are real ``True`` so the SQLAlchemy ``is_(True)``
    filter has nothing to disagree with at the Python level. (The
    fake session below filters in-memory anyway.)
    """

    def __init__(
        self, *, id, tenant_id, media_key,
        title="X", media_type="image", file_url="https://x/y",
        is_active=True,
    ):
        self.id = id
        self.tenant_id = tenant_id
        self.media_key = media_key
        self.title = title
        self.media_type = media_type
        self.file_url = file_url
        self.mime_type = "image/png"
        self.storage_kind = "external"
        self.storage_path = None
        self.file_size_bytes = None
        self.is_active = is_active


class _FakeQuery:
    """In-memory subset of a SQLAlchemy Query the resolver needs.

    Supports ``.filter().order_by().limit().all()`` and ``.first()``.
    The filter() call is a NO-OP — the test seed already contains
    only the rows that match. We DO honor ``limit`` so the resolver's
    "limit(2)" early-exit semantics are preserved.
    """

    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, n):
        self._rows = self._rows[: int(n)]
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Hand back the seeded ``AIMediaItem`` rows for any query.

    Only one table is in play; we don't need to inspect the model
    argument. This mirrors the pattern used by
    ``test_knowledge_phase1._FakeSession``.
    """

    def __init__(self, rows):
        self._rows = list(rows)

    def query(self, model):
        return _FakeQuery(self._rows)


def test_resolve_generic_payment_barcode_single_asset_fires():
    """When the tenant has exactly ONE payment barcode uploaded and
    the customer says a bare 'باركود', resolve to that asset."""
    from services.media_resolver import resolve_generic_payment_barcode

    rows = [_FakeMediaItem(id=42, tenant_id=33, media_key="payment_rajhi_barcode")]
    session = _FakeSession(rows)

    resolution, key = resolve_generic_payment_barcode(session, 33, "باركود")
    assert key == "payment_rajhi_barcode"
    assert resolution is not None
    assert resolution.id == 42
    assert resolution.media_key == "payment_rajhi_barcode"


def test_resolve_generic_payment_barcode_returns_none_when_multiple_assets():
    """Two specific payment barcodes uploaded → ambiguous → bail.
    Better to make the customer name the bank than to ship the wrong
    QR and cost the merchant a misrouted transfer."""
    from services.media_resolver import resolve_generic_payment_barcode

    rows = [
        _FakeMediaItem(id=42, tenant_id=33, media_key="payment_rajhi_barcode"),
        _FakeMediaItem(id=43, tenant_id=33, media_key="payment_alahli_barcode"),
    ]
    session = _FakeSession(rows)

    resolution, key = resolve_generic_payment_barcode(session, 33, "ابي qr")
    assert resolution is None
    assert key is None


def test_resolve_generic_payment_barcode_returns_none_when_zero_assets():
    """No payment barcode uploaded → can't fall back, return None."""
    from services.media_resolver import resolve_generic_payment_barcode

    session = _FakeSession([])

    resolution, key = resolve_generic_payment_barcode(session, 33, "رمز الدفع")
    assert resolution is None
    assert key is None


def test_resolve_generic_payment_barcode_defers_to_specific_bank():
    """When the query NAMES a bank, the generic fallback must NOT
    fire — the find_key_for_query path owns that case (and produces
    a more precise answer)."""
    from services.media_resolver import resolve_generic_payment_barcode

    rows = [_FakeMediaItem(id=42, tenant_id=33, media_key="payment_rajhi_barcode")]
    session = _FakeSession(rows)

    # The customer named the bank → must defer to the primary path.
    resolution, key = resolve_generic_payment_barcode(
        session, 33, "ابي باركود الراجحي",
    )
    assert resolution is None
    assert key is None


def test_resolve_for_query_routes_generic_to_fallback():
    """End-to-end: the public ``resolve_for_query`` entrypoint must
    transparently fall back to the generic path when no specific
    bank trigger matches. Callers (``apply_media_key_safety_net``)
    use the same signature and don't need to learn about the new
    code path."""
    from services.media_resolver import resolve_for_query

    rows = [_FakeMediaItem(id=99, tenant_id=33, media_key="payment_stcpay_qr")]
    session = _FakeSession(rows)

    resolution, key = resolve_for_query(session, 33, "QR لو سمحت")
    assert key == "payment_stcpay_qr"
    assert resolution is not None
    assert resolution.id == 99


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
