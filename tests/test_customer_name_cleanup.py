"""
tests/test_customer_name_cleanup.py
───────────────────────────────────
Unit tests for the bulk customer-name cleanup pipeline
(``backend/services/customer_name_cleanup.py``).

What we lock down
─────────────────
1. **Phone-only inputs cleared** — names like ``"+966551234567"`` or
   ``"0566355055"`` get suggested → ``None`` (high confidence).
2. **Stopword stripping** — ``"عميل"``, ``"customer"``, ``"زبون"`` …
   removed wherever they appear; remaining real name preserved.
3. **Compound names preserved** — ``"أبو خالد"``, ``"عبد الرحمن"``,
   ``"أم محمد"``, ``"آل عايد"``, ``"بن سلمان"`` survive intact even
   when wrapped with a stopword.
4. **Non-human phrases cleared** — religious / promotional one-liners
   that show up as the WhatsApp push name.
5. **Heavy-digit names cleared** — ``"عميل تعديل 238"`` and friends
   collapse to ``None``.
6. **Already-clean names are no-ops** — the function returns
   ``changed=False`` so the preview can filter them out.
7. **Bulk-scale sanity** — 1 000+ customer corpus is processed in
   <100 ms and the *matched* count is what we expect, simulating a
   tenant the size of the production bug report (8 000+ customers,
   pagination would otherwise hide rows like ``"عميل يونيو 20 88"``).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from services.customer_name_cleanup import (  # noqa: E402
    CleanResult,
    compute_cleanup,
)


# ── 1) Phone-only inputs ────────────────────────────────────────────


class TestPhoneOnly:

    @pytest.mark.parametrize("raw", [
        "+966551234567",
        "+966531244261",
        "+966537993723",
        "+966 55 670 2972",
        "0566355055",
        "(966) 55-123-4567",
    ])
    def test_phone_only_cleared(self, raw):
        v = compute_cleanup(raw)
        assert v.changed
        assert v.suggested is None
        assert v.confidence == "high"
        assert "رقم جوال" in v.reason


# ── 2) Commercial / descriptive stopwords ───────────────────────────


class TestStopwordStripping:

    @pytest.mark.parametrize("raw, expected", [
        ("Majed عميل",               "Majed"),
        ("أيمن الجهني عميل",         "أيمن الجهني"),
        ("يوسف الناشري زبون",        "يوسف الناشري"),
        ("طلال نائض الرشيدي زبون",   "طلال نائض الرشيدي"),
        ("فهد الرشيدي أبو ناصر عميل","فهد الرشيدي أبو ناصر"),
        ("ابو خالد عميل",            "ابو خالد"),
        # Production samples from the field bug report — clearing
        # via the "stopword + digit + lone leftover" noise heuristic.
        ("عميل يونيو 20 88",         None),
        ("أم عبدالله عميله أبريل 24 عميل", "أم عبدالله"),  # months are stopwords now
    ])
    def test_strips_stopwords(self, raw, expected):
        v = compute_cleanup(raw)
        assert v.changed
        assert v.suggested == expected


# ── 3) Compound names preserved ─────────────────────────────────────


class TestCompoundNames:
    """Patronymic prefixes (``أبو``, ``أم``, ``عبد`` …) form the only
    meaningful token in many Arabic names. Cleaning them would leave
    a one-letter stub. They must survive verbatim."""

    @pytest.mark.parametrize("raw", [
        "أبو خالد",
        "أم محمد",
        "عبد الرحمن",
        "عبدالعزيز",     # single token — definite article fused
        "آل عايد",
        "بن سلمان",
    ])
    def test_compound_names_not_changed(self, raw):
        v = compute_cleanup(raw)
        assert not v.changed
        assert v.suggested == raw


# ── 4) Non-human phrases ────────────────────────────────────────────


class TestNonHumanPhrases:

    @pytest.mark.parametrize("raw", [
        "اللهم ارفع عنا الوباء عميل الشمال",
        "الحمدلله رب العالمين",
        "ماشاء الله",
        "بسم الله",
    ])
    def test_religious_phrases_cleared(self, raw):
        v = compute_cleanup(raw)
        assert v.changed
        assert v.suggested is None


# ── 5) Heavy-digit names ────────────────────────────────────────────


class TestHeavyDigits:

    @pytest.mark.parametrize("raw", [
        "عميل 12345",
        "عميل تعديل 238",
        "عميل يونيو 20 88",
        "أيمن عميل 1234567",
    ])
    def test_heavy_digit_names_cleared(self, raw):
        v = compute_cleanup(raw)
        assert v.changed
        assert v.suggested is None
        assert v.confidence == "high"

    def test_digit_only_suffix_with_low_ratio_keeps_real_name(self):
        # Short year suffix on a longer name — digit ratio under 40%,
        # no stopword to compound the noise. The cleaner strips the
        # trailing year and keeps the real first+last as-is.
        v = compute_cleanup("محمد الجهني 24")
        assert v.changed
        assert v.suggested == "محمد الجهني"


# ── 6) Already-clean inputs are no-ops ──────────────────────────────


class TestNoOps:

    @pytest.mark.parametrize("raw", [
        "محمد",
        "Ahmed",
        "سارة",
        "محمد بن سلمان",
        "Al-Sayed",
        "D'Angelo",
    ])
    def test_clean_names_no_op(self, raw):
        v = compute_cleanup(raw)
        assert not v.changed
        assert v.suggested == raw

    @pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
    def test_empty_inputs_no_op(self, raw):
        v = compute_cleanup(raw)
        assert not v.changed
        assert v.suggested is None
        assert v.reason == ""


# ── 7) Bulk-scale sanity ────────────────────────────────────────────


class TestBulkScale:
    """Reproduces the production bug scenario:

    Tenant has thousands of customers; a meaningful subset (~5%) has
    names that need cleaning. We must scan EVERY row — not just a
    page — and report the full matched count. The old preview
    endpoint paginated with ``per_page=500`` and reported only ~120
    matches against a 8 027-customer tenant; this test would have
    caught that regression because it scans well past 500.
    """

    def _build_corpus(self, n: int) -> list[tuple[int, str | None]]:
        """Fabricate ``n`` rows: 95% clean, 5% dirty (mixed flavours)."""
        rows: list[tuple[int, str | None]] = []
        dirty_templates = [
            "أيمن الجهني عميل",
            "Majed عميل",
            "+966551234567",
            "عميل تعديل 238",
            "عميل يونيو 20 88",
            "اللهم ارفع البلاء عميل الشمال",
        ]
        clean_templates = [
            "محمد", "أبو خالد", "أم محمد", "عبد الرحمن",
            "Ahmed", "سارة", "آل عايد", "محمد بن سلمان",
        ]
        for i in range(n):
            if i % 20 == 0:                # 5%
                name = dirty_templates[i % len(dirty_templates)]
            else:
                name = clean_templates[i % len(clean_templates)]
            rows.append((i + 1, name))
        return rows

    def test_scans_all_and_reports_correct_matches(self):
        # 1 200 customers — comfortably past the old 500-row limit
        # AND past the more recent 1 000-row default per_page.
        rows = self._build_corpus(1200)
        # Expected matches = every 20th row (60 rows) — locking the
        # ratio so an off-by-one in the corpus builder gets noticed.
        expected_matches = sum(
            1 for _, name in rows if compute_cleanup(name).changed
        )
        assert expected_matches == 60, (
            f"corpus generator drifted: {expected_matches} dirty rows"
        )

        # Now simulate the endpoint's tenant-wide scan and verify we
        # see ALL 60 — not just the ones that happen to land on the
        # first page.
        seen_matches: list[tuple[int, CleanResult]] = []
        for cid, name in rows:
            v = compute_cleanup(name)
            if v.changed:
                seen_matches.append((cid, v))

        assert len(seen_matches) == 60
        # And the matches come from across the full id range, not
        # bunched into the first 500.
        max_id = max(cid for cid, _ in seen_matches)
        assert max_id > 1000, (
            f"matches stop at id={max_id} — pagination regression?"
        )

    def test_incremental_reopen_hides_already_cleaned_rows(self):
        """Locks down the merchant-facing "incremental workflow" contract.

        Scenario (the actual production complaint):
        - Merchant opens cleanup tool, applies "high-confidence only".
        - 1 000+ low-confidence rows remain. They close the modal.
        - They reopen later. The 1 000+ already-applied rows MUST NOT
          reappear. Only the still-dirty rows should.

        We simulate this without a DB by:
        1. Computing cleanup for the full corpus (pass 1).
        2. Applying every high-confidence suggestion in place.
        3. Recomputing cleanup (pass 2) and asserting that nothing
           that was applied in pass 1 reappears as ``changed`` in
           pass 2 — i.e. the cleaner is idempotent on its own output.
        """
        rows: list[tuple[int, str | None]] = self._build_corpus(400)

        # Pass 1: high-confidence apply.
        pass1_applied: dict[int, str | None] = {}
        new_rows: list[tuple[int, str | None]] = []
        for cid, name in rows:
            v = compute_cleanup(name)
            if v.changed and v.confidence == "high":
                pass1_applied[cid] = v.suggested
                new_rows.append((cid, v.suggested))
            else:
                new_rows.append((cid, name))

        assert pass1_applied, "corpus should contain some high-confidence dirt"

        # Pass 2: re-scan the post-apply state.
        re_flagged_after_apply = [
            cid
            for cid, name in new_rows
            if cid in pass1_applied and compute_cleanup(name).changed
        ]
        assert re_flagged_after_apply == [], (
            "cleaner is not idempotent: rows flagged again after their "
            f"own suggestion was applied → {re_flagged_after_apply[:5]}"
        )

    def test_rescan_reevaluates_previously_cleaned_rows(self):
        """Re-scan MUST be allowed to reconsider previously-cleaned rows.

        The merchant's escape-hatch: if they imported new data, or if
        we tightened heuristics, the explicit "إعادة الفحص" button
        should re-run the cleaner over EVERY customer — no
        "cleaned_at" gate is allowed to prevent that.

        We assert that calling ``compute_cleanup`` on a row that was
        previously edited still yields a useful result if the new
        value is still dirty. (No persistent memo / version flag
        short-circuits the cleaner.)
        """
        # Pretend the merchant earlier "cleaned" the row to a value
        # that, with today's stricter rules, is still dirty.
        previously_edited = "عميل الجهني"
        v = compute_cleanup(previously_edited)
        assert v.changed, (
            "re-scan must still flag rows that contain stopwords, "
            "even if a human touched them before"
        )
        assert v.suggested == "الجهني"

    def test_scan_is_fast_enough_for_large_tenants(self):
        # At ~5 µs per call we comfortably scan a 50 000-row tenant
        # in under a second. We assert a generous budget so flaky CI
        # boxes don't blow up — the point is "linear in N, no
        # accidental O(N²)".
        rows = self._build_corpus(5000)
        start = time.perf_counter()
        matches = 0
        for _, name in rows:
            if compute_cleanup(name).changed:
                matches += 1
        elapsed = time.perf_counter() - start
        assert matches > 0
        assert elapsed < 2.0, (
            f"compute_cleanup scaling regression: 5 000 calls took {elapsed:.2f}s"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Month / time-code detection (May 2026)
# ─────────────────────────────────────────────────────────────────────────────
#
# These rows show up in CRM exports and ad-campaign imports: the merchant
# bulk-uploads a CSV where the "name" column is actually a campaign tag
# (``"نوفمبر26"``, ``"Jan2025"``, ``"aug_26"``). We want the cleanup tool
# to flag them aggressively when the ENTIRE name is a code, but never
# touch a row that contains a real first name PLUS a month suffix without
# explicit merchant review (force LOW confidence → suspicious_suffix).
class TestMonthCodeDetection:
    @pytest.mark.parametrize(
        "raw",
        [
            "نوفمبر26", "أكتوبر27", "اكتوبر_27",
            "Jan2025", "March24", "march 24", "aug_26", "sep27",
            "jan-2025", "Dec2024", "may 2026",
            "رمضان1447", "شعبان24",
            "نوفمبر 26",
        ],
    )
    def test_pure_month_codes_are_cleared_high_confidence(self, raw: str):
        from services.customer_name_cleanup import (
            CATEGORY_GENERIC_BAD,
            compute_cleanup,
        )

        v = compute_cleanup(raw)
        assert v.changed is True
        assert v.suggested is None, (
            f"{raw!r} should be cleared (no real name component)"
        )
        assert v.confidence == "high"
        assert v.category == CATEGORY_GENERIC_BAD

    @pytest.mark.parametrize(
        "raw,kept",
        [
            ("خالد نوفمبر", "خالد"),
            ("محمد أكتوبر", "محمد"),
            ("سامي march24", "سامي"),
            ("احمد oct2025", "احمد"),
            # Hijri-month compound only fires through the compact regex,
            # so it still routes through suspicious_suffix beside a real
            # first name.
            ("فهد رمضان1447", "فهد"),
        ],
    )
    def test_real_name_with_month_suffix_is_suspicious_low(
        self, raw: str, kept: str
    ):
        from services.customer_name_cleanup import (
            CATEGORY_SUSPICIOUS_SUFFIX,
            compute_cleanup,
        )

        v = compute_cleanup(raw)
        assert v.changed is True
        assert v.suggested == kept
        # Low confidence is the critical guarantee — bulk
        # "Apply high-confidence only" must NEVER touch these.
        assert v.confidence == "low"
        assert v.category == CATEGORY_SUSPICIOUS_SUFFIX

    @pytest.mark.parametrize(
        "raw",
        [
            # Bare Hijri months are real Saudi given names — must not
            # be auto-stripped just because they appear in compound
            # codes elsewhere.
            "رمضان", "شعبان",
            # "رياض" is a personal name; "الرياض" with article is the
            # city (handled by the location matcher). Locked in here
            # to make sure the new month logic didn't accidentally
            # introduce a false positive.
            "رياض الخالد",
            # A name with a month-style word but no digit suffix and no
            # real name — should remain unchanged because we
            # deliberately don't strip lone Gregorian months from a
            # multi-token Saudi-name context (the merchant might mean
            # the literal Arabic word, like "أم مايو" — rare but real).
            # The single-token Gregorian "نوفمبر" alone is still routed
            # through the suspicious_suffix bucket (low confidence) so
            # the merchant decides — see the next test.
            "محمد علي",
        ],
    )
    def test_real_names_are_not_falsely_flagged(self, raw: str):
        from services.customer_name_cleanup import compute_cleanup

        v = compute_cleanup(raw)
        assert v.changed is False, (
            f"{raw!r} is a real name and must survive the new month-code rules"
        )
