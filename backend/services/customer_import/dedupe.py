"""
services/customer_import/dedupe.py
──────────────────────────────────
Three-level dedupe classifier for the import wizard.

For each `NormalizedRow` the classifier produces a `ClassifiedRow`
with one of:

    "invalid"  — row cannot be imported (no phone or bad phone format)
    "exact"    — normalized_phone matches an existing tenant customer;
                 the import will MERGE non-destructively, not insert.
    "suspect"  — phone is unique BUT email matches an existing customer,
                 OR a strong name+city heuristic match exists. The
                 wizard surfaces these for manual decision before commit.
    "new"      — phone is unique and no other strong overlap; safe to
                 create a fresh customer.

The classifier loads the tenant's existing customers in BATCHES so a
10k upload against a 100k customer book does not OOM. We index them
by normalized_phone, lower(email), and (lower-name, lower-city) so
each row check is O(1) lookups against three dicts.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .normalizer import NormalizedRow

logger = logging.getLogger("nahla.import.dedupe")

CLASSIFICATION_INVALID  = "invalid"
CLASSIFICATION_EXACT    = "exact"
CLASSIFICATION_SUSPECT  = "suspect"
CLASSIFICATION_NEW      = "new"

ALL_CLASSIFICATIONS = (
    CLASSIFICATION_INVALID,
    CLASSIFICATION_EXACT,
    CLASSIFICATION_SUSPECT,
    CLASSIFICATION_NEW,
)


@dataclass
class ClassifiedRow:
    row_index: int
    classification: str
    normalized: Dict[str, Any]
    match_customer_id: Optional[int] = None
    match_reason: str = ""
    suspect_candidates: List[Dict[str, Any]] = field(default_factory=list)
    # Set when there is an exact phone match — carries the existing customer's
    # acquisition_channel (e.g. "salla_sync") and their current name so the
    # wizard can tell the merchant "this customer is already in your store".
    match_acquisition_channel: str = ""
    match_customer_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "row_index": self.row_index,
            "classification": self.classification,
            "normalized": dict(self.normalized),
            "match_customer_id": self.match_customer_id,
            "match_reason": self.match_reason,
            "suspect_candidates": list(self.suspect_candidates),
            "match_acquisition_channel": self.match_acquisition_channel,
            "match_customer_name": self.match_customer_name,
        }


# ── Public API ───────────────────────────────────────────────────────────────

def classify_rows(
    db: Any,
    *,
    tenant_id: int,
    rows: List[NormalizedRow],
) -> List[ClassifiedRow]:
    """Classify all rows against the tenant's existing customer book.

    Detects:
        1) duplicates inside the upload itself (two rows sharing the
           same normalized_phone are collapsed — only the first becomes
           "new"/"exact"/"suspect", the rest are flagged "invalid"
           with reason `duplicate_in_file` to keep the import idempotent).
        2) duplicates against the existing tenant customers.
    """
    index = _load_existing_index(db, tenant_id=tenant_id)

    seen_phones_in_file: Dict[str, int] = {}
    seen_emails_in_file: Dict[str, int] = {}
    out: List[ClassifiedRow] = []

    for row in rows:
        result = _classify_one(row, index, seen_phones_in_file, seen_emails_in_file)
        out.append(result)

    return out


# ── Existing-customer index ──────────────────────────────────────────────────

@dataclass
class _ExistingIndex:
    by_phone:  Dict[str, Dict[str, Any]] = field(default_factory=dict)
    by_email:  Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    by_name_city: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)


def _load_existing_index(db: Any, *, tenant_id: int) -> _ExistingIndex:
    index = _ExistingIndex()
    if not db or not tenant_id:
        return index

    try:
        from models import Customer  # noqa: PLC0415
    except Exception:  # pragma: no cover
        logger.warning("Customer model unavailable — dedupe degraded")
        return index

    try:
        # Stream tenant customers in chunks to keep memory bounded.
        query = (
            db.query(
                Customer.id,
                Customer.name,
                Customer.email,
                Customer.normalized_phone,
                Customer.acquisition_channel,
                Customer.extra_metadata,
            )
            .filter(Customer.tenant_id == tenant_id)
            .yield_per(2000)
        )
        for cid, name, email, phone, channel, meta in query:
            row = {
                "id": cid,
                "name": name or "",
                "email": (email or "").strip().lower(),
                "normalized_phone": (phone or "").strip(),
                "acquisition_channel": channel or "",
                "extra_metadata": meta or {},
            }
            if row["normalized_phone"]:
                index.by_phone[row["normalized_phone"]] = row
            if row["email"]:
                index.by_email.setdefault(row["email"], []).append(row)
            city = _city_from_meta(row["extra_metadata"])
            key = _name_city_key(row["name"], city)
            if key:
                index.by_name_city.setdefault(key, []).append(row)
    except Exception as exc:
        logger.warning("dedupe index load failed: %s", exc)

    return index


# ── Per-row classification ───────────────────────────────────────────────────

def _classify_one(
    row: NormalizedRow,
    index: _ExistingIndex,
    seen_phones_in_file: Dict[str, int],
    seen_emails_in_file: Dict[str, int],
) -> ClassifiedRow:
    base = ClassifiedRow(
        row_index=row.row_index,
        classification=CLASSIFICATION_INVALID,
        normalized=row.to_dict(),
    )

    if not row.is_valid:
        base.match_reason = ",".join(row.invalid_reasons) or "invalid"
        return base

    phone = row.normalized_phone
    email = (row.email or "").strip().lower()

    # Intra-file dedupe — second occurrence of the same phone in the
    # same upload is treated as invalid so we never write twice.
    if phone:
        prior = seen_phones_in_file.get(phone)
        if prior is not None:
            base.match_reason = f"duplicate_in_file:row_{prior}"
            return base
        seen_phones_in_file[phone] = row.row_index

    # 1) Exact phone match against existing customers.
    if phone and phone in index.by_phone:
        existing = index.by_phone[phone]
        return ClassifiedRow(
            row_index=row.row_index,
            classification=CLASSIFICATION_EXACT,
            normalized=row.to_dict(),
            match_customer_id=existing["id"],
            match_reason="phone_match",
            match_acquisition_channel=existing.get("acquisition_channel") or "",
            match_customer_name=existing.get("name") or "",
        )

    # 2) Strong-suspect: same email or strong name+city overlap.
    suspects: List[Dict[str, Any]] = []

    if email:
        candidates = index.by_email.get(email, [])
        for c in candidates:
            suspects.append({
                "customer_id": c["id"],
                "name": c["name"],
                "email": c["email"],
                "normalized_phone": c["normalized_phone"],
                "acquisition_channel": c["acquisition_channel"],
                "reason": "email_match",
            })

    name_key = _name_city_key(row.name, row.city)
    if name_key:
        for c in index.by_name_city.get(name_key, []):
            # Skip if we already added this candidate via email.
            if any(s["customer_id"] == c["id"] for s in suspects):
                continue
            # Require the existing customer to ALSO have a phone OR
            # email on file — otherwise a name collision against an
            # empty record is too noisy to flag.
            if not (c["normalized_phone"] or c["email"]):
                continue
            suspects.append({
                "customer_id": c["id"],
                "name": c["name"],
                "email": c["email"],
                "normalized_phone": c["normalized_phone"],
                "acquisition_channel": c["acquisition_channel"],
                "reason": "name_city_match",
            })

    # Track email duplicates inside the file too — second use of the
    # same email becomes a suspect against the first occurrence.
    if email:
        prior = seen_emails_in_file.get(email)
        if prior is not None:
            suspects.append({
                "customer_id": None,
                "name": "",
                "email": email,
                "normalized_phone": "",
                "acquisition_channel": "in_file",
                "reason": f"duplicate_email_in_file:row_{prior}",
            })
        else:
            seen_emails_in_file[email] = row.row_index

    if suspects:
        return ClassifiedRow(
            row_index=row.row_index,
            classification=CLASSIFICATION_SUSPECT,
            normalized=row.to_dict(),
            suspect_candidates=suspects,
            match_reason=suspects[0]["reason"],
        )

    return ClassifiedRow(
        row_index=row.row_index,
        classification=CLASSIFICATION_NEW,
        normalized=row.to_dict(),
    )


# ── Heuristic helpers ────────────────────────────────────────────────────────

_NAME_PUNCT = re.compile(r"[\s_\-\.\:،]+", re.UNICODE)


def _city_from_meta(meta: Dict[str, Any]) -> str:
    if not isinstance(meta, dict):
        return ""
    val = meta.get("city") or meta.get("City") or ""
    return str(val).strip()


def _name_city_key(name: str, city: str) -> str:
    n = _normalize_for_match(name)
    c = _normalize_for_match(city)
    if not n or not c:
        return ""
    if len(n) < 3:
        return ""  # single-token names match too aggressively
    return f"{n}|{c}"


def _normalize_for_match(value: str) -> str:
    if not value:
        return ""
    s = str(value).strip().lower()
    s = _NAME_PUNCT.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه")
    return s


# Convenience for test introspection without importing dataclasses.
def to_dict(row: ClassifiedRow) -> Dict[str, Any]:  # pragma: no cover
    return asdict(row)
