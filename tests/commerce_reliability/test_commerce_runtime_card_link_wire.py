"""A refused card's page, through the whole send path (no network).

Independent review of #1155: a page address whose slug the handoff-promise scrub
matches (``…/p/سيتمتحويلك``) passed the two fingerprint checks the recovery then
made, and the send path cut it to ``https://shop.example.test/p/`` because no
handoff was active. The recovery now asks the send path's own rules
(``outbound_text_rewrite_rule``) before adding a page.

Everything between the refused card and the provider is real here: the recovery
payload, the reply transport, the pilot's text sender and wire observation,
``_send_whatsapp_message``, ``_post_wa``, the outbound sanitiser — its handoff
scrub resolving "no handoff" because the database cannot answer — the dedup,
and the provider's internal-marker scrub. The only doubles are the provider's
HTTP call and the two account gates that need a database (AI-disabled,
conversation quota). Generic merchants; no rule is about one store.
"""
from __future__ import annotations

import asyncio
import itertools
import threading
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import reply_card as rcard
from core.commerce_runtime import runtime_entry as entry
from core.outbound_sanitizer import outbound_text_rewrite_rule
from core.wa_link_buttons import strip_empty_markdown_links
from services import commerce_runtime_pilot as seam

TENANT = 5151
PHONE_ID = "1555000222"
WRITTEN = "عطر الورد متوفر بسعر 180 ريال."
PAGE = "https://shop.example.test/p/rose-perfume-100ml"

# Every rule the send path applies to a text body, one address each.
REWRITTEN_PAGES = {
    "handoff_promise": "https://shop.example.test/p/سيتمتحويلك",
    "external_research": "https://shop.example.test/p/%25D8%25B9%25D8%25B7%25D8%25B1",
    "leakage_firewall_word": "https://shop.example.test/p/debug-kit",
    "leakage_firewall_field": "https://shop.example.test/p/perfume?intent=buy",
    "internal_marker": "https://shop.example.test/p/[SKU_A1]",
}

_RECIPIENTS = itertools.count(100)


class _NoDatabase:
    """The webhook session, unreachable: every lookup on it fails and is caught."""

    def query(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no database in this test")


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[str]]:
    """Run the real send path; record the body the provider was handed."""
    import core.ai_disabled_gate as ai_gate
    import core.wa_usage as wa_usage
    import routers.whatsapp_webhook as webhook
    from core.outbound_dedup import clear_outbound_dedup
    from services.whatsapp_platform import service

    transmitted: List[str] = []

    async def provider_send_message(_db: Any, _conn: Any, *, payload: Dict[str, Any],
                                    **_kwargs: Any) -> Tuple[Dict[str, Any], Any]:
        # The provider's own last scrub runs, as it does before the HTTP POST.
        posted = service._scrub_outbound_payload(payload)
        transmitted.append(posted["text"]["body"])
        return ({"messages": [{"id": "wamid.WIRE"}], "_nahla_classification": "ok",
                 "_nahla_wamid": "wamid.WIRE", "_nahla_http_status": 200},
                SimpleNamespace(source="test"))

    monkeypatch.setattr(webhook, "provider_send_message", provider_send_message)
    monkeypatch.setattr(ai_gate, "evaluate_ai_disabled_send_block", lambda *a, **k: (False, None))
    monkeypatch.setattr(wa_usage, "check_limit", lambda *a, **k: SimpleNamespace(allowed=True))
    clear_outbound_dedup()
    yield transmitted
    clear_outbound_dedup()


@pytest.fixture()
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    """The webhook's event loop, which the pilot's sender schedules onto."""
    running = asyncio.new_event_loop()
    thread = threading.Thread(target=running.run_forever, daemon=True)
    thread.start()
    yield running
    running.call_soon_threadsafe(running.stop)
    thread.join(timeout=5)
    running.close()


def _refused_card(page: str) -> Dict[str, Any]:
    return {"text": WRITTEN, rcard.CARD_KEY: {"product_id": 7,
                                              "image_url": "https://cdn.example.test/rose.webp",
                                              "button_url": page, "button_label": "عرض العطر"}}


def _send(loop: asyncio.AbstractEventLoop, payload: Dict[str, Any]) -> seam.WireObservation:
    observed = seam.WireObservation()
    recipient = f"+9665000{next(_RECIPIENTS):05d}"      # one number each: no throttle, no dedup
    send = seam._send_factory(PHONE_ID, TENANT, _NoDatabase(), loop, observed)
    response = entry.whatsapp_reply_transport(send, None, recipient=recipient)(payload)
    assert lc.classify_send_response(response)[0] == lc.ReceiptKind.ACCEPTED
    return observed


def test_a_refused_card_s_page_reaches_the_customer_whole(wire, loop):
    payload, additions = entry._recovery_payload(_refused_card(PAGE))
    observed = _send(loop, payload)
    assert wire == [f"{WRITTEN}\n{PAGE}"]                        # every word, then the page
    stored, transformed, reasons = observed.resolve(WRITTEN, additions)
    assert stored == wire[0] and transformed is True
    assert reasons == [rcard.LINK_APPENDED_REASON]               # named, and nothing else


@pytest.mark.parametrize("rule", sorted(REWRITTEN_PAGES))
def test_a_page_the_send_path_would_rewrite_is_never_added(wire, loop, rule):
    """The model's words arrive untouched and no broken address goes with them."""
    payload, additions = entry._recovery_payload(_refused_card(REWRITTEN_PAGES[rule]))
    assert additions == () and rcard.LINK_APPENDED_KEY not in payload
    observed = _send(loop, payload)
    assert wire == [WRITTEN]
    assert observed.resolve(WRITTEN, additions) == (WRITTEN, False, [])


def test_the_path_really_cuts_the_address_the_review_found(wire, loop):
    """The control: handed the same page directly, as #1155's first head would
    have, the send path's handoff scrub cuts it mid-address. Without this the
    cases above could pass on a harness that scrubs nothing."""
    handed = f"{WRITTEN}\n{REWRITTEN_PAGES['handoff_promise']}"
    observed = _send(loop, {"text": handed})
    assert wire[0] != handed
    assert "سيتمتحويلك" not in wire[0] and wire[0].endswith("https://shop.example.test/p/")
    assert "outbound_payload_sanitizer" in observed.resolve(handed)[2]


@pytest.mark.parametrize("body", [
    f"{WRITTEN}\n{PAGE}",
    "القميص القطني الأزرق متوفر بمقاس M و L.",
    "حذاء رياضي أبيض\n\n\nبسعر 199 ريال  ",                    # tidied, not rewritten
    *(f"{WRITTEN}\n{page}" for page in REWRITTEN_PAGES.values()),
    "سيتم تحويلك للموظف الآن.",
    "المصادر:\nhttps://ar.wikipedia.org/wiki/عطر",
    "أهلاً [TRANSFER] بك",
])
def test_the_rule_names_exactly_what_the_send_path_rewrites(wire, loop, body):
    """No drift between the check and the path: a body the rule clears reaches
    the provider as written (after the whitespace tidy), and a body it names does
    not."""
    _send(loop, {"text": body})
    tidied = strip_empty_markdown_links(body)
    if outbound_text_rewrite_rule(body) is None:
        assert wire == [tidied]
    else:
        assert wire != [tidied]
