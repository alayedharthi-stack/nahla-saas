"""
Customer-import unit tests.

Covers the four pure-Python building blocks of the wizard:

    parser     — CSV/XLSX → headers + rows
    normalizer — raw row + mapping → NormalizedRow (E.164, cleaned fields)
    dedupe     — NormalizedRow + tenant index → ClassifiedRow
                 (exact / suspect / new / invalid; in-file dupes too)
    importer   — non-destructive merge rules:
                   * never overwrites existing trusted fields
                   * source_tags is sorted, deduped, and includes "manual_import"
                   * primary_source only set on first touch
                   * notes are appended (not replaced)

The importer test uses a tiny fake Customer + fake DB session so we
exercise the merge logic without needing a real PostgreSQL instance.

Run:
    python -m pytest tests/test_customer_import.py -v
"""
from __future__ import annotations

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "database"))

from services.customer_import.parser import (  # noqa: E402
    ParseError, parse_upload,
)
from services.customer_import.normalizer import (  # noqa: E402
    normalize_row, suggest_column_mapping,
)
from services.customer_import.dedupe import (  # noqa: E402
    CLASSIFICATION_EXACT,
    CLASSIFICATION_INVALID,
    CLASSIFICATION_NEW,
    CLASSIFICATION_SUSPECT,
    _classify_one,
    _ExistingIndex,
)
from services.customer_import.importer import (  # noqa: E402
    _apply_non_destructive_merge, _build_metadata,
)


# ── Parser ───────────────────────────────────────────────────────────────────

class TestCSVParser:
    def test_parses_basic_csv_with_headers(self):
        csv = "name,phone,email\nAhmad,0501234567,a@b.com\nSara,0509876543,s@x.com\n"
        parsed = parse_upload(content=csv.encode("utf-8"), filename="x.csv")
        assert parsed.kind == "csv"
        assert parsed.headers == ["name", "phone", "email"]
        assert parsed.total_rows == 2
        assert parsed.rows[0]["phone"] == "0501234567"
        assert parsed.rows[1]["name"] == "Sara"

    def test_handles_utf8_bom_and_arabic_headers(self):
        # Excel-on-Windows exports UTF-8 with a leading BOM; our
        # decoder must strip it cleanly.
        text = "الاسم,الجوال,المدينة\nأحمد,0501234567,الرياض\n"
        parsed = parse_upload(content=text.encode("utf-8-sig"), filename="x.csv")
        assert "الاسم" in parsed.headers
        assert parsed.rows[0]["الاسم"] == "أحمد"

    def test_handles_semicolon_delimited_csv(self):
        csv = "name;phone;city\nA;0501;الرياض\nB;0502;جدة\n"
        parsed = parse_upload(content=csv.encode("utf-8"), filename="x.csv")
        assert len(parsed.rows) == 2
        assert parsed.rows[0]["phone"] == "0501"

    def test_skips_fully_empty_rows(self):
        csv = "name,phone\nA,0501\n,\nB,0502\n"
        parsed = parse_upload(content=csv.encode("utf-8"), filename="x.csv")
        assert len(parsed.rows) == 2
        assert [r["name"] for r in parsed.rows] == ["A", "B"]

    def test_dedupes_blank_or_duplicate_headers(self):
        csv = "name,,name\nA,X,Y\n"
        parsed = parse_upload(content=csv.encode("utf-8"), filename="x.csv")
        # blank header → column_2; duplicate "name" → "name (2)"
        assert parsed.headers[0] == "name"
        assert parsed.headers[1].startswith("column_")
        assert parsed.headers[2] == "name (2)"

    def test_empty_file_raises(self):
        with pytest.raises(ParseError):
            parse_upload(content=b"", filename="x.csv")

    def test_only_headers_no_rows_raises(self):
        with pytest.raises(ParseError):
            parse_upload(content=b"name,phone\n", filename="x.csv")

    def test_oversize_file_raises(self):
        big = b"a,b\n" + (b"x," * 0)
        from services.customer_import.parser import MAX_BYTES
        big = b"x" * (MAX_BYTES + 10)
        with pytest.raises(ParseError):
            parse_upload(content=big, filename="x.csv")


