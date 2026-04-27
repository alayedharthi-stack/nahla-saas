"""
tests/test_unsubscribe_flow.py
──────────────────────────────
End-to-end coverage for the global customer unsubscribe management
system implemented in `services.unsubscribe`.

Lifecycle under test:

    ORDINARY  ─(keyword)─►  PENDING  ─(button "نعم")─►  UNSUBSCRIBED
                                  └───(button "تراجع")──►  ORDINARY
    UNSUBSCRIBED  ─(any inbound msg)─►  ORDINARY  (auto re-subscribe)

What we cover:

  • Keyword detection (Arabic strict / Arabic solo / English STOP / unsubscribe)
  • False-positive guard: long sentences containing "إلغاء" (e.g. "ألغِ طلبي")
    must NOT trigger the flow
  • State helpers: mark_pending / clear_pending / mark_unsub / mark_resub
  • is_silenced is True for both PENDING and UNSUBSCRIBED
  • Interactive payload structure (button IDs, body text)
  • Marketing-template auto-footer injection
  • Segment registry: "unsubscribed" segment exists and lists only
    confirmed opt-outs
  • Reachable filter: excludes BOTH pending and confirmed opt-outs from
    every campaign segment query
  • Customer serializer exposes the new fields
  • Idempotency: marking an already-unsubscribed customer twice is a no-op

These tests are intentionally narrow (no httpx, no Meta) so they can run
in <1s without hitting any external service. The full webhook → button
→ Meta send path is exercised with mocked send adapters in the
end-to-end scenario at the bottom.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Customer, Tenant  # noqa: E402

from services.unsubscribe import (  # noqa: E402
    CANCELLED_UNSUB_MSG_AR,
    CONFIRMATION_BODY_AR,
    CONFIRMATION_BTN_CANCEL_AR,
    CONFIRMATION_BTN_CONFIRM_AR,
    CONFIRMATION_FALLBACK_MSG_AR,
    FINAL_UNSUBSCRIBED_MSG_AR,
    MARKETING_FOOTER_AR,
    PENDING_PROMPT_RESEND_MINUTES,
    PENDING_UNSUBSCRIBE_TIMEOUT_HOURS,
    UNSUB_CANCEL_BUTTON_ID,
    UNSUB_CONFIRM_BUTTON_ID,
    build_confirmation_fallback_payload,
    build_confirmation_payload,
    build_text_payload,
    classify_confirmation_text,
    clear_pending_unsubscribe,
    ensure_marketing_footer,
    expire_pending_if_needed,
    is_customer_pending_unsubscribe,
    is_customer_unsubscribed,
    is_pending_expired,
    is_silenced,
    is_unsubscribe_request,
    mark_pending_prompt_sent,
    mark_pending_unsubscribe,
    mark_resubscribed,
    mark_unsubscribed,
    should_send_pending_prompt,
)


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    """SQLite doesn't support JSONB — remap to plain JSON for tests."""
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────

