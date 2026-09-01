"""
test_conversation_mode_controller.py
────────────────────────────────────
Verify the top-level conversation mode controller. The controller sits
ABOVE the Brain / legacy AI split and decides who owns the conversation
right now. The tests below are deliberately pure-function: we never boot
FastAPI or hit a real DB, we just feed fake Conversation / Order /
Customer / AutomationEvent objects into the controller and assert on
the returned ModeDecision.

Coverage:
    1. Identity / greeting detection switches to MODE_IDENTITY_REPLY
       even when prior owner was automation_recovery.
    2. Free-form reply during automation_recovery escalates to
       MODE_LIVE_CHAT (or a more specific live owner).
    3. Sticky LIVE_CHAT lease holds the conversation in live chat
       across subsequent turns and prevents bounce-back to recovery.
    4. Lease expiry restores normal mode resolution.
    5. Human handoff flag wins over everything else.
    6. Active recovery lineage with no inbound text keeps automation
       ownership.
    7. Safe fallback: missing/malformed lease returns sensible defaults.
    8. save_lease + load_lease roundtrip preserves the record.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from modules.ai.routing.conversation_mode import (  # noqa: E402
    META_KEY,
    MODE_AUTOMATION_RECOVERY,
    MODE_CHECKOUT_ASSIST,
    MODE_IDENTITY_REPLY,
    MODE_LIVE_CHAT,
    MODE_POST_PURCHASE,
    MODE_SUPPORT_ESCALATION,
    ModeLease,
    RecoverySnapshot,
    SOURCE_GREETING_DETECTED,
    SOURCE_IDENTITY_DETECTED,
    SOURCE_LEASE_HELD,
    detect_identity_topic,
    is_established_conversation,
    is_free_form_message,
    load_lease,
    resolve_conversation_mode,
    save_lease,
    should_apply_greeting_identity_card,
    should_use_greeting_fast_path,
)


# ── Test doubles ─────────────────────────────────────────────────────────────

class FakeConvo:
    """Stand-in for the SQLAlchemy Conversation row."""
    def __init__(
        self,
        *,
        extra_metadata=None,
        is_human_handoff=False,
        paused_by_human=False,
    ):
        self.id = 42
        self.extra_metadata = dict(extra_metadata or {})
        self.is_human_handoff = is_human_handoff
        self.paused_by_human = paused_by_human


class FakeDB:
    """Minimal fake DB. The controller only calls .add() / .flush() /
    .rollback() / .query() against it; we return None from query() so the
    recovery snapshot loader gets a clean 'no recovery' answer."""
    def __init__(self):
        self.added = []
        self.flushed = 0
        self.rolled_back = 0

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed += 1

    def rollback(self):
        self.rolled_back += 1

    def query(self, _model):  # pragma: no cover - patched per-test
        return self

    def filter(self, *_a, **_k):
        return self

    def order_by(self, *_a, **_k):
        return self

    def first(self):
        return None

    def limit(self, *_a, **_k):
        return self

    def all(self):
        return []


def _seed_lease(convo: FakeConvo, *, mode: str, locked_until: str = "") -> None:
    convo.extra_metadata[META_KEY] = {
        "mode":          mode,
        "previous_mode": "",
        "reason":        "seed",
        "source":        "seed",
        "changed_at":    datetime.now(timezone.utc).isoformat(),
        "locked_until":  locked_until,
    }


def _future(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _past(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _outbound_history():
    return [{"direction": "outbound", "body": "أهلاً فيك 🌷"}]


def _seed_brain_greeted(convo: FakeConvo) -> None:
    meta = dict(convo.extra_metadata or {})
    meta["brain_state"] = {"greeted": True}
    convo.extra_metadata = meta


# ══════════════════════════════════════════════════════════════════════════════
# Identity / greeting detection
# ══════════════════════════════════════════════════════════════════════════════

class TestIdentityDetection:
    @pytest.mark.parametrize("text", [
        "السلام عليكم",
        "السلام عليكم ورحمة الله",
        "هلا والله",
        "صباح الخير",
        "hi",
        "hello there",
    ])
    def test_greeting_detected(self, text):
        assert detect_identity_topic(text) == "greeting"

    @pytest.mark.parametrize("text", [
        "من أنت",
        "من انت",
        "مين أنت",
        "وش انت",
        "who are you",
        "what are you?",
    ])
    def test_identity_detected(self, text):
        assert detect_identity_topic(text) == "identity"

    @pytest.mark.parametrize("text", [
        "أبغى منتج جديد",
        "كم سعر القميص",
        "وين طلبي",
        "",
        "1",
    ])
    def test_non_identity_returns_empty(self, text):
        assert detect_identity_topic(text) == ""


# ══════════════════════════════════════════════════════════════════════════════
# Identity routing wins even after automation_recovery
# ══════════════════════════════════════════════════════════════════════════════

class TestIdentityOverridesRecovery:
    def test_who_are_you_during_recovery(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_AUTOMATION_RECOVERY)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="من انت",
        )

        assert decision.mode == MODE_IDENTITY_REPLY
        assert decision.identity_topic == "identity"
        assert decision.previous_mode == MODE_AUTOMATION_RECOVERY
        # Lease is for live_chat — identity reply is a single-turn answer
        # but the next turn must remain in live chat, not bounce to recovery.
        assert decision.lease.mode == MODE_LIVE_CHAT
        assert decision.lease.locked_until  # non-empty ISO timestamp

    def test_greeting_during_recovery(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_AUTOMATION_RECOVERY)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="السلام عليكم",
        )

        assert decision.mode == MODE_IDENTITY_REPLY
        assert decision.identity_topic == "greeting"
        assert decision.source == SOURCE_GREETING_DETECTED
        assert decision.lease.mode == MODE_LIVE_CHAT


# ══════════════════════════════════════════════════════════════════════════════
# Established-conversation guard — pure greetings yield to live_chat / Brain
# ══════════════════════════════════════════════════════════════════════════════

class TestGreetingEstablishedGuard:
    def test_live_chat_active_lease_heLa_stays_live_chat(self):
        """Established live_chat with active lease must not lose to identity card."""
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_LIVE_CHAT, locked_until=_future(5))

        decision = resolve_conversation_mode(
            db,
            tenant_id=99, convo=convo, customer_phone="+966500000099",
            text="هلا",
            history=_outbound_history(),
        )

        assert decision.mode == MODE_LIVE_CHAT
        assert decision.source == SOURCE_LEASE_HELD
        assert decision.identity_topic == ""

    def test_active_lease_blocks_even_without_history(self):
        """Migrated rows: active live_chat lease alone blocks the card."""
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_LIVE_CHAT, locked_until=_future(5))

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="هلا",
        )

        assert decision.mode == MODE_LIVE_CHAT
        assert decision.source == SOURCE_LEASE_HELD

    def test_greeted_flag_skips_identity_card(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_brain_greeted(convo)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="هلا",
        )

        assert decision.mode != MODE_IDENTITY_REPLY

    def test_outbound_history_skips_identity_card(self):
        db = FakeDB()
        convo = FakeConvo()

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="السلام عليكم",
            history=_outbound_history(),
        )

        assert decision.mode != MODE_IDENTITY_REPLY

    def test_cold_start_salaam_still_identity_reply(self):
        db = FakeDB()
        convo = FakeConvo()

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="السلام عليكم",
        )

        assert decision.mode == MODE_IDENTITY_REPLY
        assert decision.identity_topic == "greeting"
        assert decision.source == SOURCE_GREETING_DETECTED

    def test_cold_start_heLa_still_identity_reply(self):
        db = FakeDB()
        convo = FakeConvo()

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="هلا",
        )

        assert decision.mode == MODE_IDENTITY_REPLY
        assert decision.source == SOURCE_GREETING_DETECTED

    def test_greeting_during_recovery_with_history_unchanged(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_AUTOMATION_RECOVERY)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="السلام عليكم",
            history=_outbound_history(),
        )

        assert decision.mode == MODE_IDENTITY_REPLY
        assert decision.identity_topic == "greeting"
        assert decision.source == SOURCE_GREETING_DETECTED

    def test_identity_probe_unchanged_during_live_chat(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_LIVE_CHAT, locked_until=_future(5))

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="من أنت؟",
            history=_outbound_history(),
        )

        assert decision.mode == MODE_IDENTITY_REPLY
        assert decision.identity_topic == "identity"
        assert decision.source == SOURCE_IDENTITY_DETECTED

    def test_actionable_greeting_not_detected_as_pure_greeting(self):
        assert detect_identity_topic("هلا، كم سعر العسل؟") == ""

    def test_is_established_conversation_helpers(self):
        convo = FakeConvo()
        assert is_established_conversation(convo, []) is False
        assert is_established_conversation(convo, _outbound_history()) is True
        _seed_brain_greeted(convo)
        assert is_established_conversation(convo, []) is True

    def test_should_apply_greeting_identity_card_matrix(self):
        convo = FakeConvo()
        lease = ModeLease(mode=MODE_LIVE_CHAT, locked_until=_future(5))
        assert should_apply_greeting_identity_card(
            prior_mode=MODE_AUTOMATION_RECOVERY,
            prior_lease=lease, convo=convo,
        ) is True
        assert should_apply_greeting_identity_card(
            prior_mode=MODE_LIVE_CHAT,
            prior_lease=lease, convo=convo,
        ) is False
        assert should_apply_greeting_identity_card(
            prior_mode=MODE_LIVE_CHAT,
            prior_lease=ModeLease(), convo=convo,
            history=_outbound_history(),
        ) is False

    def test_webhook_fast_path_blocked_for_established_live_chat(self):
        from modules.ai.routing.conversation_mode import ModeDecision

        convo = FakeConvo()
        decision = ModeDecision(
            mode=MODE_IDENTITY_REPLY,
            lease=ModeLease(),
            previous_mode=MODE_LIVE_CHAT,
            identity_topic="greeting",
        )
        assert should_use_greeting_fast_path(
            mode_decision=decision,
            convo=convo,
            history=_outbound_history(),
        ) is False

    def test_webhook_fast_path_allowed_for_cold_start(self):
        from modules.ai.routing.conversation_mode import ModeDecision

        decision = ModeDecision(
            mode=MODE_IDENTITY_REPLY,
            lease=ModeLease(),
            previous_mode=MODE_LIVE_CHAT,
            identity_topic="greeting",
        )
        assert should_use_greeting_fast_path(
            mode_decision=decision,
            convo=FakeConvo(),
            history=[],
        ) is True

    def test_webhook_fast_path_allowed_for_recovery_escape(self):
        from modules.ai.routing.conversation_mode import ModeDecision

        decision = ModeDecision(
            mode=MODE_IDENTITY_REPLY,
            lease=ModeLease(),
            previous_mode=MODE_AUTOMATION_RECOVERY,
            identity_topic="greeting",
        )
        assert should_use_greeting_fast_path(
            mode_decision=decision,
            convo=FakeConvo(),
            history=_outbound_history(),
        ) is True


# ══════════════════════════════════════════════════════════════════════════════
# Free-form override of automation_recovery
# ══════════════════════════════════════════════════════════════════════════════

class TestFreeFormOverride:
    def test_product_question_overrides_recovery(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_AUTOMATION_RECOVERY)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="عندكم منتج آخر؟",
        )

        assert decision.mode == MODE_LIVE_CHAT
        assert decision.free_form_override is True
        assert decision.previous_mode == MODE_AUTOMATION_RECOVERY
        assert decision.lease.mode == MODE_LIVE_CHAT
        assert decision.lease.locked_until

    def test_support_complaint_overrides_recovery(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_AUTOMATION_RECOVERY)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="أبغى أتحدث مع موظف الآن",
        )

        assert decision.mode == MODE_SUPPORT_ESCALATION
        assert decision.free_form_override is True

    def test_checkout_request_overrides_recovery(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_AUTOMATION_RECOVERY)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="ابغى رابط الدفع",
        )

        assert decision.mode == MODE_CHECKOUT_ASSIST
        assert decision.free_form_override is True

    def test_tracking_request_overrides_recovery(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_AUTOMATION_RECOVERY)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="وين طلبي؟",
        )

        assert decision.mode == MODE_POST_PURCHASE
        assert decision.free_form_override is True


# ══════════════════════════════════════════════════════════════════════════════
# Sticky LIVE_CHAT lease
# ══════════════════════════════════════════════════════════════════════════════

class TestStickyLiveChatLease:
    def test_lease_held_keeps_live_chat_even_with_recovery_signal(self):
        """After an override, the next turn must stay in live chat
        unless a stronger signal (handoff/identity) appears."""
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_LIVE_CHAT, locked_until=_future(5))

        # An ambiguous follow-up that COULD be misread as recovery
        # context — e.g. "ok شكراً". The lease must dominate.
        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="ok شكراً",
        )

        assert decision.mode == MODE_LIVE_CHAT
        assert decision.transitioned is False
        # Sliding window: lease refreshed on every turn
        assert decision.lease.locked_until

    def test_expired_lease_established_greeting_stays_live_chat(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_LIVE_CHAT, locked_until=_past(1))

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="مرحبا",
            history=_outbound_history(),
        )

        assert decision.mode == MODE_LIVE_CHAT
        assert decision.mode != MODE_IDENTITY_REPLY

    def test_expired_lease_cold_start_greeting_still_identity_reply(self):
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_LIVE_CHAT, locked_until=_past(1))

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="مرحبا",
        )

        assert decision.mode == MODE_IDENTITY_REPLY
        assert decision.source == SOURCE_GREETING_DETECTED


# ══════════════════════════════════════════════════════════════════════════════
# Human handoff precedence
# ══════════════════════════════════════════════════════════════════════════════

class TestHandoffPrecedence:
    def test_advisory_handoff_flag_does_not_route_to_support(self):
        """May 2026 #46 (Tenant 33) — the auto-flipped advisory tags
        (``is_human_handoff`` / ``needs_human`` / ``handoff_active``)
        no longer trap the conversation in MODE_SUPPORT_ESCALATION on
        their own. They surface the request to staff via the
        dashboard's "طلب موظف" filter, but the brain keeps running so
        the customer's natural product / pricing / shipping
        follow-ups are answered. Pre-#46 this same setup routed to
        MODE_SUPPORT_ESCALATION and silenced the brain.
        """
        db = FakeDB()
        convo = FakeConvo(is_human_handoff=True)
        _seed_lease(convo, mode=MODE_AUTOMATION_RECOVERY)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="عندكم خصم؟",
        )

        assert decision.mode != MODE_SUPPORT_ESCALATION, (
            "Advisory handoff tag alone must not silence the brain — "
            "Tenant 33 #46 policy: only manual takeover from the "
            "staff dashboard should pivot to MODE_SUPPORT_ESCALATION."
        )

    def test_paused_by_human_does_not_route_to_support(self):
        """``paused_by_human`` is leftover staff-activity residue.
        Mode must not treat it as keyboard ownership."""
        db = FakeDB()
        convo = FakeConvo(paused_by_human=True)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="السلام عليكم",
        )

        assert decision.mode != MODE_SUPPORT_ESCALATION

    def test_taken_over_at_does_not_route_to_support(self):
        """``taken_over_at`` alone is implicit residue, not Stop AI."""
        from datetime import datetime, timezone

        db = FakeDB()
        convo = FakeConvo()
        convo.taken_over_at = datetime.now(timezone.utc)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="السلام عليكم",
        )

        assert decision.mode != MODE_SUPPORT_ESCALATION


# ══════════════════════════════════════════════════════════════════════════════
# Active recovery + no override = stay in recovery
# ══════════════════════════════════════════════════════════════════════════════

class TestActiveRecoveryNoOverride:
    def test_button_payload_does_not_override(self, monkeypatch):
        """Interactive button taps come through as `[button:...]` and
        must NOT be treated as free-form overrides — the customer is
        still inside the automation flow."""
        db = FakeDB()
        convo = FakeConvo()
        _seed_lease(convo, mode=MODE_AUTOMATION_RECOVERY)

        # Stub the recovery snapshot loader so we simulate active
        # recovery without needing real DB rows.
        from modules.ai.routing import conversation_mode as cm
        monkeypatch.setattr(
            cm, "load_recovery_snapshot",
            lambda _db, *, tenant_id, customer_phone: RecoverySnapshot(
                has_recovery=True, recovery_active=True,
                last_step_idx=2, order_id=99,
            ),
        )

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="[button:cart_resume]",
        )

        assert decision.mode == MODE_AUTOMATION_RECOVERY
        assert decision.free_form_override is False


# ══════════════════════════════════════════════════════════════════════════════
# Safe fallback for missing / malformed lease
# ══════════════════════════════════════════════════════════════════════════════

class TestSafeFallback:
    def test_missing_metadata_returns_default(self):
        db = FakeDB()
        convo = FakeConvo(extra_metadata=None)

        decision = resolve_conversation_mode(
            db,
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="مرحبا",
        )

        # Greeting → identity; just make sure no crash and a valid mode.
        assert decision.mode == MODE_IDENTITY_REPLY
        assert isinstance(decision.lease, ModeLease)

    def test_malformed_metadata_returns_default(self):
        db = FakeDB()
        convo = FakeConvo(extra_metadata={META_KEY: "not-a-dict"})

        # load_lease must coerce to a default ModeLease without raising.
        lease = load_lease(convo)
        assert lease.mode == MODE_LIVE_CHAT
        assert lease.locked_until == ""

    def test_resolver_never_raises_on_db_failure(self):
        """Even if every DB call blows up, the controller must return
        a usable ModeDecision."""
        class ExplodingDB:
            def add(self, *_a, **_k): raise RuntimeError("boom")
            def flush(self): raise RuntimeError("boom")
            def rollback(self): pass
            def query(self, _model):
                raise RuntimeError("db down")

        convo = FakeConvo()
        decision = resolve_conversation_mode(
            ExplodingDB(),
            tenant_id=1, convo=convo, customer_phone="+966500000000",
            text="مرحبا",
        )
        assert decision.mode in (MODE_IDENTITY_REPLY, MODE_LIVE_CHAT)


# ══════════════════════════════════════════════════════════════════════════════
# Lease persistence roundtrip
# ══════════════════════════════════════════════════════════════════════════════

class TestLeasePersistence:
    def test_save_then_load_roundtrip(self):
        db = FakeDB()
        convo = FakeConvo()
        lease = ModeLease(
            mode=MODE_LIVE_CHAT,
            previous_mode=MODE_AUTOMATION_RECOVERY,
            reason="r",
            source="s",
            changed_at=datetime.now(timezone.utc).isoformat(),
            locked_until=_future(10),
        )

        save_lease(db, convo, lease)
        loaded = load_lease(convo)

        assert loaded.mode == MODE_LIVE_CHAT
        assert loaded.previous_mode == MODE_AUTOMATION_RECOVERY
        assert loaded.locked_until == lease.locked_until

    def test_save_lease_does_not_overwrite_sibling_metadata(self):
        """Critical: the metadata write must MERGE, not replace, so we
        do not wipe brain_state, phone, customer_phone, etc."""
        db = FakeDB()
        convo = FakeConvo(extra_metadata={
            "phone": "+966500000000",
            "brain_state": {"stage": "ordering", "greeted": True},
        })
        lease = ModeLease(mode=MODE_LIVE_CHAT)

        save_lease(db, convo, lease)

        assert convo.extra_metadata["phone"] == "+966500000000"
        assert convo.extra_metadata["brain_state"]["stage"] == "ordering"
        assert convo.extra_metadata[META_KEY]["mode"] == MODE_LIVE_CHAT


# ══════════════════════════════════════════════════════════════════════════════
# is_free_form_message classifier
# ══════════════════════════════════════════════════════════════════════════════

class TestIsFreeForm:
    @pytest.mark.parametrize("text,expected", [
        ("مرحبا", True),
        ("hello", True),
        ("[button:cod_confirm]", False),
        ("pick_2", False),
        ("", False),
        ("   ", False),
    ])
    def test_classification(self, text, expected):
        assert is_free_form_message(text) is expected