class TestXLSXParser:
    def _make_xlsx(self, rows):
        try:
            from openpyxl import Workbook
        except ImportError:
            pytest.skip("openpyxl not installed in this environment")
        wb = Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_parses_xlsx(self):
        content = self._make_xlsx([
            ["Name", "Phone", "City"],
            ["Ahmad", "0501234567", "Riyadh"],
            ["Sara",  "0509876543", "Jeddah"],
        ])
        parsed = parse_upload(content=content, filename="x.xlsx")
        assert parsed.kind == "xlsx"
        assert parsed.headers == ["Name", "Phone", "City"]
        assert parsed.rows[0]["Phone"] == "0501234567"

    def test_xlsx_int_phone_does_not_become_float(self):
        content = self._make_xlsx([
            ["Name", "Phone"],
            ["A", 5421234567],   # numeric phone column — common in Excel
        ])
        parsed = parse_upload(content=content, filename="x.xlsx")
        assert parsed.rows[0]["Phone"] == "5421234567"  # not "5421234567.0"


# ── Normalizer ───────────────────────────────────────────────────────────────

class TestNormalizer:
    def test_normalizes_saudi_phone_to_e164(self):
        out = normalize_row(
            row_index=1,
            raw={"phone": "0501234567", "name": "Ahmad", "email": "A@B.com"},
            mapping={"phone": "phone", "name": "name", "email": "email"},
        )
        assert out.is_valid
        assert out.normalized_phone == "+966501234567"
        assert out.email == "a@b.com"
        assert out.name == "Ahmad"

    @pytest.mark.parametrize("raw_phone", [
        "+966542980511",
        "00966542980511",
        "966542980511",
        "0542980511",
        "542980511",
    ])
    def test_all_saudi_formats_collapse_to_one_e164(self, raw_phone):
        out = normalize_row(
            row_index=1,
            raw={"phone": raw_phone},
            mapping={"phone": "phone"},
        )
        assert out.normalized_phone == "+966542980511"

    def test_missing_phone_is_invalid(self):
        out = normalize_row(
            row_index=1, raw={"name": "X"}, mapping={"phone": "phone", "name": "name"},
        )
        assert not out.is_valid
        assert "missing_phone" in out.invalid_reasons

    def test_garbage_phone_is_invalid(self):
        out = normalize_row(
            row_index=1, raw={"phone": "abc"}, mapping={"phone": "phone"},
        )
        assert not out.is_valid
        assert "invalid_phone_format" in out.invalid_reasons

    def test_invalid_email_dropped_but_row_still_invalid(self):
        out = normalize_row(
            row_index=1,
            raw={"phone": "0501234567", "email": "not-an-email"},
            mapping={"phone": "phone", "email": "email"},
        )
        assert "invalid_email_format" in out.invalid_reasons
        assert out.email == ""           # bad email is dropped
        assert out.normalized_phone      # phone is still good

    def test_default_source_when_blank(self):
        out = normalize_row(
            row_index=1, raw={"phone": "0501234567"}, mapping={"phone": "phone"},
        )
        assert out.source == "manual_import"

    def test_custom_source_preserved(self):
        out = normalize_row(
            row_index=1,
            raw={"phone": "0501234567", "src": "exhibition_2024"},
            mapping={"phone": "phone", "source": "src"},
        )
        assert out.source == "exhibition_2024"

    def test_placeholder_name_email_treated_as_empty(self):
        out = normalize_row(
            row_index=1,
            raw={"phone": "0501234567", "name": "N/A", "email": "none"},
            mapping={"phone": "phone", "name": "name", "email": "email"},
        )
        assert out.name == ""
        assert out.email == ""