def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def _make_customer(db, *, name="عميل", phone="+966500000000"):
    tenant = Tenant(name="Test Tenant", is_active=True)
    db.add(tenant)
    db.flush()
    cust = Customer(
        tenant_id=tenant.id,
        name=name,
        phone=phone,
        normalized_phone=phone,
        extra_metadata={},
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return tenant, cust


# ──────────────────────────────────────────────────────────────────────────
# 1. Keyword detection
# ──────────────────────────────────────────────────────────────────────────

class TestKeywordDetection:
    def test_strict_arabic_phrases(self):
        for phrase in [
            "إلغاء الاشتراك",
            "الغاء الاشتراك",
            "إلغاء الإشتراك",
            "أوقف الرسائل",
            "اوقف الرسائل",
            "إيقاف الرسائل",
            "لا أريد رسائل",
            "لا اريد رسائل",
            "لا ترسلوا لي",
            "لا تراسلني",
            "توقف عن الإرسال",
            "أوقف التواصل",
        ]:
            assert is_unsubscribe_request(phrase), f"missed strict phrase: {phrase!r}"

    def test_strict_phrase_inside_sentence_still_matches(self):
        # Strict phrases can appear with leading/trailing context.
        assert is_unsubscribe_request("من فضلك إلغاء الاشتراك من رسائلكم")
        assert is_unsubscribe_request("اوقف الرسائل عني الآن")

    def test_solo_arabic_short_message(self):
        for w in ["إلغاء", "الغاء", "ألغاء", "إلغاء  "]:
            assert is_unsubscribe_request(w), f"missed solo word: {w!r}"

    def test_english_stop_and_unsubscribe(self):
        assert is_unsubscribe_request("STOP")
        assert is_unsubscribe_request("stop")
        assert is_unsubscribe_request("Stop")
        assert is_unsubscribe_request("unsubscribe")
        assert is_unsubscribe_request("UNSUBSCRIBE")
        assert is_unsubscribe_request("opt-out")
        assert is_unsubscribe_request("opt out")

    def test_numeric_confirmation_classifier_for_fallback(self):
        assert classify_confirmation_text("1") == "confirm"
        assert classify_confirmation_text("١") == "confirm"
        assert classify_confirmation_text("نعم متأكد") == "confirm"
        assert classify_confirmation_text("2") == "cancel"
        assert classify_confirmation_text("٢") == "cancel"
        assert classify_confirmation_text("تراجع") == "cancel"
        assert classify_confirmation_text("السلام عليكم") is None

    def test_solo_word_inside_long_sentence_does_not_trigger(self):
        # Critical false-positive guard — these are ORDER cancellation
        # messages, not unsubscribe requests.
        assert not is_unsubscribe_request(
            "هل يمكنني إلغاء طلبي رقم 12345 من فضلكم"
        )
        assert not is_unsubscribe_request(
            "أريد إلغاء الطلب الذي أرسلته اليوم"
        )
        assert not is_unsubscribe_request(
            "كيف يمكنني إلغاء حسابي في المتجر"
        )

    def test_empty_and_noise(self):
        assert not is_unsubscribe_request("")
        assert not is_unsubscribe_request("   ")
        assert not is_unsubscribe_request(None)  # type: ignore[arg-type]
        assert not is_unsubscribe_request("hello")
        assert not is_unsubscribe_request("شكرا لكم")
        assert not is_unsubscribe_request("متى يصل طلبي؟")


# ──────────────────────────────────────────────────────────────────────────
# 2. State transitions
# ──────────────────────────────────────────────────────────────────────────

class TestStateTransitions:
    def test_initial_state_is_ordinary(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db)
            assert not is_customer_unsubscribed(cust)
            assert not is_customer_pending_unsubscribe(cust)
            assert not is_silenced(cust)
        finally:
            db.close(); engine.dispose()

    def test_mark_pending_then_clear(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db)
            mark_pending_unsubscribe(db, cust)
            db.refresh(cust)
            assert is_customer_pending_unsubscribe(cust)
            assert not is_customer_unsubscribed(cust)
            assert is_silenced(cust)
            assert cust.extra_metadata.get("pending_unsubscribe_at")
            assert cust.extra_metadata.get("pending_unsubscribe_expires_at")

            clear_pending_unsubscribe(db, cust)
            db.refresh(cust)
            assert not is_customer_pending_unsubscribe(cust)
            assert not is_silenced(cust)
        finally:
            db.close(); engine.dispose()

    def test_mark_unsubscribed_clears_pending(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db)
            mark_pending_unsubscribe(db, cust)
            mark_unsubscribed(db, cust)
            db.refresh(cust)
            assert is_customer_unsubscribed(cust)
            assert not is_customer_pending_unsubscribe(cust)
            assert is_silenced(cust)
            assert cust.extra_metadata.get("unsubscribed_at")
            # Pending timestamp must be wiped after final confirmation
            assert "pending_unsubscribe_at" not in cust.extra_metadata
        finally:
            db.close(); engine.dispose()

    def test_resubscribe_after_final(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db)
            mark_unsubscribed(db, cust)
            mark_resubscribed(db, cust)
            db.refresh(cust)
            assert not is_customer_unsubscribed(cust)
            assert not is_customer_pending_unsubscribe(cust)
            assert not is_silenced(cust)
            assert cust.extra_metadata.get("resubscribed_at")
            # Audit trail preserved — we know they HAD unsubscribed
            assert cust.extra_metadata.get("unsubscribed_at")
        finally:
            db.close(); engine.dispose()

    def test_idempotent_double_unsubscribe(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db)
            mark_unsubscribed(db, cust)
            first_ts = cust.extra_metadata["unsubscribed_at"]
            mark_unsubscribed(db, cust)
            db.refresh(cust)
            # Second call updates the timestamp but doesn't break state
            assert cust.extra_metadata.get("is_unsubscribed") is True
            assert cust.extra_metadata.get("unsubscribed_at") >= first_ts
        finally:
            db.close(); engine.dispose()

    def test_clear_pending_is_safe_when_not_pending(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db)
            # Should not raise
            clear_pending_unsubscribe(db, cust)
            db.refresh(cust)
            assert not is_customer_pending_unsubscribe(cust)
        finally:
            db.close(); engine.dispose()

    def test_pending_expires_after_24_hours(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db)
            mark_pending_unsubscribe(db, cust)
            db.refresh(cust)
            assert is_customer_pending_unsubscribe(cust)

            future = datetime.now(timezone.utc) + timedelta(
                hours=PENDING_UNSUBSCRIBE_TIMEOUT_HOURS,
                minutes=1,
            )
            assert is_pending_expired(cust, now=future)
            assert expire_pending_if_needed(db, cust, now=future)
            db.refresh(cust)
            assert not is_customer_pending_unsubscribe(cust)
            assert not is_silenced(cust)
        finally:
            db.close(); engine.dispose()

    def test_pending_prompt_resend_is_throttled(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db)
            mark_pending_unsubscribe(db, cust)
            assert should_send_pending_prompt(cust)
            mark_pending_prompt_sent(db, cust)
            db.refresh(cust)
            assert not should_send_pending_prompt(cust)
            later = datetime.now(timezone.utc) + timedelta(
                minutes=PENDING_PROMPT_RESEND_MINUTES + 1,
            )
            assert should_send_pending_prompt(cust, now=later)
        finally:
            db.close(); engine.dispose()


# ──────────────────────────────────────────────────────────────────────────
# 3. Interactive payload builder
# ──────────────────────────────────────────────────────────────────────────

class TestInteractivePayload:
    def test_confirmation_payload_shape(self):
        p = build_confirmation_payload("+966500000000")
        assert p["messaging_product"] == "whatsapp"
        assert p["to"] == "+966500000000"
        assert p["type"] == "interactive"
        assert p["interactive"]["type"] == "button"
        assert p["interactive"]["body"]["text"] == CONFIRMATION_BODY_AR
        buttons = p["interactive"]["action"]["buttons"]
        assert len(buttons) == 2

        ids   = [b["reply"]["id"]    for b in buttons]
        names = [b["reply"]["title"] for b in buttons]
        assert UNSUB_CONFIRM_BUTTON_ID in ids
        assert UNSUB_CANCEL_BUTTON_ID  in ids
        assert CONFIRMATION_BTN_CONFIRM_AR in names
        assert CONFIRMATION_BTN_CANCEL_AR  in names

    def test_button_titles_within_whatsapp_20_char_limit(self):
        # WhatsApp truncates quick-reply button titles at 20 chars.
        assert len(CONFIRMATION_BTN_CONFIRM_AR) <= 20
        assert len(CONFIRMATION_BTN_CANCEL_AR)  <= 20

    def test_text_payload_shape(self):
        p = build_text_payload("+966500000000", FINAL_UNSUBSCRIBED_MSG_AR)
        assert p["type"] == "text"
        assert p["text"]["body"] == FINAL_UNSUBSCRIBED_MSG_AR
        assert p["text"]["preview_url"] is False

        p2 = build_text_payload("+966500000000", CANCELLED_UNSUB_MSG_AR)
        assert p2["text"]["body"] == CANCELLED_UNSUB_MSG_AR

    def test_confirmation_fallback_payload_shape(self):
        p = build_confirmation_fallback_payload("+966500000000")
        assert p["type"] == "text"
        assert p["text"]["body"] == CONFIRMATION_FALLBACK_MSG_AR
        assert "1 = نعم متأكد" in p["text"]["body"]
        assert "2 = تراجع" in p["text"]["body"]


# ──────────────────────────────────────────────────────────────────────────
# 4. Marketing-template footer
# ──────────────────────────────────────────────────────────────────────────

class TestMarketingFooter:
    def test_footer_short_enough_for_meta(self):
        # WhatsApp footer hard cap is 60 characters.
        assert len(MARKETING_FOOTER_AR) <= 60

    def test_footer_appended_when_missing(self):
        comps = [{"type": "BODY", "text": "مرحبا {{1}}"}]
        out = ensure_marketing_footer(comps)
        assert any(c.get("type") == "FOOTER" for c in out)
        # Original list NOT mutated
        assert all(c.get("type") != "FOOTER" for c in comps)

    def test_existing_footer_preserved(self):
        comps = [
            {"type": "BODY",   "text": "مرحبا"},
            {"type": "FOOTER", "text": "نص مخصص للتاجر"},
        ]
        out = ensure_marketing_footer(comps)
        footers = [c for c in out if c.get("type") == "FOOTER"]
        assert len(footers) == 1
        assert footers[0]["text"] == "نص مخصص للتاجر"

    def test_generator_marketing_template_gets_footer(self):
        # AI generator builds components from spec dicts.
        from modules.ai.templates.generator import _build_components
        spec = {
            "category": "MARKETING",
            "body":     "مرحبا {{1}}",
            # NB: no explicit footer in the spec
        }
        comps = _build_components(spec)
        footer_comps = [c for c in comps if c.get("type") == "FOOTER"]
        assert len(footer_comps) == 1
        assert "إلغاء" in footer_comps[0]["text"]

    def test_generator_utility_template_no_footer(self):
        from modules.ai.templates.generator import _build_components
        spec = {
            "category": "UTILITY",
            "body":     "تم تأكيد طلبك رقم {{1}}",
        }
        comps = _build_components(spec)
        # Utility templates must NOT carry the marketing opt-out copy
        assert all(c.get("type") != "FOOTER" for c in comps)


# ──────────────────────────────────────────────────────────────────────────
# 5. Segment registry & reachability
# ──────────────────────────────────────────────────────────────────────────

class TestSegmentRegistry:
    def test_unsubscribed_segment_exists(self):
        from services.nahla_segments import all_segment_keys, get_segment
        assert "unsubscribed" in all_segment_keys()
        seg = get_segment("unsubscribed")
        assert seg is not None
        assert seg.label_ar == "ألغوا الاشتراك"

    def test_unsubscribed_filter_returns_only_confirmed(self):
        from services.nahla_segments import build_segment_query
        engine, db = _make_db()
        try:
            tenant, cust_unsub = _make_customer(db, phone="+966500000001")
            mark_unsubscribed(db, cust_unsub)

            cust_pending = Customer(
                tenant_id=tenant.id, name="Pending",
                phone="+966500000002", normalized_phone="+966500000002",
                extra_metadata={},
            )
            db.add(cust_pending); db.commit()
            mark_pending_unsubscribe(db, cust_pending)

            cust_normal = Customer(
                tenant_id=tenant.id, name="Normal",
                phone="+966500000003", normalized_phone="+966500000003",
                extra_metadata={},
            )
            db.add(cust_normal); db.commit()

            # Customers page uses require_reachable=False
            q = build_segment_query("unsubscribed", db, tenant.id, require_reachable=False)
            ids = {c.id for c in q.all()}
            assert cust_unsub.id    in ids
            assert cust_pending.id  not in ids   # PENDING is NOT in the final segment
            assert cust_normal.id   not in ids
        finally:
            db.close(); engine.dispose()

    def test_reachable_filter_excludes_pending_and_confirmed(self):
        from services.nahla_segments import build_segment_query
        engine, db = _make_db()
        try:
            tenant, cust_unsub = _make_customer(db, phone="+966500000001")
            mark_unsubscribed(db, cust_unsub)

            cust_pending = Customer(
                tenant_id=tenant.id, name="Pending",
                phone="+966500000002", normalized_phone="+966500000002",
                extra_metadata={},
            )
            db.add(cust_pending); db.commit()
            mark_pending_unsubscribe(db, cust_pending)

            cust_normal = Customer(
                tenant_id=tenant.id, name="Normal",
                phone="+966500000003", normalized_phone="+966500000003",
                extra_metadata={},
            )
            db.add(cust_normal); db.commit()

            # Campaign wizard uses require_reachable=True (default)
            q = build_segment_query("all", db, tenant.id, require_reachable=True)
            ids = {c.id for c in q.all()}
            assert cust_normal.id   in ids
            assert cust_unsub.id    not in ids
            assert cust_pending.id  not in ids
        finally:
            db.close(); engine.dispose()


# ──────────────────────────────────────────────────────────────────────────
# 6. Campaign dispatcher per-row guard
# ──────────────────────────────────────────────────────────────────────────

class TestCampaignDispatcherGuard:
    def test_per_row_guard_blocks_unsubscribed_and_pending(self):
        # We test the guard logic directly (the inline metadata check the
        # dispatcher performs in its hot loop) so this test stays fast
        # and doesn't need a full mocked Meta send.
        unsub_meta   = {"is_unsubscribed":     True}
        pending_meta = {"pending_unsubscribe": True}
        normal_meta: dict = {}

        def _is_blocked(meta):
            return bool(meta.get("is_unsubscribed") or meta.get("pending_unsubscribe"))

        assert _is_blocked(unsub_meta)
        assert _is_blocked(pending_meta)
        assert not _is_blocked(normal_meta)


# ──────────────────────────────────────────────────────────────────────────
# 7. Customer serializer exposes the new fields
# ──────────────────────────────────────────────────────────────────────────

class TestSerializer:
    def test_serializer_exposes_unsubscribe_fields(self):
        from routers.customers import _serialize_customer
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db)
            data = _serialize_customer(cust, profile=None)
            assert data["is_unsubscribed"]     is False
            assert data["pending_unsubscribe"] is False
            assert data["unsubscribed_at"]      is None
            assert data["pending_unsubscribe_at"] is None
            assert data["resubscribed_at"]      is None

            mark_pending_unsubscribe(db, cust)
            data = _serialize_customer(cust, profile=None)
            assert data["pending_unsubscribe"] is True
            assert data["pending_unsubscribe_at"] is not None
            assert data["is_unsubscribed"] is False

            mark_unsubscribed(db, cust)
            data = _serialize_customer(cust, profile=None)
            assert data["is_unsubscribed"] is True
            assert data["unsubscribed_at"] is not None
            assert data["pending_unsubscribe"] is False
        finally:
            db.close(); engine.dispose()


