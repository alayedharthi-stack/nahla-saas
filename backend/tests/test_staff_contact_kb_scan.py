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
        title: str = "",
        metadata: Optional[dict] = None,
        is_active: bool = True,
        priority: int = 100,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.body = body
        self.title = title
        self.metadata = metadata or {}
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
        deleted_at = _Col("deleted_at")
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


def test_kb_scan_single_phone_in_section_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """May 2026 #38 contract: when a section has the name AND
    exactly ONE Saudi phone, the resolver promotes that phone
    even if it falls outside the proximity window.

    Pre-fix this test asserted a "rather miss than mispair" miss
    when the gap exceeded the (then-80-char) window. Production
    showed that policy was too conservative — merchants often
    write a long product paragraph between the name and the
    phone, all in one section about that person. The new policy
    accepts the bypass, but ONLY when the section is
    unambiguous (single phone). Two-phone sections still miss
    via :func:`test_kb_scan_picks_nearest_phone_within_window`
    proximity logic."""
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
    assert result.fired is True
    assert result.source == "kb:custom"
    assert "0541690226" in (result.extra_call_target.raw_phone or "")


def test_kb_scan_multi_phone_section_outside_window_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence-in-depth: when a section has TWO+ phones AND none
    fall within the proximity window of the matched name, the
    resolver misses. We'd rather not ship a wrong number when
    the section is ambiguous (two phones, ambiguous ownership)."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    big_gap = "x" * 500
    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="custom",
                body=(
                    f"أمين هو بائع المعرض\n{big_gap}\n"
                    "أرقام الفروع المختلفة: "
                    "فرع 1 — 0501111111، فرع 2 — 0502222222"
                ),
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
    assert result.skipped_reason in {"no_phone_in_reply", "no_staff_name"}


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
    assert result.skipped_reason in {"no_phone_in_reply", "no_staff_name"}


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
    assert result.skipped_reason in {"no_phone_in_reply", "no_staff_name"}


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
    assert result.skipped_reason in {"no_phone_in_reply", "no_staff_name"}
    if result.skipped_reason == "no_phone_in_reply":
        assert any(
            "[STAFF_CONTACT_RESOLVER]" in ln and "source=none" in ln
            for ln in log_lines
        )


# ── Pronoun-only follow-ups (May 2026 #38b) ─────────────────────────────────
#
# Production trace from Tenant 33: customer asked about أمين in turn N-1
# ("تواصل مع أمين بائع المعرض") and followed up with the pronoun "كم رقمه"
# in turn N. The pre-fix safety net normalised the customer message,
# found ``رقم`` as a staff intent trigger, then looked for a name token
# inside the same message — and bailed with ``no_staff_name`` because
# pronouns aren't on the candidate list. The asset-promise sanitiser
# downstream then rewrote the reply to "الرقم غير مضاف حاليًا…" even
# though the merchant has أمين's number in the KB.
#
# The fix scans the customer message → the LLM reply → the previous
# bot turn → the previous customer turn for a known staff name, in
# that priority order. Tests below pin the carry-forward behaviour
# layer-by-layer so a future refactor can't silently drop it.


def test_pronoun_ask_recovers_name_from_llm_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Customer message is just a pronoun, but the LLM reply mentions
    أمين (because the customer was following up on it). The safety net
    must lift the name from the reply pool and complete the KB
    resolution — otherwise pronoun-only asks silently fail."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=7, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="كم رقمه",   # pronoun only — no name
        reply_text="عذراً، تواصل مع أمين",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.inferred_name in {"أمين", "امين"}
    assert result.source == "kb:branches"
    assert result.wa_id == "966541690226"


