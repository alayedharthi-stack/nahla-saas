"""
tests/test_upstream_marker_scrub.py
───────────────────────────────────
Locks the contract that internal markers (`[TEMPLATE:contact_owner]`,
`[TRANSFER]`, `[DEBUG]`, `[ACTION]`, `[INTERNAL]`, `[MEDIA:N]`,
`[TEMPLATE:foo]`, etc.) are stripped at THREE upstream layers — not
just at the wire layer.

Why three layers
────────────────
A previous incident: a customer received a WhatsApp message
containing `[TEMPLATE:contact_owner]` literally. The wire-layer
scrub (F3) at `services.whatsapp_platform.service.
_scrub_outbound_payload` correctly cleaned the bytes sent to Meta,
but the MessageEvent row written to the DB BEFORE the wire send
kept the marker — and the merchant dashboard renders straight
from that DB row.

The fix (F7) installs scrubs at:

  Layer 1: MerchantBrain.run() return    (pipeline.py)
           — earliest possible, catches AI hallucinations at
             the brain boundary. Single chokepoint for AI text.

  Layer 2: StateManager.save_message     (conversation_engine.py)
           — catches non-brain outbound paths (identity reply,
             handoff ack, loop-pause, etc.) at persistence time.
             INBOUND direction left untouched on purpose.

  Layer 3: record_outbound_message       (routers/conversations.py)
           — catches campaign / automation / COD / AI-fallback /
             orders paths that don't go through StateManager.

  Layer 4 (already locked by test_wire_layer_marker_scrub.py):
           provider_send_message wire layer — last-line defense.

Invariant: by the time ANY observer (Meta, the DB, the dashboard,
the audit log) sees outbound text, markers are gone.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ──────────────────────────────────────────────────────────────────────
# Layer 1 — MerchantBrain.run() return
# ──────────────────────────────────────────────────────────────────────


class TestBrainBoundaryScrub:
    """The brain returns ``{"reply": "...", "buttons": [...],
    "handoff": bool}``. The reply string must have markers
    stripped before it leaves the brain.

    We don't construct a full brain pipeline run here — that
    requires a real DB + intent classifier + decision engine.
    Instead we replay the scrub at the same boundary by importing
    the module and asserting that the stripping logic lives where
    we expect."""

    def test_pipeline_imports_scrub_at_brain_boundary(self):
        """Pipeline source MUST call scrub_internal_markers as the
        last transform before returning the reply. Locks the
        physical location so a future refactor that moves the
        scrub elsewhere is forced to update this test (and notice
        the contract change)."""
        from modules.ai.brain import pipeline
        src = Path(pipeline.__file__).read_text(encoding="utf-8")
        # The scrub must appear AFTER the trace-log block and
        # BEFORE the `return {"reply": reply, ...}` statement.
        assert "scrub_internal_markers" in src, (
            "brain pipeline.py no longer imports scrub_internal_markers — "
            "marker stripping at the brain boundary has been removed"
        )
        assert "[BRAIN_SCRUB]" in src, (
            "brain pipeline.py no longer emits [BRAIN_SCRUB] log line"
        )
        # Sanity: the scrub block sits inside the function that
        # produces the {"reply": ...} return, NOT in some unrelated
        # helper. We assert ordering with a simple substring index.
        scrub_idx  = src.find("scrub_internal_markers(reply or")
        return_idx = src.find('return {\n            "reply": reply,')
        assert scrub_idx != -1, "brain-boundary scrub call removed"
        assert return_idx != -1, "brain return dict signature changed"
        assert scrub_idx < return_idx, (
            "scrub_internal_markers must be called BEFORE the brain "
            "returns the reply dict, otherwise downstream consumers "
            "see un-scrubbed text"
        )

    def test_scrub_helper_strips_template_marker(self):
        """End-to-end: feed the exact marker that leaked in
        production through the helper the brain uses. Stripping
        must succeed."""
        from core.ai_libraries import scrub_internal_markers
        raw = (
            "يا أم هشام، أرى استفسارك عن العسل.\n"
            "تواصل مع المتجر للمزيد. [TEMPLATE:contact_owner]"
        )
        cleaned = scrub_internal_markers(raw)
        assert "[TEMPLATE:contact_owner]" not in cleaned
        assert "[TEMPLATE" not in cleaned
        assert "تواصل مع المتجر للمزيد" in cleaned

    def test_scrub_helper_strips_all_four_named_markers(self):
        """The product owner explicitly listed four marker names
        that must be stripped: TRANSFER, DEBUG, ACTION, INTERNAL.
        Add MEDIA:N (which has its own extractor upstream but
        whose remnants must also be caught) and TEMPLATE:foo
        for completeness."""
        from core.ai_libraries import scrub_internal_markers
        for marker in (
            "[TRANSFER]", "[DEBUG]", "[ACTION]", "[INTERNAL]",
            "[MEDIA:5]", "[TEMPLATE:contact_owner]",
            "[TEMPLATE:order_confirmation]",
        ):
            raw = f"بدايه النص {marker} نهايه النص"
            cleaned = scrub_internal_markers(raw)
            assert marker not in cleaned, f"{marker} survived: {cleaned!r}"
            assert "بدايه النص" in cleaned
            assert "نهايه النص" in cleaned


# ──────────────────────────────────────────────────────────────────────
# Layer 2 — StateManager.save_message
# ──────────────────────────────────────────────────────────────────────


class TestStateManagerSaveMessageScrub:
    """`StateManager.save_message(db, phone, body, direction)`
    writes a MessageEvent row. Outbound bodies must be scrubbed
    before persistence; inbound bodies must NOT be touched."""

    def _captured_save(self):
        """Stub DB whose ``.add(...)`` records the MessageEvent's
        body field so the test can assert on what was written."""
        captured: Dict[str, Any] = {}

        class _Sess:
            def add(self, obj):
                captured["body"] = obj.body
                captured["direction"] = obj.direction
                captured["tenant_id"] = obj.tenant_id
            def commit(self): pass
            def rollback(self): pass

        return _Sess(), captured

    def test_outbound_body_is_scrubbed(self):
        from core.conversation_engine import StateManager

        db, captured = self._captured_save()
        StateManager.save_message(
            db, "+966500000111",
            "تواصل مع المتجر [TEMPLATE:contact_owner] شكراً",
            "outbound",
            conversation_id=1, tenant_id=33,
        )
        assert "[TEMPLATE:contact_owner]" not in captured["body"]
        assert "تواصل مع المتجر" in captured["body"]
        assert "شكراً" in captured["body"]

    def test_outbound_alias_out_also_scrubbed(self):
        """Some callers pass ``direction='out'`` (alias for
        ``outbound``). The scrub must accept both."""
        from core.conversation_engine import StateManager

        db, captured = self._captured_save()
        StateManager.save_message(
            db, "+966500000111",
            "نص [TRANSFER] داخلي",
            "out",
            conversation_id=1, tenant_id=33,
        )
        assert "[TRANSFER]" not in captured["body"]
        assert "نص" in captured["body"] and "داخلي" in captured["body"]

    def test_inbound_body_preserved_verbatim(self):
        """Customer-typed bracketed text is evidence — never
        scrubbed. A merchant audit may need the exact input."""
        from core.conversation_engine import StateManager

        db, captured = self._captured_save()
        original = "اطلب [طلبية] جديدة من [الموقع]"
        StateManager.save_message(
            db, "+966500000111", original, "inbound",
            conversation_id=1, tenant_id=33,
        )
        assert captured["body"] == original

    def test_inbound_with_uppercase_brackets_also_preserved(self):
        """Even ASCII-uppercase brackets in customer input are
        kept. The customer might be quoting our own messages
        back at us, or sending a product SKU."""
        from core.conversation_engine import StateManager

        db, captured = self._captured_save()
        original = "أبغى المنتج [SKU123]"
        StateManager.save_message(
            db, "+966500000111", original, "inbound",
            conversation_id=1, tenant_id=33,
        )
        assert captured["body"] == "أبغى المنتج [SKU123]"

    def test_empty_body_passes_through(self):
        from core.conversation_engine import StateManager

        db, captured = self._captured_save()
        StateManager.save_message(
            db, "+966500000111", "", "outbound",
            conversation_id=1, tenant_id=33,
        )
        assert captured["body"] == ""

    def test_non_string_body_passes_through(self):
        """Defensive: a buggy caller might pass None. We must not
        crash — the underlying DB column will reject if needed."""
        from core.conversation_engine import StateManager

        db, captured = self._captured_save()
        StateManager.save_message(
            db, "+966500000111", None, "outbound",
            conversation_id=1, tenant_id=33,
        )
        assert captured["body"] is None


# ──────────────────────────────────────────────────────────────────────
# Layer 3 — record_outbound_message
# ──────────────────────────────────────────────────────────────────────


class TestRecordOutboundMessageScrub:
    """`record_outbound_message(db, tenant, phone, body)` is
    called by campaigns / automations / COD / AI-fallback /
    orders. Same scrub invariant as save_message — but this
    function is ALWAYS outbound (no direction param), so it
    always scrubs."""

    def _stub_db(self):
        """Minimal DB stub that records the body of the added
        MessageEvent and supports SAVEPOINT semantics."""
        captured: Dict[str, Any] = {"body": None}

        class _Query:
            def filter(self, *_a, **_k): return self
            def first(self): return None

        class _Sess:
            def begin_nested(self): pass
            def add(self, obj):
                captured["body"] = obj.body
                captured["direction"] = getattr(obj, "direction", None)
            def flush(self): pass
            def rollback(self): pass
            def query(self, *_a, **_k): return _Query()

        return _Sess(), captured

    def test_outbound_body_is_scrubbed(self, monkeypatch):
        from routers import conversations as conv_mod

        # The function looks up _get_or_create_conversation —
        # stub it to avoid hitting the real DB layer.
        monkeypatch.setattr(
            conv_mod, "_get_or_create_conversation",
            lambda db, tid, phone, name="": MagicMock(id=42),
        )

        db, captured = self._stub_db()
        conv_mod.record_outbound_message(
            db, tenant_id=33, phone="+966500000111",
            body="تواصل [TEMPLATE:contact_owner] الآن",
        )
        assert "[TEMPLATE:contact_owner]" not in captured["body"]
        assert "تواصل" in captured["body"]
        assert "الآن" in captured["body"]
        assert captured["direction"] == "outbound"

    def test_multiple_markers_all_stripped(self, monkeypatch):
        from routers import conversations as conv_mod

        monkeypatch.setattr(
            conv_mod, "_get_or_create_conversation",
            lambda db, tid, phone, name="": MagicMock(id=42),
        )
        db, captured = self._stub_db()
        conv_mod.record_outbound_message(
            db, tenant_id=33, phone="+966500000111",
            body="[ACTION] خطوة [DEBUG] أخرى [INTERNAL]",
        )
        for tok in ("[ACTION]", "[DEBUG]", "[INTERNAL]"):
            assert tok not in captured["body"]
        assert "خطوة" in captured["body"]
        assert "أخرى" in captured["body"]

    def test_clean_body_passes_through_unchanged(self, monkeypatch):
        from routers import conversations as conv_mod

        monkeypatch.setattr(
            conv_mod, "_get_or_create_conversation",
            lambda db, tid, phone, name="": MagicMock(id=42),
        )
        db, captured = self._stub_db()
        clean = "مرحباً، شكراً لطلبك."
        conv_mod.record_outbound_message(
            db, tenant_id=33, phone="+966500000111", body=clean,
        )
        assert captured["body"] == clean

    def test_arabic_brackets_preserved(self, monkeypatch):
        """`[ملاحظة]` is Arabic-text-in-brackets — the regex is
        ASCII-uppercase-only so this MUST pass through."""
        from routers import conversations as conv_mod

        monkeypatch.setattr(
            conv_mod, "_get_or_create_conversation",
            lambda db, tid, phone, name="": MagicMock(id=42),
        )
        db, captured = self._stub_db()
        body = "[ملاحظة] الطلب جاهز"
        conv_mod.record_outbound_message(
            db, tenant_id=33, phone="+966500000111", body=body,
        )
        assert captured["body"] == body


# ──────────────────────────────────────────────────────────────────────
# Defense-in-depth invariants
# ──────────────────────────────────────────────────────────────────────


class TestScrubDefenseInDepth:
    """If the brain scrub somehow returned text WITH a marker
    (bug, regression, regex skipped a variant), the persistence
    layer must catch it on the way to the DB. The wire layer
    must catch it on the way to Meta. Three independent layers
    means no single bug can leak."""

    def test_persistence_catches_brain_scrub_miss(self, monkeypatch):
        """Simulate a hypothetical brain-scrub regression: feed
        save_message text WITH a marker (as if the brain failed
        to strip it). The persistence layer must still produce
        a clean row."""
        from core.conversation_engine import StateManager

        captured: Dict[str, Any] = {}

        class _Sess:
            def add(self, obj): captured["body"] = obj.body
            def commit(self): pass
            def rollback(self): pass

        StateManager.save_message(
            _Sess(), "+966500000111",
            "نص [TEMPLATE:contact_owner] مع marker",
            "outbound",
            conversation_id=1, tenant_id=33,
        )
        assert "[TEMPLATE" not in captured["body"]

    def test_wire_layer_catches_persistence_scrub_miss(self):
        """Same idea for the wire layer — independently locked
        by test_wire_layer_marker_scrub.py, restated here as a
        belt-and-suspenders check that both still strip the same
        marker pattern."""
        from services.whatsapp_platform.service import _scrub_outbound_payload
        payload = {
            "messaging_product": "whatsapp",
            "to": "+966500000111",
            "type": "text",
            "text": {"body": "[TEMPLATE:contact_owner] أهلاً"},
        }
        cleaned = _scrub_outbound_payload(payload)
        assert "[TEMPLATE:contact_owner]" not in cleaned["text"]["body"]
        assert "أهلاً" in cleaned["text"]["body"]
