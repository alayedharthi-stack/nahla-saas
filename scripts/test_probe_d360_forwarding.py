#!/usr/bin/env python3
"""scripts/test_probe_d360_forwarding.py
─────────────────────────────────────────
Pure-function smoke tests for the W2.2-INV D360 forwarding probe.
Network and DB layers are NOT exercised here — those are covered by
the operational use of the script itself. We only verify:

* ``classify`` maps known input shapes to the correct A-class suspects.
* ``_expected_channel_url`` builds the canonical URL.
* ``_normalize_url`` cancels trailing-slash drift.
* ``_mask_tail`` masks safely.

Stdlib only — runs anywhere Python 3.9+ is installed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_d360_forwarding as P  # noqa: E402


def _row(**overrides) -> P.ConnectionRow:
    defaults = dict(
        tenant_id=33,
        connection_id=1,
        phone_number_id_local="1234567890",
        phone_number="+9665550706",
        waba_id="waba_x",
        provider="dialog360",
        connection_type="coexistence",
        status="connected",
        sending_enabled=True,
        webhook_verified=True,
        access_token="sk_demo_abcd",
        last_webhook_received_at=None,
        webhook_coexistence_received_at=None,
        webhook_status_received_at=None,
        meta_quality_rating="GREEN",
        meta_messaging_limit="TIER_10K",
        extra_metadata={"provider_details": {"channel_id": "CH123"}},
        silent_min=120.0,
    )
    defaults.update(overrides)
    return P.ConnectionRow(**defaults)


def _snap(url: str, *, matches: bool, status: int = 200) -> P.WebhookConfigSnapshot:
    return P.WebhookConfigSnapshot(status=status, url=url, matches_expected=matches, raw={})


class PureFnTests(unittest.TestCase):

    def test_expected_channel_url(self):
        self.assertEqual(
            P._expected_channel_url("https://api.nahlah.ai"),
            "https://api.nahlah.ai/webhook/whatsapp/360dialog",
        )
        self.assertEqual(
            P._expected_channel_url("https://api.nahlah.ai/"),
            "https://api.nahlah.ai/webhook/whatsapp/360dialog",
        )
        self.assertEqual(
            P._expected_channel_url(""),
            "/webhook/whatsapp/360dialog",
        )

    def test_normalize_url(self):
        self.assertEqual(
            P._normalize_url("https://x.com/y/"),
            "https://x.com/y",
        )
        self.assertEqual(
            P._normalize_url("  https://x.com/y  "),
            "https://x.com/y",
        )
        self.assertEqual(P._normalize_url(None), "")

    def test_mask_tail(self):
        self.assertEqual(P._mask_tail(None), "-")
        self.assertEqual(P._mask_tail(""), "-")
        self.assertEqual(P._mask_tail("abc"), "*abc")
        self.assertEqual(P._mask_tail("abcdef"), "*cdef")
        self.assertEqual(P._mask_tail("+966555012345"), "*2345")


class ClassifyTests(unittest.TestCase):

    expected = "https://api.nahlah.ai/webhook/whatsapp/360dialog"

    def _do(self, *, row=None, channel=None, waba=None,
            waba_numbers: List[str] = None, hub_body=None) -> P.TenantVerdict:
        return P.classify(
            row or _row(),
            channel=channel,
            waba=waba,
            waba_numbers=waba_numbers or [],
            hub_status=200 if hub_body is not None else None,
            hub_body=hub_body,
            expected_url=self.expected,
            silent_min_threshold=60.0,
        )

    def test_A1_channel_not_configured(self):
        v = self._do(
            channel=_snap("", matches=False, status=200),
            waba=_snap(self.expected, matches=True),
        )
        self.assertIn("A1", v.suspects)
        self.assertTrue(v.silent_loss)

    def test_A2_channel_url_mismatch(self):
        v = self._do(
            channel=_snap("https://api.nahlah.ai/old/webhook", matches=False),
            waba=_snap(self.expected, matches=True),
        )
        self.assertIn("A2", v.suspects)
        self.assertTrue(v.silent_loss)

    def test_A4_waba_not_configured(self):
        v = self._do(
            channel=_snap(self.expected, matches=True),
            waba=_snap("", matches=False, status=200),
        )
        self.assertIn("A4", v.suspects)
        self.assertTrue(v.silent_loss)

    def test_A3_phone_id_drift(self):
        v = self._do(
            channel=_snap(self.expected, matches=True),
            waba=_snap(self.expected, matches=True),
            hub_body={"phone_number_id": "9999999999"},
        )
        self.assertIn("A3", v.suspects)
        self.assertTrue(v.silent_loss)

    def test_A9_url_not_nahla_domain(self):
        v = self._do(
            channel=_snap("https://other-service.example.com/webhook", matches=False),
            waba=_snap(self.expected, matches=True),
        )
        # A2 fires first (URL mismatch), A9 augments because the URL is
        # not even a Nahla domain.
        self.assertIn("A2", v.suspects)
        self.assertIn("A9", v.suspects)
        self.assertTrue(v.silent_loss)

    def test_A10_quality_red(self):
        v = self._do(
            row=_row(meta_quality_rating="RED"),
            channel=_snap(self.expected, matches=True),
            waba=_snap(self.expected, matches=True),
        )
        self.assertIn("A10", v.suspects)
        self.assertTrue(v.silent_loss)

    def test_A8_edge_drop_when_all_green(self):
        v = self._do(
            channel=_snap(self.expected, matches=True),
            waba=_snap(self.expected, matches=True),
        )
        self.assertIn("A8", v.suspects)
        self.assertTrue(v.silent_loss)

    def test_no_silent_loss_when_fresh_and_all_green(self):
        v = self._do(
            row=_row(silent_min=2.0),
            channel=_snap(self.expected, matches=True),
            waba=_snap(self.expected, matches=True),
        )
        self.assertFalse(v.silent_loss)
        self.assertEqual(v.suspects, [])

    def test_A5_note_when_unknown_numbers_on_waba(self):
        v = self._do(
            channel=_snap(self.expected, matches=True),
            waba=_snap(self.expected, matches=True),
            waba_numbers=["9999", "1234567890"],
        )
        joined = "\n".join(v.notes)
        self.assertIn("9999", joined)

    def test_no_api_key_marks_A1_and_A4(self):
        v = self._do(
            row=_row(access_token=None),
            channel=None,
            waba=None,
        )
        self.assertIn("A1", v.suspects)
        self.assertIn("A4", v.suspects)
        self.assertTrue(v.silent_loss)


if __name__ == "__main__":
    unittest.main(verbosity=2)