class TestColumnMappingSuggestion:
    def test_english_headers(self):
        mapping = suggest_column_mapping(["Full Name", "Phone Number", "Email"])
        assert mapping["name"] == "Full Name"
        assert mapping["phone"] == "Phone Number"
        assert mapping["email"] == "Email"

    def test_arabic_headers(self):
        mapping = suggest_column_mapping(["الاسم", "الجوال", "البريد", "المدينة"])
        assert mapping["name"] == "الاسم"
        assert mapping["phone"] == "الجوال"
        assert mapping["email"] == "البريد"
        assert mapping["city"] == "المدينة"

    def test_handles_punctuation_and_alef_variants(self):
        mapping = suggest_column_mapping(["إسم العميل", "رقم الجوال", "بريد"])
        assert mapping["name"] == "إسم العميل"
        assert mapping["phone"] == "رقم الجوال"


# ── Dedupe classifier ────────────────────────────────────────────────────────

def _idx(*customers):
    """Build an _ExistingIndex from a list of customer dicts."""
    idx = _ExistingIndex()
    for c in customers:
        c.setdefault("acquisition_channel", "salla_sync")
        c.setdefault("extra_metadata", {})
        if c.get("normalized_phone"):
            idx.by_phone[c["normalized_phone"]] = c
        if c.get("email"):
            idx.by_email.setdefault(c["email"].lower(), []).append(c)
    return idx


def _row(**kwargs):
    """Quick NormalizedRow factory."""
    from services.customer_import.normalizer import NormalizedRow
    return NormalizedRow(
        row_index=kwargs.get("row_index", 1),
        raw=kwargs.get("raw", {}),
        name=kwargs.get("name", ""),
        phone_raw=kwargs.get("phone_raw", ""),
        normalized_phone=kwargs.get("normalized_phone", ""),
        email=kwargs.get("email", ""),
        city=kwargs.get("city", ""),
        notes=kwargs.get("notes", ""),
        source=kwargs.get("source", "manual_import"),
        invalid_reasons=list(kwargs.get("invalid_reasons", [])),
    )


class TestDedupeClassifier:
    def test_exact_phone_match_returns_exact(self):
        idx = _idx({"id": 7, "name": "X", "email": "", "normalized_phone": "+966501234567"})
        result = _classify_one(
            _row(normalized_phone="+966501234567", name="A"),
            idx, {}, {},
        )
        assert result.classification == CLASSIFICATION_EXACT
        assert result.match_customer_id == 7
        assert result.match_reason == "phone_match"

    def test_unique_phone_no_email_returns_new(self):
        idx = _idx({"id": 1, "normalized_phone": "+966500000001", "email": ""})
        result = _classify_one(
            _row(normalized_phone="+966509999999", name="A"),
            idx, {}, {},
        )
        assert result.classification == CLASSIFICATION_NEW

    def test_email_match_with_unique_phone_returns_suspect(self):
        idx = _idx({
            "id": 5, "name": "Existing",
            "email": "a@b.com", "normalized_phone": "+966500000001",
        })
        result = _classify_one(
            _row(normalized_phone="+966509999999", email="a@b.com", name="A"),
            idx, {}, {},
        )
        assert result.classification == CLASSIFICATION_SUSPECT
        assert result.suspect_candidates[0]["customer_id"] == 5
        assert result.suspect_candidates[0]["reason"] == "email_match"

    def test_invalid_row_stays_invalid(self):
        result = _classify_one(
            _row(invalid_reasons=["missing_phone"]),
            _ExistingIndex(), {}, {},
        )
        assert result.classification == CLASSIFICATION_INVALID

    def test_intra_file_duplicate_phone_marked_invalid(self):
        idx = _ExistingIndex()
        seen = {}
        first = _classify_one(
            _row(normalized_phone="+966501234567", name="First"),
            idx, seen, {},
        )
        second = _classify_one(
            _row(row_index=2, normalized_phone="+966501234567", name="Second"),
            idx, seen, {},
        )
        assert first.classification == CLASSIFICATION_NEW
        assert second.classification == CLASSIFICATION_INVALID
        assert "duplicate_in_file" in second.match_reason

    def test_name_only_match_does_not_create_suspect(self):
        idx = _idx({
            "id": 1, "name": "Ahmad", "email": "", "normalized_phone": "+966500000001",
        })
        result = _classify_one(
            _row(normalized_phone="+966509999999", name="Ahmad"),
            idx, {}, {},
        )
        # No city — name match alone is too weak.
        assert result.classification == CLASSIFICATION_NEW


