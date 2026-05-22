"""
backend/tests/test_staff_contact_kb_scan.py
─────────────────────────────────────────────
Regression suite for the May 2026 #36 staff-contact KB-scan layer.

Production bug we are pinning down:
  Customer asks "أبغى أكلم أمين"; Claude replies
  "تواصل مع أمين بائع المعرض" (NO phone in the reply, NO
  ``[CALL:...]`` marker emitted) and the customer never gets the
  number, even though the merchant typed
  "أمين - 0541690226" into a free-form ``branches`` / ``custom``
  KB section months ago.

Root cause:
  The pre-fix ``apply_staff_contact_safety_net`` only scanned the
  LLM reply for digits. When the reply omitted the phone (the
  reported case), the net bailed with ``"no_phone_in_reply"`` and
  no contact card was attached.

Fix (this commit):
  Two-layer resolution:
    1. Reply scan (legacy, unchanged).
    2. KB free-text scan: looks up the ``MerchantKnowledgeSection``
       rows for the tenant whose ``kind`` is in
       :data:`_STAFF_KB_FALLBACK_KINDS` and matches a
       name+phone pair within
       :data:`_STAFF_KB_PROXIMITY_WINDOW` characters. The first
       hit wins (DB ``priority`` ordering).

These tests assert the resolution chain, the proximity rule, the
alif-folding name match, the structured ``[STAFF_CONTACT_RESOLVER]``
telemetry, and the no-regression invariant for the reply-scan
path.
"""
from __future__ import annotations

import os
import re
import sys
import types as _types
from typing import Any, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ── Stubs ────────────────────────────────────────────────────────────────────


class _StubKBSection:
    def __init__(
        self,
        *,
        section_id: int,
        kind: str,
        body: str,
        is_active: bool = True,
        priority: int = 100,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.body = body
        self.is_active = is_active
        self.priority = priority
        self.updated_at = section_id  # monotonic stand-in


class _KBQuery:
    def __init__(self, sections: List[_StubKBSection]) -> None:
        self._sections = list(sections)

    def filter(self, *args: Any, **kwargs: Any) -> "_KBQuery":
        for expr in args:
            kinds = getattr(expr, "_kinds", None)
            if kinds:
                self._sections = [
                    s for s in self._sections if s.kind in kinds
                ]
        return self

    def order_by(self, *_: Any) -> "_KBQuery":
        # Keep priority ASC then updated_at DESC stable; the
        # production query reorders the same way.
        self._sections.sort(
            key=lambda s: (s.priority, -s.updated_at)
        )
        return self

    def limit(self, _n: int) -> "_KBQuery":
        return self

    def all(self) -> List[_StubKBSection]:
        return list(self._sections)


class _StubDB:
    def __init__(self, sections: Optional[List[_StubKBSection]] = None) -> None:
        self._sections = list(sections or [])

    def query(self, _model: Any) -> _KBQuery:
        return _KBQuery(self._sections)


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    sections: Optional[List[_StubKBSection]] = None,
) -> _StubDB:
    """Install the minimal ``models`` + ``services.call_resolver``
    stubs the staff safety net needs. We deliberately do NOT stub
    ``core.tenant`` here — the staff net never touches it (only
    the maps net does)."""
    models_stub = _types.ModuleType("models")

    class _Col:
        def __init__(self, name: str) -> None:
            self.name = name

        def __eq__(self, other: Any) -> _types.SimpleNamespace:  # type: ignore[override]
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def is_(self, other: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def in_(self, values: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(
                col_name=self.name, _kinds=tuple(values),
            )

        def asc(self) -> "_Col":
            return self

        def desc(self) -> "_Col":
            return self

    class _MksStub:
        tenant_id = _Col("tenant_id")
        kind = _Col("kind")
        is_active = _Col("is_active")
        priority = _Col("priority")
        updated_at = _Col("updated_at")

    models_stub.MerchantKnowledgeSection = _MksStub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "models", models_stub)

    # services.call_resolver — we need CallTarget +
    # _normalize_saudi_phone + _pretty_phone.
    call_stub = _types.ModuleType("services.call_resolver")

    class _CallTarget:  # noqa: D401
        def __init__(self, **kwargs: Any) -> None:
            self.name = kwargs.get("name", "")
            self.wa_id = kwargs.get("wa_id", "")
            self.phone_display = kwargs.get("phone_display", "")
            self.raw_phone = kwargs.get("raw_phone", "")

    def _fake_normalize(phone: str) -> str:
        digits = re.sub(r"\D+", "", phone or "")
        if digits.startswith("00966"):
            digits = digits[2:]
        if digits.startswith("966"):
            return digits
        if digits.startswith("05"):
            return "966" + digits[1:]
        if digits.startswith("5") and len(digits) == 9:
            return "966" + digits
        return ""

    def _fake_pretty(wa_id: str) -> str:
        if not wa_id or not wa_id.startswith("966"):
            return wa_id or ""
        rest = wa_id[3:]
        return f"+966 {rest[:3]} {rest[3:6]} {rest[6:]}"

    call_stub.CallTarget = _CallTarget  # type: ignore[attr-defined]
    call_stub._normalize_saudi_phone = _fake_normalize  # type: ignore[attr-defined]
    call_stub._pretty_phone = _fake_pretty  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)

    # Force the staff net feature flag ON so the test environment
    # behaves like production. The flag default is also "on", but
    # being explicit guards against a CI env override.
    monkeypatch.setenv("STAFF_CONTACT_SAFETY_NET_ENABLED", "1")

    return _StubDB(sections or [])