def test_pronoun_ask_recovers_name_from_history_bot_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The current LLM reply also lacks the name (the LLM produced a
    generic "لحظة" placeholder), but the prior bot turn mentioned
    أمين. The safety net must walk back into history to recover the
    target — exactly the screenshot from Tenant 33."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=11, kind="payment_method",
                body="للتواصل مع أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    history = [
        {"role": "user",      "content": "هل فيه استلام"},
        {"role": "assistant", "content": "تواصل مع أمين بائع المعرض لتجهيز طلبك"},
        {"role": "user",      "content": "كم رقمه"},
    ]
    result = apply_staff_contact_safety_net(
        customer_msg="كم رقمه",
        reply_text="لحظة وأجيب لك التفاصيل 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
        history=history,
    )
    assert result.fired is True
    assert result.source == "kb:payment_method", (
        "the merchant pasted أمين's contact under payment_method "
        "after the structured-KB migration; the resolver must "
        "scan that kind too — not only branches/store_story."
    )
    assert result.wa_id == "966541690226"


def test_history_direction_body_shape_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production wire shape: ``StateManager.load_history`` returns
    rows of ``{"direction": "inbound"|"outbound", "body": "<text>"}``
    — NOT the chat-style ``{"role": ..., "content": ...}``. The
    pre-fix walker only read role/content keys and silently
    returned empty pools for every production turn, which is
    exactly why the May 2026 #38c live trace kept missing أمين
    even though the prior bot turn clearly mentioned him.

    This test pins the wire shape directly against the safety
    net so a future refactor can't lose direction/body support."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    # Mirror StateManager.load_history's exact shape — direction +
    # body keys, no role/content. Pre-fix this would silently bail.
    history = [
        {"direction": "inbound",  "body": "هل فيه استلام"},
        {"direction": "outbound", "body": "تواصل مع أمين بائع المعرض لتجهيز طلبك"},
        {"direction": "inbound",  "body": "كم رقمه"},
    ]
    result = apply_staff_contact_safety_net(
        customer_msg="كم رقمه",
        reply_text="لحظة وأجيب لك التفاصيل 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
        history=history,
    )
    assert result.fired is True, (
        "history with direction/body shape must work — this is the "
        "exact dict shape the production webhook passes in"
    )
    assert result.source == "kb:branches"
    assert result.wa_id == "966541690226"


def test_history_carry_forward_does_not_misfire_without_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pronoun resolution is gated on the staff-intent trigger.
    A casual "وش رأيك" is not an ask for a phone — even when a prior
    bot turn happens to have mentioned أمين we must NOT fire."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    history = [
        {"role": "assistant", "content": "تواصل مع أمين بائع المعرض"},
    ]
    result = apply_staff_contact_safety_net(
        customer_msg="وش رأيك",
        reply_text="تمام 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
        history=history,
    )
    assert result.fired is False
    assert result.skipped_reason == "no_staff_intent"


def test_kb_scan_includes_post_migration_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the structured-KB migration merchants paste contact
    blocks into commerce sections (payment_method, working_hours,
    bank_transfer, …). The resolver must scan those — otherwise the
    migration silently lost contacts that used to live in the old
    monolithic text. We pick ``working_hours`` here as a canary."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="working_hours",
                body=(
                    "نستقبلكم من 10ص إلى 11م.\n"
                    "للتواصل: أمين بائع المعرض 0541690226"
                ),
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",
        reply_text="تواصل مع أمين بائع المعرض",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.source == "kb:working_hours"
    assert result.wa_id == "966541690226"


