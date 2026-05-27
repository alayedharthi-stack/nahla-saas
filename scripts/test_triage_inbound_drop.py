#!/usr/bin/env python3
"""scripts/test_triage_inbound_drop.py
──────────────────────────────────────
Smoke tests for the W2.2-INV triage helper. Each test feeds a small
synthetic log corpus and asserts the classifier picks the expected
scenario. Run with::

    python scripts/test_triage_inbound_drop.py

No pytest dependency — uses unittest from stdlib so it runs on a
clean operator laptop without ``pip install``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make the scripts/ package importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import triage_inbound_drop as T  # noqa: E402


def _classify(log_text: str, *, sender=None, wamid=None) -> str:
    ev = T.collect_evidence(
        lines=log_text.splitlines(),
        sender=sender,
        wamid=wamid,
    )
    code, _ = T.classify(ev)
    return code


class TriageScenarioTests(unittest.TestCase):

    # ── Region 0 — no RAW ──────────────────────────────────────────

    def test_A_no_raw_no_lifecycle_no_evidence(self):
        log = "2026-05-22 unrelated trace nothing about the sender here\n"
        self.assertEqual(_classify(log, sender="*0706"), "A")

    def test_B_body_parse_failed(self):
        log = (
            "2026-05-22 [webhook/360dialog/any] body parse failed "
            "(returning 200): JSONDecodeError sender=*0706\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "B")

    def test_C_bg_rejected_standalone(self):
        log = (
            "2026-05-22 [INBOUND_LIFECYCLE] standalone event=bg_rejected "
            "provider=360dialog phone_id=12345 detail='queue full'\n"
        )
        # bg_rejected is captured even without a sender match (it
        # fires before sender is known).
        self.assertEqual(_classify(log, sender="*0706"), "C")

    # ── Region 1 — RAW yes / LIFECYCLE no ──────────────────────────

    def test_D1_missing_phone_id(self):
        log = (
            "[D360_RAW_INBOUND] field=messages msgs_count=1 "
            "first_sender_masked=*0706 message_ids_tail=wamid.X\n"
            "[D360_DISPATCH_GAP] reason=missing_phone_id "
            "first_sender_masked=*0706\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "D1")

    def test_D2_unknown_phone_id(self):
        log = (
            "[D360_RAW_INBOUND] field=messages msgs_count=1 "
            "first_sender_masked=*0706\n"
            "[D360_DISPATCH_GAP] reason=unknown_phone_id "
            "first_sender_masked=*0706\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "D2")

    def test_D5_field_not_messages(self):
        log = (
            "[D360_RAW_INBOUND] field=smb_message_echoes msgs_count=1 "
            "first_sender_masked=*0706\n"
            "[D360_DISPATCH_GAP] reason=field_not_messages "
            "first_sender_masked=*0706\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "D5")

    # ── Region 3 — LIFECYCLE yes ───────────────────────────────────

    def test_E1_dedup_drop_memory(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w1 sender=*0706 "
            "final=end_dropped path=received->dedup_drop_memory->end_dropped\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "E1")

    def test_E2_dedup_drop_db_silent_loss(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w2 sender=*0706 "
            "final=end_dropped path=received->dedup_drop_db->end_dropped\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "E2")

    def test_F_db_session_fail(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w3 sender=*0706 "
            "final=end_dropped path=received->db_session_fail->end_dropped\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "F")

    def test_G_unsupported_type(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w4 sender=*0706 "
            "final=end_dropped path=received->unsupported_type->persist_inbound_only_ok->end_dropped\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "G")

    def test_J_pre_brain_handoff_silent_inbound_loss(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[Merchant/HANDOFF_GUARD] PRE-BRAIN handoff fired tenant=33 "
            "to=9665550706 snippet='ابي اكلم المالك'\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w5 sender=*0706 "
            "final=end_ok path=received->message_saved->end_ok\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "J")

    def test_K_historical_skip(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[HISTORICAL_MESSAGE_SKIP_AI] tenant_id=33 conversation_id=99 "
            "message_id=wamid.X to=9665550706 message_ts=2024-01-01\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w6 sender=*0706 "
            "final=end_ok path=received->message_saved->end_ok\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "K")

    def test_L_payment_asset_early_bypass(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[PAYMENT_INFO] early-bypass APPLIED tenant=33 convo=99 "
            "to=9665550706 asset_id=7 hard_override=true\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w7 sender=*0706 "
            "final=end_ok path=received->message_saved->end_ok\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "L")

    def test_M_end_ok_without_brain(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w8 sender=*0706 "
            "final=end_ok path=received->message_saved->end_ok\n"
        )
        # No brain_invoked, no handoff, no hist, no bypass → AI pause.
        self.assertEqual(_classify(log, sender="*0706"), "M")

    def test_N_uncaught_exception(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w9 sender=*0706 "
            "final=end_uncaught_exception path=received->end_uncaught_exception\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "N")

    def test_O_happy_path(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w10 sender=*0706 "
            "final=end_ok "
            "path=received->normalizer_ok->brain_invoked->message_saved->end_ok\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "O")

    def test_I_payment_short_circuit_via_path_token(self):
        log = (
            "[D360_RAW_INBOUND] field=messages first_sender_masked=*0706\n"
            "[INBOUND_LIFECYCLE] trace_id=il_360dialog_w11 sender=*0706 "
            "final=end_ok "
            "path=received->payment_short_circuit->auto_link_ok->message_saved->end_ok\n"
        )
        self.assertEqual(_classify(log, sender="*0706"), "I")

    # ── Sender resolution ──────────────────────────────────────────

    def test_sender_resolution_from_full_phone(self):
        class _A:
            sender = None
            phone = "+9665550706"
            wamid = None
        self.assertEqual(T._resolve_sender(_A()), "*0706")

    def test_sender_resolution_from_masked(self):
        class _A:
            sender = "0706"
            phone = None
            wamid = None
        self.assertEqual(T._resolve_sender(_A()), "*0706")


if __name__ == "__main__":
    unittest.main(verbosity=2)