# ── Layer 1 (reply scan) — legacy contract preserved ────────────────────────


def test_reply_scan_still_wins_when_phone_in_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply-scan path must remain the FIRST resolution layer.
    A phone in the LLM reply wins even when the KB also has one —
    Claude saw the KB and chose this number for THIS request."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين - 0500000000",
            )
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",
        reply_text="تواصل مع أمين على 0541690226",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.source == "reply"
    assert result.wa_id == "966541690226"


# ── Layer 2 (KB scan) — the May 2026 #36 fix ────────────────────────────────


def test_kb_scan_fires_when_reply_has_no_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production case: reply mentions name without a phone, but
    the merchant has the pair in a ``branches`` KB section. The
    safety net must lift the phone from the KB and ship the
    contact card."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=42, kind="branches",
                body=(
                    "الفرع الرئيسي – الرياض، حي الورود.\n"
                    "أمين بائع المعرض: 0541690226\n"
                    "الإدارة: 0555906901"
                ),
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",
        reply_text="تواصل مع أمين بائع المعرض، يخدمك على طول 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.source == "kb:branches"
    assert result.wa_id == "966541690226"
    assert "kb:branches" in result.reason


def test_kb_scan_alif_folding_resilient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alif and yaa variants must match across customer / KB:
    customer wrote ``أمين`` (with hamza), merchant typed
    ``امين`` (without). The match must still succeed, otherwise
    the KB scan would be brittle to keyboard noise."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="امين - 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",   # with hamza
        reply_text="تواصل مع أمين",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.source == "kb:branches"


def test_kb_scan_picks_nearest_phone_within_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proximity rule: when one section carries multiple staff
    name/phone pairs, the phone CLOSEST to the matched name wins.
    Stops "أمين" being mis-paired with the warehouse phone three
    bullets down."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body=(
                    "أمين بائع المعرض: 0541690226\n"
                    "خالد مسؤول المستودع: 0501234567"
                ),
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم خالد",
        reply_text="تواصل مع خالد",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.wa_id == "966501234567", (
        "proximity rule should pair خالد with his own phone, "
        "not أمين's number further up the body."
    )


def test_kb_scan_skips_phone_outside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence-in-depth: a phone that lives WAY past the proximity
    window is NOT promoted, even if no other phone exists. We'd
    rather miss than ship a wrong number."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    big_gap = "x" * 500
    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="custom",
                body=f"أمين هو بائع المعرض\n{big_gap}\nرقم المعرض: 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ابي رقم أمين",
        reply_text="تواصل مع أمين",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is False
    assert result.skipped_reason == "no_phone_in_reply"


def test_kb_scan_respects_kind_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sections in non-allowlisted kinds (e.g. ``forbidden_phrases``,
    ``response_tone``) MUST be ignored — they're behavioural rules,
    not contact directories. We should not lift a number from a
    "don't say كذا" rule and pretend it's a staff phone."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="forbidden_phrases",
                body="أمين - 0541690226",   # behavioural — must be ignored
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",
        reply_text="تواصل مع أمين",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is False
    assert result.skipped_reason == "no_phone_in_reply"


def test_kb_scan_role_noun_resolves_via_kb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Customer didn't say a name but used a role label
    ("بائع المعرض"). The static name allowlist now includes
    common Saudi role nouns, so the KB scan can still resolve
    a contact even without a personal name in the inbound."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="بائع المعرض: 0555906901",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ابي رقم بائع المعرض",
        reply_text="تواصل مع بائع المعرض",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.source == "kb:branches"
    assert result.wa_id == "966555906901"


def test_kb_scan_no_op_when_db_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backwards-compat: callers that don't pass ``db`` /
    ``tenant_id`` get the legacy reply-only behaviour. No crash,
    no implicit DB binding."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    _install_stubs(monkeypatch, sections=[])
    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",
        reply_text="تواصل مع أمين",
        existing_call_targets=[],
        detected_call_markers=0,
        # no db / tenant_id passed
    )
    assert result.fired is False
    assert result.skipped_reason == "no_phone_in_reply"


def test_kb_scan_logs_structured_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every staff-contact resolution must emit a single
    ``[STAFF_CONTACT_RESOLVER]`` INFO line so production triage
    can grep "why didn't أمين get a number?" without enabling
    DEBUG. Both hits and misses should be observable."""
    import logging
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=99, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )

    caplog.set_level(logging.INFO)
    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",
        reply_text="تواصل مع أمين",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    log_lines = [r.getMessage() for r in caplog.records]
    assert any("[STAFF_CONTACT_RESOLVER]" in ln for ln in log_lines), (
        f"expected a [STAFF_CONTACT_RESOLVER] line; got: {log_lines!r}"
    )
    # Hit details surfaced (kind + section_id).
    assert any(
        "source=kb:branches" in ln and "section_id=99" in ln
        for ln in log_lines
    )


def test_kb_scan_logs_miss_when_no_phone_anywhere(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telemetry must distinguish hits from misses. A miss emits a
    line with ``source=none`` (post-scan) so we can monitor how
    often the system needs to fall through to "no phone known"."""
    import logging
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(monkeypatch, sections=[])
    caplog.set_level(logging.INFO)
    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",
        reply_text="تواصل مع أمين",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is False
    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[STAFF_CONTACT_RESOLVER]" in ln and "source=none" in ln
        for ln in log_lines
    )