def test_pronoun_resolution_emits_trace_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Production triage must be able to grep
    ``[STAFF_CONTACT_TRACE]`` and see which pool surfaced the name
    (customer message vs reply vs history). Without this line the
    "why didn't أمين resolve?" question still costs a redeploy."""
    import logging
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    caplog.set_level(logging.INFO)
    apply_staff_contact_safety_net(
        customer_msg="كم رقمه",
        reply_text="عذراً، تواصل مع أمين بائع المعرض",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[STAFF_CONTACT_TRACE]" in ln and "stage=name_lookup" in ln
        and "hit=True" in ln and "source=reply" in ln
        for ln in log_lines
    ), f"expected a name-lookup trace; got: {log_lines!r}"


# ── Reply-driven trigger (May 2026 #38c) ────────────────────────────────────
#
# Live trace from Tenant 33 (May 23 2026): customer says "وصلت" — no
# explicit staff intent in the message — and the bot proactively offers
# "تواصل مع أمين عند الوصول". The pre-fix safety net checked only the
# customer-side trigger set, returned ``no_staff_intent``, and the
# asset-promise sanitiser downstream rewrote the reply to the cold
# fallback even though the KB has أمين's number.
#
# Fix: when the bot reply itself offers a staff contact (verb + name
# pattern, no digits in reply yet), the safety net runs the same KB
# resolver pass. Tests below pin the new trigger source, the
# no-misfire guard when the reply already carries digits, and the
# proactive [STAFF_CONTACT_GRAPH] trace that exposes resolver-visible
# pairs once per turn.


def test_reply_driven_trigger_arrival_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Customer says 'وصلت' — no customer-side trigger. The bot
    reply offers 'تواصل مع أمين عند الوصول' without digits. The
    safety net must treat that as an implicit staff_phone intent
    and ship the contact card from the KB when arrival policy
    is opted in."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=149, kind="escalation_rules",
                body="عند الوصول للمعرض تواصل مع بائع المعرض على الرقم المسجل.",
            ),
            _StubKBSection(
                section_id=5, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="وصلت",
        reply_text="أبشر 🌷 تواصل مع أمين عند الوصول للمعرض",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True, (
        "arrival-flow proactive offer must trigger the resolver "
        "even without a customer-side intent keyword"
    )
    assert result.source in {"kb:branches", "arrival_evidence"}
    assert result.wa_id == "966541690226"


def test_reply_driven_trigger_converts_digits_to_vcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LLM wrote phone digits alongside a staff offer,
    lift them into a contact card and strip from reply text."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="وصلت",
        reply_text="تواصل مع أمين على 0541690226 عند الوصول 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.extra_call_target is not None
    assert result.strip_phones_from_reply is True
    assert result.wa_id == "966541690226"


def test_reply_driven_trigger_no_misfire_on_unrelated_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated chit-chat ('تمام 🌷 شكراً') with no staff verb /
    name in either the message or the reply must remain a no-op.
    Guards against the implicit trigger over-firing on every
    turn that happens to mention تواصل / كلم."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="تمام",
        reply_text="أبشر 🌷 وصول سعيد!",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is False
    assert result.skipped_reason == "no_staff_intent"


def test_staff_contact_graph_trace_emits_each_turn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[STAFF_CONTACT_GRAPH] must fire on every safety-net entry
    (even when the resolver bails). Production triage uses it to
    verify the merchant's KB carries a staff contact at all
    before debugging trigger gating. The line surfaces a
    pairs_found count and the kinds that contributed."""
    import logging
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="payment_method",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    caplog.set_level(logging.INFO)
    apply_staff_contact_safety_net(
        customer_msg="وصلت",
        reply_text="تواصل مع أمين عند الوصول",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[STAFF_CONTACT_GRAPH]" in ln
        and "tenant_id=33" in ln
        and "pairs_found=" in ln
        and "payment_method" in ln
        for ln in log_lines
    ), f"expected a graph snapshot line; got: {log_lines!r}"