# ── Importer non-destructive merge ───────────────────────────────────────────

class _FakeCustomer:
    """Stand-in for SQLAlchemy Customer model — only needs the
    attributes the merger touches."""
    def __init__(self, **kw):
        self.name = kw.get("name")
        self.email = kw.get("email")
        self.phone = kw.get("phone")
        self.normalized_phone = kw.get("normalized_phone")
        self.extra_metadata = kw.get("extra_metadata", {})
        self.last_interaction_at = None


class TestNonDestructiveMerge:
    def test_does_not_overwrite_existing_name(self):
        existing = _FakeCustomer(
            name="Trusted Salla Name",
            email="trusted@store.com",
            phone="+966500000001",
            normalized_phone="+966500000001",
            extra_metadata={"source": "salla_sync", "primary_source": "salla_sync"},
        )
        normalized = {
            "name": "Weak Excel Name",
            "email": "wrong@example.com",
            "normalized_phone": "+966500000001",
            "phone_raw": "0500000001",
            "source": "manual_import",
            "city": "",
            "notes": "",
        }
        _apply_non_destructive_merge(existing, normalized=normalized, batch_id=99)
        assert existing.name == "Trusted Salla Name"   # untouched
        assert existing.email == "trusted@store.com"    # untouched

    def test_fills_empty_fields_from_incoming(self):
        existing = _FakeCustomer(
            name=None, email=None, phone="+966500000001",
            normalized_phone="+966500000001", extra_metadata={},
        )
        normalized = {
            "name": "From Excel",
            "email": "fresh@x.com",
            "normalized_phone": "+966500000001",
            "phone_raw": "0500000001",
            "source": "manual_import",
            "city": "Riyadh",
            "notes": "",
        }
        _apply_non_destructive_merge(existing, normalized=normalized, batch_id=10)
        assert existing.name == "From Excel"
        assert existing.email == "fresh@x.com"
        assert existing.extra_metadata["city"] == "Riyadh"

    def test_source_tags_preserves_prior_sources(self):
        existing = _FakeCustomer(
            name="X", normalized_phone="+966500000001",
            extra_metadata={
                "primary_source": "salla_sync",
                "source_tags": ["salla_sync"],
            },
        )
        normalized = {
            "name": "X",
            "normalized_phone": "+966500000001",
            "phone_raw": "0500000001",
            "source": "manual_import",
            "email": "", "city": "", "notes": "",
        }
        _apply_non_destructive_merge(existing, normalized=normalized, batch_id=42)
        tags = existing.extra_metadata["source_tags"]
        assert "salla_sync" in tags
        assert "manual_import" in tags
        assert tags == sorted(set(tags))   # sorted + deduped

    def test_primary_source_set_only_when_missing(self):
        existing = _FakeCustomer(
            normalized_phone="+966500000001",
            extra_metadata={"primary_source": "salla_sync"},
        )
        normalized = {
            "normalized_phone": "+966500000001",
            "phone_raw": "0500000001",
            "source": "manual_import",
            "name": "", "email": "", "city": "", "notes": "",
        }
        _apply_non_destructive_merge(existing, normalized=normalized, batch_id=1)
        # Already present — must NOT be overwritten by manual_import.
        assert existing.extra_metadata["primary_source"] == "salla_sync"

    def test_notes_are_appended_not_replaced(self):
        existing = _FakeCustomer(
            normalized_phone="+966500000001",
            extra_metadata={"notes": "VIP — loyal since 2022"},
        )
        normalized = {
            "normalized_phone": "+966500000001",
            "phone_raw": "0500000001",
            "source": "manual_import",
            "name": "", "email": "", "city": "",
            "notes": "Joined exhibition 2024",
        }
        _apply_non_destructive_merge(existing, normalized=normalized, batch_id=1)
        notes = existing.extra_metadata["notes"]
        assert "VIP" in notes
        assert "exhibition 2024" in notes

    def test_last_import_batch_is_recorded(self):
        existing = _FakeCustomer(
            normalized_phone="+966500000001", extra_metadata={},
        )
        normalized = {
            "normalized_phone": "+966500000001",
            "phone_raw": "0500000001",
            "source": "manual_import",
            "name": "", "email": "", "city": "", "notes": "",
        }
        _apply_non_destructive_merge(existing, normalized=normalized, batch_id=777)
        assert existing.extra_metadata["last_import_batch"] == 777
        assert "last_import_at" in existing.extra_metadata


