"""
tests/test_tenant_payment_accounts.py
─────────────────────────────────────
Tenant 33 #48 (May 2026) — payment understanding correction.

These tests cover the reusable building blocks that decide whether
a customer's payment evidence is real and matches the merchant's
official accounts. They never load the AI brain — every test is a
pure-function check on the pieces in
``backend/core/tenant_payment_accounts.py``.

Coverage map
────────────
1. IBAN canonicalisation (strip whitespace + hyphens, uppercase).
2. IBAN extraction from messy text (multiple bank-app formats).
3. Beneficiary extraction from labelled body text.
4. ``load_tenant_payment_accounts`` reads bank_transfer / payment_method
   KB sections and produces a typed snapshot.
5. ``receipt_matches_tenant_accounts`` returns the four documented
   verdicts: match, mismatch, no_tenant_accounts, no_signal_in_receipt.
6. Beneficiary token-set match (all tokens must appear).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest

from core.tenant_payment_accounts import (
    TenantPaymentAccounts,
    canonical_iban,
    extract_beneficiaries,
    extract_ibans,
    load_tenant_payment_accounts,
    receipt_matches_tenant_accounts,
)


# ── Test fixtures ───────────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_a, **_k):
        return _FakeQuery(self._rows)


def _kb_section(
    *,
    kind: str = "bank_transfer",
    title: str = "",
    body: str = "",
    metadata_json: dict = None,
    is_active: bool = True,
    section_id: int = 1,
):
    return SimpleNamespace(
        id=section_id,
        kind=kind,
        title=title,
        body=body,
        metadata_json=metadata_json or {},
        is_active=is_active,
    )


# ── 1. IBAN canonicalisation ────────────────────────────────────────


class TestCanonicalIban:
    def test_simple_iban(self):
        assert canonical_iban("SA0380000000608010167519") == "SA0380000000608010167519"

    def test_strips_whitespace(self):
        assert canonical_iban("SA03 8000 0000 6080 1016 7519") == "SA0380000000608010167519"

    def test_strips_hyphens(self):
        assert canonical_iban("SA03-8000-0000-6080-1016-7519") == "SA0380000000608010167519"

    def test_uppercases(self):
        assert canonical_iban("sa0380000000608010167519") == "SA0380000000608010167519"

    def test_rejects_non_saudi_prefix(self):
        assert canonical_iban("AE070331234567890123456") == ""

    def test_rejects_short(self):
        assert canonical_iban("SA03800000006080") == ""

    def test_rejects_long(self):
        assert canonical_iban("SA038000000060801016751999") == ""

    def test_rejects_letters_in_digits(self):
        assert canonical_iban("SA0380000000608010167X19") == ""

    def test_empty_returns_empty(self):
        assert canonical_iban("") == ""
        assert canonical_iban(None) == ""  # type: ignore[arg-type]


# ── 2. IBAN extraction from blob text ───────────────────────────────


class TestExtractIbans:
    def test_finds_single_iban(self):
        text = "تحويل إلى الراجحي SA0380000000608010167519 نجح بحمد الله"
        assert extract_ibans(text) == ["SA0380000000608010167519"]

    def test_finds_iban_with_spacing(self):
        text = "IBAN: SA03 8000 0000 6080 1016 7519\nAmount: 250 SAR"
        assert extract_ibans(text) == ["SA0380000000608010167519"]

    def test_finds_iban_with_hyphens(self):
        text = "حساب الراجحي\nSA03-8000-0000-6080-1016-7519"
        assert extract_ibans(text) == ["SA0380000000608010167519"]

    def test_finds_multiple_distinct_ibans(self):
        text = (
            "حساب 1: SA0380000000608010167519\n"
            "حساب 2: SA1234567890123456789012\n"
        )
        result = extract_ibans(text)
        assert "SA0380000000608010167519" in result
        assert "SA1234567890123456789012" in result
        assert len(result) == 2

    def test_dedupes_repeats(self):
        text = (
            "SA0380000000608010167519\n"
            "SA03 8000 0000 6080 1016 7519\n"
        )
        assert extract_ibans(text) == ["SA0380000000608010167519"]

    def test_no_iban_returns_empty(self):
        assert extract_ibans("صباح الخير، أبي اعرف سعر العسل") == []

    def test_empty_input(self):
        assert extract_ibans(None) == []
        assert extract_ibans("") == []


# ── 3. Beneficiary extraction ───────────────────────────────────────


class TestExtractBeneficiaries:
    def test_arabic_label(self):
        text = "اسم المستفيد: عبدالله محمد السالم\nرقم الحساب: 12345"
        out = extract_beneficiaries(text)
        assert any("عبدالله" in b and "محمد" in b for b in out)

    def test_english_label(self):
        text = "Beneficiary: Abdullah Mohammed\nIBAN: SA03..."
        out = extract_beneficiaries(text)
        assert any("abdullah" in b for b in out)

    def test_no_label_returns_empty(self):
        assert extract_beneficiaries("just some text") == []

    def test_drops_overlong_beneficiary(self):
        long_name = "اسم المستفيد: " + ("أ" * 200)
        assert extract_beneficiaries(long_name) == []


# ── 4. load_tenant_payment_accounts ─────────────────────────────────


class TestLoadTenantPaymentAccounts:
    def test_no_db_returns_empty(self):
        accts = load_tenant_payment_accounts(None, tenant_id=1)
        assert isinstance(accts, TenantPaymentAccounts)
        assert not accts.has_accounts

    def test_no_tenant_id_returns_empty(self):
        accts = load_tenant_payment_accounts(_FakeDB([]), tenant_id=0)
        assert not accts.has_accounts

    def test_no_rows_returns_empty(self):
        accts = load_tenant_payment_accounts(_FakeDB([]), tenant_id=33)
        assert not accts.has_accounts

    def test_extracts_iban_from_body(self):
        db = _FakeDB([
            _kb_section(
                kind="bank_transfer",
                title="حساب الراجحي",
                body="IBAN: SA0380000000608010167519\nاسم المستفيد: نحلة",
            ),
        ])
        accts = load_tenant_payment_accounts(db, tenant_id=33)
        assert "SA0380000000608010167519" in accts.ibans
        assert accts.has_accounts

    def test_extracts_iban_from_metadata_json(self):
        db = _FakeDB([
            _kb_section(
                kind="bank_transfer",
                title="حساب الراجحي",
                body="معاملة بنكية",
                metadata_json={
                    "iban": "SA03 8000 0000 6080 1016 7519",
                    "beneficiary_name": "نحلة الفلاح",
                },
            ),
        ])
        accts = load_tenant_payment_accounts(db, tenant_id=33)
        assert "SA0380000000608010167519" in accts.ibans
        assert any("نحله" in b or "نحلة" in b for b in accts.beneficiaries)

    def test_includes_payment_method_kind(self):
        db = _FakeDB([
            _kb_section(
                kind="payment_method",
                title="طرق الدفع",
                body="SA0380000000608010167519",
            ),
        ])
        accts = load_tenant_payment_accounts(db, tenant_id=33)
        assert "SA0380000000608010167519" in accts.ibans

    def test_dedupes_across_sections(self):
        db = _FakeDB([
            _kb_section(
                kind="bank_transfer",
                body="SA0380000000608010167519",
                section_id=1,
            ),
            _kb_section(
                kind="bank_transfer",
                body="SA03 8000 0000 6080 1016 7519",
                section_id=2,
            ),
        ])
        accts = load_tenant_payment_accounts(db, tenant_id=33)
        assert accts.ibans.count("SA0380000000608010167519") == 1


# ── 5. receipt_matches_tenant_accounts ──────────────────────────────


class TestReceiptMatchesTenantAccounts:
    def test_no_tenant_accounts(self):
        verdict = receipt_matches_tenant_accounts(
            accounts=TenantPaymentAccounts(),
            receipt_text="some receipt text with SA0380000000608010167519",
        )
        assert verdict["status"] == "no_tenant_accounts"

    def test_no_signal_in_receipt(self):
        accts = TenantPaymentAccounts(
            ibans=("SA0380000000608010167519",),
        )
        verdict = receipt_matches_tenant_accounts(
            accounts=accts,
            receipt_text="just a screenshot description, no IBAN here",
        )
        assert verdict["status"] == "no_signal_in_receipt"

    def test_iban_match(self):
        accts = TenantPaymentAccounts(
            ibans=("SA0380000000608010167519",),
        )
        verdict = receipt_matches_tenant_accounts(
            accounts=accts,
            receipt_text="تم التحويل إلى SA0380000000608010167519 بنجاح",
        )
        assert verdict["status"] == "match"
        assert verdict["matched_iban"] == "SA0380000000608010167519"

    def test_iban_match_with_spacing(self):
        accts = TenantPaymentAccounts(
            ibans=("SA0380000000608010167519",),
        )
        verdict = receipt_matches_tenant_accounts(
            accounts=accts,
            receipt_text="إلى الحساب SA03 8000 0000 6080 1016 7519",
        )
        assert verdict["status"] == "match"

    def test_iban_mismatch(self):
        accts = TenantPaymentAccounts(
            ibans=("SA0380000000608010167519",),
        )
        verdict = receipt_matches_tenant_accounts(
            accounts=accts,
            receipt_text="تم التحويل إلى SA9999999999999999999999",
        )
        assert verdict["status"] == "mismatch"
        assert verdict["matched_iban"] == ""

    def test_beneficiary_match_all_tokens(self):
        accts = TenantPaymentAccounts(
            beneficiaries=("نحله الفلاح",),
        )
        verdict = receipt_matches_tenant_accounts(
            accounts=accts,
            receipt_text="اسم المستفيد: نحله الفلاح للتجاره",
        )
        assert verdict["status"] == "match"
        assert "نحله" in verdict["matched_beneficiary"]

    def test_beneficiary_partial_does_not_match(self):
        accts = TenantPaymentAccounts(
            beneficiaries=("نحله الفلاح",),
        )
        verdict = receipt_matches_tenant_accounts(
            accounts=accts,
            receipt_text="اسم المستفيد: محمد علي",
        )
        assert verdict["status"] == "mismatch"
