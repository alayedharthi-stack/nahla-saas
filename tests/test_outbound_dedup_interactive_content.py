"""Two different interactive messages with the same sentence are two messages.

The outbound dedup hashed an interactive message's body text and reply buttons
and nothing else. Two pages of one browse — the same short sentence, different
rows — hashed alike, so the second send was skipped and reported as delivered
under the first one's wamid: a page the customer never received, recorded as
sent. Two product cards with the same sentence and different products collided
the same way.

These cases pin both halves of the guard: different content no longer
collides, and a true replay of the same message still does.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.outbound_dedup import _payload_signature  # noqa: E402


def _list(rows, *, body="تفضل", button="اختر"):
    return {"messaging_product": "whatsapp", "to": "966500000000", "type": "interactive",
            "interactive": {"type": "list", "body": {"text": body},
                            "action": {"button": button, "sections": [{"rows": rows}]}}}


def _card(url, *, body="تفضل", label="عرض", image="https://cdn.example.test/a.jpg"):
    return {"messaging_product": "whatsapp", "to": "966500000000", "type": "interactive",
            "interactive": {"type": "cta_url", "body": {"text": body},
                            "header": {"type": "image", "image": {"link": image}},
                            "action": {"name": "cta_url",
                                       "parameters": {"display_text": label, "url": url}}}}


PAGE_TWO = [{"id": f"nahla:choice:{i}", "title": f"قميص قطني {i}"} for i in range(10, 19)]
PAGE_THREE = [{"id": f"nahla:choice:{i}", "title": f"قميص قطني {i}"} for i in range(19, 24)]


def test_two_pages_with_the_same_sentence_are_two_messages():
    assert _payload_signature(_list(PAGE_TWO)) != _payload_signature(_list(PAGE_THREE))


def test_a_replay_of_the_same_page_is_still_the_same_message():
    assert _payload_signature(_list(PAGE_TWO)) == _payload_signature(_list(list(PAGE_TWO)))


def test_two_cards_with_the_same_sentence_are_two_messages():
    first = _card("https://shop.example.test/p/1")
    second = _card("https://shop.example.test/p/2", image="https://cdn.example.test/b.jpg")
    assert _payload_signature(first) != _payload_signature(second)
    assert _payload_signature(first) == _payload_signature(_card("https://shop.example.test/p/1"))


def test_reply_buttons_hash_exactly_as_before():
    """A message with no list, card or header keeps its old signature inputs."""
    buttons = {"type": "interactive", "interactive": {
        "type": "button", "body": {"text": "نعم أو لا؟"},
        "action": {"buttons": [{"type": "reply", "reply": {"id": "y", "title": "نعم"}}]}}}
    import hashlib
    import json
    legacy = {"type": "interactive", "body": "نعم أو لا؟", "btns": ["y:نعم"]}
    expected = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, sort_keys=True)
                              .encode("utf-8")).hexdigest()
    assert _payload_signature(buttons) == expected