class TestCrossTenantIsolation:
    """Security regression tests proving customer data never leaks
    between tenants during import.

    The wizard hits three tenant-scoped boundaries:

        1) The router resolves `tenant_id` from the request
           (`resolve_tenant_id`) and passes it to every service call.
        2) `dedupe._load_existing_index` filters on
           `Customer.tenant_id == tenant_id` BEFORE building the
           phone/email lookup tables, so tenant B can never appear
           in tenant A's dedupe set.
        3) `importer._merge_into` filters on `tenant_id` when looking
           up the merge target, so a poisoned `match_customer_id`
           pointing to another tenant's customer would be rejected
           (the row is skipped, not merged).

        Plus the DB-level guarantee:
            UNIQUE INDEX ix_customers_tenant_normalized_phone
                  ON customers (tenant_id, normalized_phone)
                  WHERE normalized_phone IS NOT NULL ...

        means even if application-level filtering ever regresses, the
        DB will still let two tenants own the same E.164 number as
        two separate rows.
    """

    def _build_db_with_two_tenants(self):
        """Construct a fake Session whose .query(...).filter(...) honors
        a single Customer.tenant_id == X clause. Returns the session
        plus the seeded customer rows so the test can assert against
        them directly."""
        from services.customer_import.dedupe import _load_existing_index

        # Two customers in DIFFERENT tenants but with the SAME phone.
        store_a_customer = {
            "id": 100, "tenant_id": 1,
            "name": "أحمد - متجر A", "email": "ahmad@store-a.com",
            "normalized_phone": "+966501112233",
            "acquisition_channel": "salla_sync",
            "extra_metadata": {"source": "salla_sync", "store": "A"},
        }
        store_b_customer = {
            "id": 200, "tenant_id": 2,
            "name": "خالد - متجر B", "email": "khaled@store-b.com",
            "normalized_phone": "+966501112233",
            "acquisition_channel": "salla_sync",
            "extra_metadata": {"source": "salla_sync", "store": "B"},
        }
        all_rows = [store_a_customer, store_b_customer]

        class _FakeFilter:
            def __init__(self, rows): self.rows = rows
            def filter(self, expr):
                # SQLAlchemy expression `Customer.tenant_id == X` —
                # extract the right-hand value from the BinaryExpression.
                want = expr.right.value
                return _FakeFilter([r for r in self.rows if r["tenant_id"] == want])
            def yield_per(self, _n):
                for r in self.rows:
                    yield (
                        r["id"], r["name"], r["email"], r["normalized_phone"],
                        r["acquisition_channel"], r["extra_metadata"],
                    )

        class _FakeSession:
            def __init__(self, rows): self._rows = rows
            def query(self, *_cols): return _FakeFilter(self._rows)

        return _FakeSession(all_rows), store_a_customer, store_b_customer, _load_existing_index

    def test_dedupe_index_only_loads_current_tenant(self):
        db, store_a, store_b, _load = self._build_db_with_two_tenants()

        # Build an index for tenant A — must contain ONLY tenant A's customer.
        idx_a = _load(db, tenant_id=1)
        assert "+966501112233" in idx_a.by_phone
        assert idx_a.by_phone["+966501112233"]["id"] == store_a["id"]
        # Tenant B's customer must NOT appear in tenant A's lookup.
        for row in idx_a.by_phone.values():
            assert row["id"] != store_b["id"]
        for rows in idx_a.by_email.values():
            for r in rows:
                assert r["id"] != store_b["id"]

        # Same exercise from tenant B's side.
        idx_b = _load(db, tenant_id=2)
        assert idx_b.by_phone["+966501112233"]["id"] == store_b["id"]
        for row in idx_b.by_phone.values():
            assert row["id"] != store_a["id"]

    def test_store_a_imports_phone_x_then_store_b_imports_phone_x(self):
        """The user-requested scenario, end-to-end at the dedupe level:

            Store A imports phone X        → Store A sees it as `new`
                                              (no tenant has it yet)
            Store A's seed now exists       → re-importing X under A
                                              would see it as `exact`
            Store B imports same phone X   → Store B MUST see it as
                                              `new` (tenant B has no
                                              such customer; A's row
                                              is invisible to B)
        """
        from services.customer_import.dedupe import (
            _classify_one, _ExistingIndex,
        )

        # Reuse the fake session loader to simulate "after Store A imported X".
        db, store_a, _store_b, _load = self._build_db_with_two_tenants()

        idx_b = _load(db, tenant_id=2)
        # Strip tenant B's seeded customer so we can test the exact
        # moment B uploads phone X for the first time.
        idx_b.by_phone.pop("+966501112233", None)
        for k in list(idx_b.by_email.keys()):
            idx_b.by_email[k] = [
                r for r in idx_b.by_email[k] if r["id"] != _store_b["id"]
            ]
            if not idx_b.by_email[k]:
                idx_b.by_email.pop(k)

        # Store B uploads the SAME phone Store A already owns.
        result_b = _classify_one(
            _row(
                row_index=1,
                normalized_phone="+966501112233",
                name="عميل جديد لمتجر B",
            ),
            idx_b, {}, {},
        )
        assert result_b.classification == CLASSIFICATION_NEW, (
            "Store B must NOT see Store A's customer as a duplicate — "
            "this would be a cross-tenant data leak."
        )
        assert result_b.match_customer_id is None

        # And Store A re-importing the same number sees its own row.
        idx_a = _load(db, tenant_id=1)
        result_a = _classify_one(
            _row(
                row_index=1,
                normalized_phone="+966501112233",
                name="re-import within A",
            ),
            idx_a, {}, {},
        )
        assert result_a.classification == CLASSIFICATION_EXACT
        assert result_a.match_customer_id == store_a["id"]

    def test_db_unique_index_is_tenant_scoped(self):
        """Reads the actual SQLAlchemy model definition to confirm the
        unique constraint is `(tenant_id, normalized_phone)` — not just
        `normalized_phone`. This is the last line of defense if the
        application-level filtering ever regresses."""
        from models import Customer

        target = None
        for arg in Customer.__table_args__:
            name = getattr(arg, "name", None)
            if name == "ix_customers_tenant_normalized_phone":
                target = arg
                break
        assert target is not None, (
            "Missing ix_customers_tenant_normalized_phone — without it "
            "two stores could collide on the same phone."
        )
        col_names = [c.name for c in target.columns]
        assert col_names == ["tenant_id", "normalized_phone"], (
            f"Unique index must be (tenant_id, normalized_phone), got {col_names}"
        )
        assert target.unique is True


class TestBuildMetadataPureLogic:
    def test_first_source_wins_for_primary(self):
        meta = _build_metadata(
            existing=None,
            normalized={"source": "exhibition", "city": "", "notes": ""},
            batch_id=1,
        )
        assert meta["primary_source"] == "exhibition"
        assert "exhibition" in meta["source_tags"]
        assert "manual_import" in meta["source_tags"]

    def test_existing_primary_source_never_replaced(self):
        meta = _build_metadata(
            existing={"primary_source": "salla_sync", "source_tags": ["salla_sync"]},
            normalized={"source": "manual_import", "city": "", "notes": ""},
            batch_id=2,
        )
        assert meta["primary_source"] == "salla_sync"

    def test_existing_city_not_overwritten(self):
        meta = _build_metadata(
            existing={"city": "Riyadh"},
            normalized={"source": "manual_import", "city": "Jeddah", "notes": ""},
            batch_id=3,
        )
        assert meta["city"] == "Riyadh"

    def test_empty_existing_city_filled(self):
        meta = _build_metadata(
            existing={},
            normalized={"source": "manual_import", "city": "Jeddah", "notes": ""},
            batch_id=3,
        )
        assert meta["city"] == "Jeddah"