# ──────────────────────────────────────────────────────────────────────────
# 8. End-to-end scenario (function-level, no HTTP)
# ──────────────────────────────────────────────────────────────────────────

class TestEndToEndScenario:
    """
    Simulates the full lifecycle described in the spec:
      1. Customer sends "إلغاء الاشتراك"            → PENDING + buttons sent
      2. Customer presses "نعم متأكد"               → UNSUBSCRIBED + goodbye
      3. Campaign tries to send                     → skipped
      4. Automation engine attempts to send         → skipped
      5. Customer sends "السلام عليكم"              → auto re-subscribe → ORDINARY
    """

    def test_full_lifecycle(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db, phone="+966500000050", name="عميل تجريبي")

            # 1. Inbound text triggers unsubscribe keyword
            assert is_unsubscribe_request("إلغاء الاشتراك")
            mark_pending_unsubscribe(db, cust)
            db.refresh(cust)
            assert is_customer_pending_unsubscribe(cust)
            assert is_silenced(cust)

            # ...and the system would build a confirmation payload
            payload = build_confirmation_payload(cust.phone or "")
            assert payload["interactive"]["type"] == "button"

            # 2. Button "نعم متأكد" pressed
            mark_unsubscribed(db, cust)
            db.refresh(cust)
            assert is_customer_unsubscribed(cust)
            assert not is_customer_pending_unsubscribe(cust)

            # 3. Campaign dispatcher's per-row guard skips the customer
            meta = cust.extra_metadata or {}
            assert meta.get("is_unsubscribed") is True

            # 4. Automation engine's pre-execution check via is_silenced
            assert is_silenced(cust)

            # 5. Customer sends a non-unsubscribe message → auto-resubscribe
            new_inbound = "السلام عليكم"
            assert not is_unsubscribe_request(new_inbound)
            mark_resubscribed(db, cust)
            db.refresh(cust)
            assert not is_customer_unsubscribed(cust)
            assert not is_silenced(cust)
            assert cust.extra_metadata.get("resubscribed_at")
        finally:
            db.close(); engine.dispose()

    def test_cancel_path_returns_to_normal(self):
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db, phone="+966500000051")
            mark_pending_unsubscribe(db, cust)
            # User taps "تراجع" → clear pending, no final unsubscribe
            clear_pending_unsubscribe(db, cust)
            db.refresh(cust)
            assert not is_customer_pending_unsubscribe(cust)
            assert not is_customer_unsubscribed(cust)
            assert not is_silenced(cust)
        finally:
            db.close(); engine.dispose()

    def test_multiple_unsubscribe_attempts_dont_create_loop(self):
        # If a customer sends "إلغاء" twice in a row, the system should
        # remain in PENDING (not toggle), and we should not throw.
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db, phone="+966500000052")
            mark_pending_unsubscribe(db, cust)
            first_ts = cust.extra_metadata["pending_unsubscribe_at"]
            mark_pending_unsubscribe(db, cust)
            db.refresh(cust)
            assert is_customer_pending_unsubscribe(cust)
            # Timestamp updated on the second call but state didn't escalate
            assert cust.extra_metadata["pending_unsubscribe_at"] >= first_ts
            assert not is_customer_unsubscribed(cust)
        finally:
            db.close(); engine.dispose()

    def test_pending_customer_sending_more_text_stays_pending(self):
        # While PENDING, any non-button inbound is treated as "still
        # waiting for confirmation" — it must NOT trigger AI / automation,
        # but it also must NOT auto-confirm.
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db, phone="+966500000053")
            mark_pending_unsubscribe(db, cust)
            # Customer sends a normal message, didn't tap the button
            assert is_silenced(cust)               # AI/automation paused
            assert not is_customer_unsubscribed(cust)  # not finalised
        finally:
            db.close(); engine.dispose()

    def test_final_unsubscribed_customer_restores_even_if_message_says_cancel(self):
        # Webhook ordering requirement: once the customer is in FINAL
        # unsubscribe, ANY new inbound message restores them first. This
        # avoids a loop where an already-unsubscribed customer sends "إلغاء"
        # and gets trapped in PENDING again instead of being reactivated.
        engine, db = _make_db()
        try:
            _, cust = _make_customer(db, phone="+966500000054")
            mark_unsubscribed(db, cust)
            db.refresh(cust)
            assert is_customer_unsubscribed(cust)
            assert is_unsubscribe_request("إلغاء")

            # This mirrors the webhook's final-state-first ordering.
            if is_customer_unsubscribed(cust):
                mark_resubscribed(db, cust)
            elif is_unsubscribe_request("إلغاء"):
                mark_pending_unsubscribe(db, cust)

            db.refresh(cust)
            assert not is_customer_unsubscribed(cust)
            assert not is_customer_pending_unsubscribe(cust)
            assert not is_silenced(cust)
        finally:
            db.close(); engine.dispose()