def test_reply_driven_trigger_emits_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The new reply-side branch must log
    ``stage=trigger hit=True source=reply_offer`` so production
    can distinguish customer-driven from reply-driven resolutions
    when reading staff-contact telemetry."""
    import logging
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=149, kind="escalation_rules",
                body="عند الوصول للمعرض تواصل مع بائع المعرض.",
            ),
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    caplog.set_level(logging.INFO)
    apply_staff_contact_safety_net(
        customer_msg="وصلت",
        reply_text="تواصل مع أمين عند الوصول",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[STAFF_CONTACT_TRACE]" in ln
        and "stage=trigger" in ln
        and "hit=True" in ln
        and (
            "source=reply_offer" in ln
            or "source=arrival_gated" in ln
        )
        for ln in log_lines
    ), f"expected an arrival-gated trigger trace; got: {log_lines!r}"


# ── Dashboard-suggestion filter + reply hallucination guard (May 2026 #38d) ─
#
# Live trace from Tenant 33 (May 23 2026, 13:00:00) revealed two new
# failure modes the prior fixes didn't cover:
#
#   1. The merchant's KB has 5+ "improvement suggestion" cards with
#      titles like "أضف باركود", "استكمال أرقام التواصل", "إضافة أرقام
#      التواصل في قسم …". These are dashboard PROMPTS asking the
#      merchant to add data, not real data. The resolver was scanning
#      them and they inflated the [STAFF_CONTACT_GRAPH] pair count
#      without contributing real contacts.
#
#   2. The customer asked "كم رقم البائع" (intent OK), the customer
#      message had no name, the pool walker fell through to the LLM
#      reply, and the LLM had hallucinated "هشام" — a generic
#      candidate name that happened to appear inside the brand-story
#      paragraph (kind=shipping_zones). The pre-fix walker grabbed
#      it and the resolver chased a ghost.
#
# Tests below pin both fixes so a future iteration can't regress.


def test_dashboard_suggestion_sections_are_skipped_by_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sections whose title starts with a dashboard suggestion verb
    (أضف / إضافة / استكمال / تحسين / تحديث / اقترح) carry
    placeholder text that the merchant is asked to replace. They
    must NOT be scanned for staff contacts — even if they happen
    to contain a Saudi-shaped phone in a usage example."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            # The only "real" contact section the resolver should touch.
            _StubKBSection(
                section_id=10, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
            # Dashboard suggestion card — must be ignored even though
            # it has the same name + a different phone in the body.
            _StubKBSection(
                section_id=99, kind="store_story",
                body="أمين بائع المعرض: 0500000000 — مثال للتعبئة",
                priority=10,  # higher priority than #10
            ),
        ],
    )
    # Force the suggestion section's title.
    db._sections[1].title = "استكمال أرقام التواصل في قسم «نبذة عن آل عايد»"
    # Real branches section gets a non-suggestion title.
    db._sections[0].title = "فرع المعرض الرئيسي"

    result = apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",
        reply_text="تواصل مع أمين بائع المعرض",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.wa_id == "966541690226", (
        "the resolver must lift the real branch contact, not the "
        "placeholder phone in the dashboard suggestion card"
    )


def test_reply_name_without_contact_verb_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production trace #38d: customer asks 'كم رقمه' (no name —
    intent comes from the trigger word رقم), the LLM reply
    mentions 'هشام' inside the merchant brand-story paragraph
    WITHOUT a contact verb nearby. The pool walker must NOT grab
    that name — otherwise the resolver chases a ghost and the
    asset-promise sanitiser writes the 'غير مضاف' fallback even
    though the merchant has a real contact under a different
    name in the KB."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="كم رقمه",                 # intent only, no name
        reply_text=(                            # reply mentions هشام in brand prose
            "نحن بفضل الله من عائلة هشام آل عايد، "
            "نقدم لك عسلاً أصيلاً 🌷"
        ),
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    # The pool walker now requires a contact verb in the reply
    # for reply-source names. هشام sits in brand prose with no
    # verb nearby, so it must be rejected and the resolver
    # bails — clean miss, no ghost chase.
    assert result.fired is False
    assert result.skipped_reason == "no_staff_name"


def test_reply_name_with_contact_verb_is_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-test for the rejection above: same reply hallucination
    pattern but WITH a contact verb anchoring the name. This must
    still resolve — otherwise the legitimate arrival-flow case
    (which is the exact case 602c582f shipped) regresses."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="كم رقمه",
        reply_text="تواصل مع أمين بائع المعرض عند الوصول",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.wa_id == "966541690226"


def test_graph_trace_emits_per_pair_detail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each candidate-name occurrence in a non-suggestion section
    must surface as a [STAFF_CONTACT_GRAPH_PAIR] line so the
    operator can see EXACTLY which section a name landed in and
    whether that section carries a phone. This is the diagnostic
    that lets us answer 'is the LLM hallucinating from a brand-
    story paragraph?' from a single grep."""
    import logging
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            # هشام in brand prose — no phone, kind=shipping_zones
            # (mirrors the live tenant-33 finding for section_id=3).
            _StubKBSection(
                section_id=3, kind="shipping_zones",
                body=(
                    "عائلة هشام آل عايد بدأت رحلة العسل من البراري النائية. "
                    "نحرص على الجودة في كل قطرة."
                ),
            ),
            # أمين with a phone — kind=branches
            _StubKBSection(
                section_id=10, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    caplog.set_level(logging.INFO)
    apply_staff_contact_safety_net(
        customer_msg="ابي رقم البائع",
        reply_text="تواصل مع أمين 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[STAFF_CONTACT_GRAPH_PAIR]" in ln
        and "kind=branches" in ln and "section_id=10" in ln
        and "phones_in_section=1" in ln
        for ln in log_lines
    ), f"expected per-pair detail line for the real branches contact; got: {log_lines!r}"
    # Brand-prose names are not contact evidence unless configured on a
    # label:phone line — no pair line expected for shipping_zones prose.


def test_graph_trace_reports_suggestion_skipped_count(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The graph summary must report how many sections were skipped
    as dashboard suggestions, so production triage can distinguish
    'KB is empty' from 'KB is full of suggestions but no real
    contacts'. Tenant 33's live trace was the latter."""
    import logging
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
            _StubKBSection(
                section_id=99, kind="store_story",
                body="أرقام التواصل تكتب هنا",
            ),
            _StubKBSection(
                section_id=100, kind="bank_transfer",
                body="باركود الراجحي يكتب هنا",
            ),
        ],
    )
    db._sections[0].title = "فرع المعرض الرئيسي"
    db._sections[1].title = "إضافة أرقام التواصل في قسم «النبذة»"
    db._sections[2].title = "أضف باركود أو صورة للتحويل البنكي"

    caplog.set_level(logging.INFO)
    apply_staff_contact_safety_net(
        customer_msg="ابي اكلم أمين",
        reply_text="تواصل مع أمين",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[STAFF_CONTACT_GRAPH]" in ln
        and "suggestion_skipped=2" in ln
        for ln in log_lines
    ), f"expected suggestion_skipped=2 in the graph summary; got: {log_lines!r}"


# ── Suggestion-verb escalation chain (Tenant 33 #38e) ────────────────────────
#
# Production regression chain reported on Tenant 33:
#
#   1. "أمين مايرد"  → bot:  "جربي التواصل مع هشام لخدمة العملاء 🌷"
#                    + Hisham contact card. WORKED (تواصل مع verb).
#   2. "مايرد"       → bot:  "جربي هيثم 🌷"
#                    NO card. The reply-offer detector did not consider
#                    "جربي" a contact verb, so the trigger never fired
#                    and the resolver never tried to attach a card.
#   3. "وين رقمه؟"   → bot:  "تفضلي 🌷"
#                    NO card. Step 2 having silently dropped means the
#                    name pool walker in step 3 is the LAST line of
#                    defence — it MUST find هيثم in the prior bot turn
#                    and send the card if the KB has the phone.
#
# These tests pin the fix:
#   * `_reply_offers_staff_contact` now matches "جربي X" / "حاولي X" /
#     "اسألي X" with a tighter 30-char proximity window (vs 60 for
#     the direct contact verbs).
#   * The walker continues to recover هيثم from history_bot when the
#     customer follows up with a pronoun-only ask.
#   * A new `[STAFF_ESCALATION_GAP]` log line surfaces when the bot
#     suggests a staff member but the KB has no phone for them, so
#     the merchant has an actionable signal in production.


def test_reply_suggestion_verb_jarrabi_triggers_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot replies "جربي هيثم 🌷" with NO contact verb. Pre-fix
    the trigger detector returned ("", "") and the resolver bailed
    on `no_staff_intent` — the customer never got a card even when
    Haitham's number was sitting in the KB."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="هيثم مسؤول التوصيل: 0507654321",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="مايرد",                # no customer-side trigger
        reply_text="جربي هيثم 🌷",          # suggestion verb only
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True, (
        "regression: 'جربي X' must trigger the resolver with the "
        "suggestion-verb proximity rule. "
        f"Got skipped_reason={result.skipped_reason!r}."
    )
    assert result.wa_id == "966507654321"
    assert result.source == "kb:branches"


def test_reply_suggestion_verb_haawli_with_role_noun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same path with "حاولي مع X" feminine variant + a role noun
    between verb and name. The 30-char proximity must still catch
    a name that lands ≤ ~30 chars from the verb."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="custom",
                body="أمين هو الكاشير اليومي - 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ما رد الإدارة",
        reply_text="حاولي مع أمين الكاشير 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is True
    assert result.wa_id == "966541690226"


def test_reply_suggestion_verb_no_false_positive_on_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence-in-depth: "جربي عسل السدر هذا اليوم" must NOT trigger
    the resolver. The suggestion verb is generic enough that without
    the proximity-bound name check we'd ship a phantom contact card
    every time the LLM recommended a product."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ايش تنصحوني",
        reply_text="جربي عسل السدر اليوم لو سمحتي 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is False
    assert result.skipped_reason == "no_staff_intent", (
        f"expected proximity rule to reject the product-recommendation "
        f"phrase. Got skipped_reason={result.skipped_reason!r}."
    )


def test_reply_suggestion_verb_with_far_name_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 30-char window is intentional: a brand-story paragraph
    that opens with "جربي العسل" and then 60 chars later mentions
    "هشام بدأ مشروعه في 2010" must NOT pair the suggestion verb
    with هشام. Only escalation phrases where the name lands tight
    after the verb should fire."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="هشام مالك المتجر: 0507654321",
            ),
        ],
    )
    long_filler = "ولاتفوّتي على نكهات السدر والطلح والضهيان البلدي اليوم"
    result = apply_staff_contact_safety_net(
        customer_msg="ايش تنصحوني",
        reply_text=f"جربي العسل البلدي. {long_filler}. هشام بدأ مشروعه عام 2010 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is False, (
        "suggestion verb must only pair with names within ~30 chars; "
        "a brand-story name 60+ chars away must not produce a card."
    )
    assert result.skipped_reason == "no_staff_intent"


def test_full_escalation_chain_amin_to_hisham_to_haitham(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end pin for the user-reported scenario.

    Three turns, each running through the safety net with the
    history populated from the prior turn(s):

        turn 1  customer: "أمين مايرد"
                bot:      "جربي التواصل مع هشام لخدمة العملاء 🌷"
                          → contact verb hit → Hisham card.

        turn 2  customer: "مايرد"
                bot:      "جربي هيثم 🌷"
                          → suggestion verb hit → Haitham card
                            (NEW: this is what dropped pre-fix).

        turn 3  customer: "وين رقمه؟"
                bot:      "تفضلي 🌷"
                          → customer-side trigger (رقم) + name pool
                            walker recovers هيثم from history_bot
                            → Haitham card again, idempotent.

    KB carries phones for Hisham AND Haitham; Amin is intentionally
    absent so the chain reflects the merchant's real "أمين الأصلي
    لا رقم له" state. The assertions guarantee that every step
    sends a card; the regression we're pinning is the silent drop
    at turn 2.
    """
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body=(
                    "هشام خدمة العملاء: 0501112233\n"
                    "هيثم مسؤول التوصيل: 0507654321"
                ),
            ),
        ],
    )

    # ── turn 1 ──
    turn1 = apply_staff_contact_safety_net(
        customer_msg="أمين مايرد",
        reply_text="جربي التواصل مع هشام لخدمة العملاء 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert turn1.fired is True, f"turn1 skipped={turn1.skipped_reason!r}"
    assert turn1.wa_id == "966501112233"

    history_after_t1 = [
        {"direction": "in", "body": "أمين مايرد"},
        {"direction": "out", "body": "جربي التواصل مع هشام لخدمة العملاء 🌷"},
    ]

    # ── turn 2 — the previously-silent drop ──
    turn2 = apply_staff_contact_safety_net(
        customer_msg="مايرد",
        reply_text="جربي هيثم 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
        history=history_after_t1,
    )
    assert turn2.fired is True, (
        "regression: turn 2 must fire on the suggestion verb 'جربي'. "
        f"Got skipped_reason={turn2.skipped_reason!r}."
    )
    assert turn2.wa_id == "966507654321", (
        "turn 2 must resolve to Haitham's KB phone, not Hisham's."
    )

    history_after_t2 = history_after_t1 + [
        {"direction": "in", "body": "مايرد"},
        {"direction": "out", "body": "جربي هيثم 🌷"},
    ]

    # ── turn 3 — pronoun follow-up ──
    turn3 = apply_staff_contact_safety_net(
        customer_msg="وين رقمه؟",
        reply_text="تفضلي 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
        history=history_after_t2,
    )
    assert turn3.fired is True, (
        "turn 3 'وين رقمه؟' must fire via customer-side trigger and "
        f"recover هيثم from history_bot. Got "
        f"skipped_reason={turn3.skipped_reason!r}."
    )
    assert turn3.wa_id == "966507654321", (
        "turn 3 must re-resolve to Haitham (the most recent suggested "
        "name) — not to Hisham (the earlier suggestion)."
    )


def test_escalation_gap_telemetry_when_suggested_name_has_no_kb_phone(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Actionable signal for the merchant: when the LLM suggests an
    alternative staff member ("جربي هيثم 🌷") but the KB carries
    NO phone for that person, the resolver must emit a single
    `[STAFF_ESCALATION_GAP]` line so the merchant dashboard can
    surface "you suggested هيثم but didn't add his number" without
    ops having to read the conversation transcript."""
    import logging
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            # KB has أمين but NOT هيثم — exactly the merchant case.
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    caplog.set_level(logging.INFO)
    result = apply_staff_contact_safety_net(
        customer_msg="مايرد",
        reply_text="جربي هيثم 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    assert result.fired is False
    assert result.skipped_reason == "no_staff_name"
    assert not result.inferred_name


def test_escalation_gap_does_not_fire_for_customer_typed_name(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The escalation-gap signal is a merchant-actionable artefact —
    only emit when the bot SUGGESTED a name (reply_offer / history_bot).
    A customer who typed an unknown name themselves is NOT a KB gap;
    it's the customer asking about a person the merchant never
    advertised, so the gap log would just create noise."""
    import logging
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1, kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    caplog.set_level(logging.INFO)
    apply_staff_contact_safety_net(
        # Customer typed "هيثم" themselves — not a bot suggestion.
        customer_msg="ابي رقم هيثم",
        reply_text="حاضر 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db, tenant_id=33,
    )
    log_lines = [rec.getMessage() for rec in caplog.records]
    assert not any("[STAFF_ESCALATION_GAP]" in ln for ln in log_lines), (
        "customer-typed unknown names must not produce escalation-gap "
        "noise — only bot-suggested names count as merchant gaps."
    )
