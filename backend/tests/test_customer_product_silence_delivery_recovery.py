"""Customer product-silence incident — end-to-end delivery recovery regressions.

Production diagnosis (Tenant 1, legacy Commerce V1):

* ``وش منتجاتكم ؟`` — catalog search succeeded, the truth guard emptied the
  composed text, the arbitrary first-candidate card was correctly rejected as
  ``stale_or_unrelated_product`` and the customer received NOTHING while the
  lifecycle still reported ``end_ok``.
* ``وش عندكم`` — three candidates ``فستان / فستان / جاكيت`` produced
  duplicate visible button titles, Meta rejected the interactive payload with
  HTTP 400 ``Duplicate button title`` (misclassified as ``invalid_phone``) and
  no plain-text recovery was sent.

These tests drive ``_handle_merchant_message`` through the real ``_post_wa``
and ``provider_send_message`` layers against a scripted fake HTTP provider —
the nearest safe boundary below the webhook HTTP handler. They assert
provider-call counts, provider message ids, outbound persistence, delivery
audit outcome, lifecycle result and duplicate-send prevention. They do NOT
assert exact Arabic prose except where the wording itself is the evidence
(catalog facts must be grounded in the verified candidates).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytest.importorskip("observability.event_logger")

import models as _models  # noqa: E402

sys.modules.setdefault("database.models", _models)

from core.inbound_lifecycle import (  # noqa: E402
    EVENT_END_OK,
    EVENT_MESSAGE_SAVED,
    inbound_lifecycle_trace,
    record_lifecycle,
)
from core.outbound_dedup import clear_outbound_dedup  # noqa: E402
from core.outbound_send_status import build_provider_send_block  # noqa: E402
from modules.ai.brain.commerce.product_presentation_selection import (  # noqa: E402
    build_standard_pick_buttons,
)
from routers.whatsapp_webhook import (  # noqa: E402
    _handle_merchant_message,
    _send_whatsapp_message as _REAL_SEND_TEXT,
)
from services.whatsapp_platform.service import (  # noqa: E402
    provider_post_with_context as _REAL_PROVIDER_POST,
)
from tests.test_trusted_context_shadow_wireup import (  # noqa: E402
    _merchant_handler_convo,
    _merchant_handler_db,
    _merchant_handler_patch_ctx,
)

# Exact production inputs (wording and punctuation preserved).
INPUT_BROAD = "وش منتجاتكم ؟"
INPUT_WHAT = "وش عندكم"
INPUT_SPECIFIC = "ابي فستان"
INPUT_SALAM = "السلام عليكم ورحمة الله وبركاته"
INPUT_HOWDY = "كيف الحال"

# Generic merchant fixture (platform-wide policy — no production store data).
MERCHANT = "متجر تجريبي عام"
TENANT_ID = 1
PHONE_ID = "PH1"
DUP_TITLE_DETAIL = "Duplicate button title"

# 39-char grounded V1 text answer for the second incident path.
GROUNDED_TEXT = "عندنا فساتين وجاكيتات، اختر اللي يناسبك"
# The model's composed catalog answer for path 1 — the webhook-level
# availability/truth guard reduces it to empty (production first divergence).
COMPOSED_CATALOG_ANSWER = "عندنا فستان وجاكيت متوفرة حالياً، أي واحد يناسبك؟"


def _candidates() -> List[Dict[str, Any]]:
    """Verified catalog candidates exactly as V1 stores them in brain_state."""
    return [
        {
            "id": 11, "external_id": "p-11", "title": "فستان", "price": "250",
            "in_stock": True, "can_checkout": True, "orderable": True,
            "stock_qty": 4, "status": "active",
            "product_url": "https://example.test/products/11",
            "image_url": "https://example.test/images/11.jpg",
        },
        {
            "id": 12, "external_id": "p-12", "title": "فستان", "price": "300",
            "in_stock": True, "can_checkout": True, "orderable": True,
            "stock_qty": 2, "status": "active",
            "product_url": "https://example.test/products/12",
            "image_url": "https://example.test/images/12.jpg",
        },
        {
            "id": 13, "external_id": "p-13", "title": "جاكيت", "price": "400",
            "in_stock": False, "can_checkout": True, "orderable": True,
            "stock_qty": 0, "status": "active",
            "product_url": "https://example.test/products/13",
            "image_url": "https://example.test/images/13.jpg",
        },
    ]


def _brain_state(candidates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    rows = _candidates() if candidates is None else list(candidates)
    return {
        "turn": 3,
        "stage": "browsing",
        "last_intent": "ask_product",
        "last_intent_confidence": 0.93,
        "last_action": "search_products",
        "last_search_candidates": rows,
        "last_recommended_products": [],
        "current_product_focus": None,
        "last_browse_query": "",
        "order_prep": {},
    }


def _resolution_for(query: str) -> Optional[SimpleNamespace]:
    for row in _candidates():
        if row["title"] == (query or "").strip():
            return SimpleNamespace(
                id=row["id"], external_id=row["external_id"], title=row["title"],
                price=row["price"], sale_price=None, image_url=row["image_url"],
                product_url=row["product_url"], description="",
                in_stock=row["in_stock"], can_checkout=True, variants=[],
                needs_variant_choice=False, default_variant_id=None,
                default_variant_retailer_id=None, has_variants=False,
                matched_query=query, confidence="fts",
            )
    return None


def _brain_return(
    *,
    reply: str,
    buttons: Optional[list] = None,
    product_cards: Optional[list] = None,
    decision_action: str = "search_products",
    intent: str = "ask_product",
    catalog_product_ids: Optional[list] = None,
    chosen_path: str = "catalog_search_compose",
) -> Dict[str, Any]:
    return {
        "reply": reply,
        "buttons": list(buttons or []),
        "product_cards": list(product_cards or []),
        "handoff": False,
        "chosen_path": chosen_path,
        "compose_source": "persona_llm",
        "response_mode": "catalog_answer",
        "llm_candidate_present": True,
        "final_text_transformed": not bool(reply),
        "final_transform_reasons": ["availability_truth_guard"] if not reply else [],
        "decision_action": decision_action,
        "intent": intent,
        "catalog_product_ids": (
            [11, 12, 13] if catalog_product_ids is None else list(catalog_product_ids)
        ),
        "facts_snapshot_id": "snap-incident",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fake provider transport — scripted at the httpx boundary so the real
# ``provider_post_with_context`` classification runs on every attempt.
# ─────────────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int, body: Dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self) -> Dict[str, Any]:
        return self._body


def accepted(wamid: str) -> _Resp:
    return _Resp(200, {"messaging_product": "whatsapp", "messages": [{"id": wamid}]})


def duplicate_button_title() -> _Resp:
    return _Resp(400, {
        "error": {
            "message": "(#100) Invalid parameter",
            "type": "OAuthException",
            "code": 100,
            "error_data": {
                "messaging_product": "whatsapp",
                "details": DUP_TITLE_DETAIL,
            },
            "fbtrace_id": "AbCdEf",
        }
    })


class FakeProvider:
    """Records every POST payload and answers from ``script(payload, n)``."""

    def __init__(self, script: Callable[[Dict[str, Any], int], _Resp]) -> None:
        self.script = script
        self.calls: List[Dict[str, Any]] = []

    async def post(self, url: str, headers=None, json=None, params=None) -> _Resp:
        payload = dict(json or {})
        self.calls.append(payload)
        return self.script(payload, len(self.calls))

    @property
    def types(self) -> List[str]:
        return [str(p.get("type")) for p in self.calls]

    def text_bodies(self) -> List[str]:
        return [
            str(((p.get("text") or {}).get("body")) or "")
            for p in self.calls if p.get("type") == "text"
        ]


class _FakeClient:
    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, **kwargs: Any) -> _Resp:
        return await self._provider.post(url, **kwargs)


def _script_accept_all(payload: Dict[str, Any], n: int) -> _Resp:
    return accepted(f"wamid.accepted.{n}")


def _script_reject_interactive(payload: Dict[str, Any], n: int) -> _Resp:
    if payload.get("type") == "interactive":
        return duplicate_button_title()
    return accepted(f"wamid.accepted.{n}")


def _script_meta_duplicate_title_check(payload: Dict[str, Any], n: int) -> _Resp:
    """Behave like Meta: reject only when visible titles really collide."""
    if payload.get("type") == "interactive":
        btns = ((payload.get("interactive") or {}).get("action") or {}).get("buttons") or []
        titles = [str((b.get("reply") or {}).get("title") or "") for b in btns]
        if len(titles) != len(set(titles)):
            return duplicate_button_title()
    return accepted(f"wamid.accepted.{n}")


def _script_timeout(payload: Dict[str, Any], n: int) -> _Resp:
    raise httpx.ReadTimeout("provider read timed out after dispatch may have begun")


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────


class Evidence:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider
        self.saved: List[Dict[str, Any]] = []
        self.stamps: List[Dict[str, Any]] = []
        self.wire_attempts: List[Dict[str, Any]] = []
        self.catalog_sends: List[Dict[str, Any]] = []
        self.outcomes: List[Dict[str, Any]] = []
        self.traces: List[Any] = []

    @property
    def outbound_rows(self) -> List[Dict[str, Any]]:
        return [r for r in self.saved if r["direction"] == "outbound"]

    def last_stamp_block(self) -> Optional[Dict[str, Any]]:
        if not self.stamps:
            return None
        s = self.stamps[-1]
        return build_provider_send_block(
            classification=s["classification"],
            response_body=s["response_body"],
            wamid=s["wamid"],
            operation=s["operation"],
            error_text=s.get("error_text"),
        )

    def accepted_wamids(self) -> List[str]:
        return [
            str(s["wamid"]) for s in self.stamps
            if s["classification"] == "ok" and s.get("wamid")
        ]

    @property
    def last_outcome(self) -> Dict[str, Any]:
        return dict(self.outcomes[-1]) if self.outcomes else {}


@contextmanager
def incident_ctx(
    *,
    brain_return: Dict[str, Any],
    script: Callable[[Dict[str, Any], int], _Resp],
    brain_state: Optional[Dict[str, Any]] = None,
    catalog_send_ok: bool = True,
    guard_empties_reply: bool = False,
):
    from observability import rate_limiter as _rl
    from modules.ai.brain.postprocess.post_compose_guard_pipeline import (
        PostComposeGuardEvent,
        PostComposeGuardResult,
    )

    clear_outbound_dedup()
    with _rl._store_lock:
        _rl._store.clear()

    provider = FakeProvider(script)
    ev = Evidence(provider)
    convo = _merchant_handler_convo(
        extra_metadata={"brain_state": _brain_state() if brain_state is None else brain_state},
    )
    db = _merchant_handler_db()

    def _save(_db, phone, body, direction, *args, **kwargs):
        ev.saved.append({
            "phone": phone, "body": body, "direction": direction,
            "extra_metadata": dict(kwargs.get("extra_metadata") or {}),
        })
        return len(ev.saved)

    def _stamp(_db, **kwargs):
        ev.stamps.append(dict(kwargs))
        return 1

    def _wire(**kwargs):
        ev.wire_attempts.append(dict(kwargs))

    async def _catalog_send(**kwargs):
        ev.catalog_sends.append(dict(kwargs))
        audit = kwargs.get("delivery_audit")
        if catalog_send_ok and isinstance(audit, dict):
            audit["catalog_card_sent_count"] = int(audit.get("catalog_card_sent_count", 0)) + 1
        return bool(catalog_send_ok)

    with ExitStack() as stack:
        mock_brain, _state = stack.enter_context(_merchant_handler_patch_ctx(convo=convo))
        mock_brain.return_value.process = AsyncMock(return_value=dict(brain_return))
        # Real send path down to the transport.
        stack.enter_context(patch(
            "routers.whatsapp_webhook._send_whatsapp_message", new=_REAL_SEND_TEXT,
        ))
        stack.enter_context(patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=_REAL_PROVIDER_POST,
        ))
        stack.enter_context(patch(
            "services.whatsapp_platform.service.httpx.AsyncClient",
            new=lambda **kw: _FakeClient(provider),
        ))
        # Persistence spies (row count / status / wamid evidence).
        stack.enter_context(patch(
            "routers.whatsapp_webhook.StateManager.save_message", side_effect=_save,
        ))
        stack.enter_context(patch(
            "core.outbound_send_status.stamp_outbound_send_status", side_effect=_stamp,
        ))
        stack.enter_context(patch(
            "core.outbound_wire_audit.record_wire_attempt", side_effect=_wire,
        ))
        stack.enter_context(patch(
            "routers.whatsapp_webhook._try_send_catalog_product", new=_catalog_send,
        ))
        if guard_empties_reply:
            # Production first divergence: the webhook-level availability /
            # truth guard pipeline reduced the composed catalog answer to "".
            def _guard_empties(**kwargs):
                return PostComposeGuardResult(
                    reply="",
                    events=[PostComposeGuardEvent(
                        guard="product_availability_truth_guard",
                        acted=True, modified=True, suppressed_send=False,
                        reason="availability_claim_unverified",
                        layer="webhook_truth_guards",
                    )],
                    primary_applied=True,
                )

            stack.enter_context(patch(
                "modules.ai.brain.postprocess.post_compose_guard_pipeline."
                "run_post_compose_truth_guards",
                side_effect=_guard_empties,
            ))
        # Catalog resolution — verified candidates only.
        stack.enter_context(patch(
            "services.product_resolver.resolve_by_query",
            side_effect=lambda _db, _tid, q, **k: _resolution_for(q),
        ))
        stack.enter_context(patch(
            "services.product_resolver.resolve_best_effort", return_value=None,
        ))
        stack.enter_context(patch(
            "core.fail_closed_visual_presentation.bind_structured_visual_referent",
            return_value=SimpleNamespace(canonical_present=False),
        ))
        stack.enter_context(patch(
            "modules.ai.brain.catalog.catalog_product_card_filter.filter_product_card_attachments",
            side_effect=lambda atts, **k: SimpleNamespace(
                attachments=list(atts), dropped=0, evidence={},
            ),
        ))
        # Delivery-outcome spy (only present once the fix is applied).
        try:
            import core.product_reply_recovery as _prr  # noqa: PLC0415

            _real_outcome = _prr.record_product_reply_outcome

            def _outcome_spy(*args, **kwargs):
                out = _real_outcome(*args, **kwargs)
                ev.outcomes.append(dict(out or {}))
                return out

            stack.enter_context(patch(
                "core.product_reply_recovery.record_product_reply_outcome",
                side_effect=_outcome_spy,
            ))
        except ImportError:
            pass

        ev.db = db  # type: ignore[attr-defined]
        yield ev


def run_turn(ev: Evidence, *, text: str, event_id: str, phone: str = "966500000099"):
    """One inbound provider event through the merchant handler."""
    with inbound_lifecycle_trace(
        provider="meta",
        phone_number_id=PHONE_ID,
        msg={"id": event_id, "type": "text", "text": {"body": text}, "from": phone},
    ) as trace:
        # The dispatcher persists the inbound row before routing to the brain.
        record_lifecycle(EVENT_MESSAGE_SAVED, conversation_id=42)
        asyncio.run(_handle_merchant_message(
            phone_id=PHONE_ID, to=phone, text=text, tenant_id=TENANT_ID,
            db=ev.db, wa_msg_id=event_id,  # type: ignore[attr-defined]
        ))
    ev.traces.append(trace)
    return trace


def _trace_events(trace) -> List[str]:
    return [e.name for e in trace.events]


def _trace_detail(trace, name: str) -> str:
    for e in trace.events:
        if e.name == name:
            return e.detail
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 1. Exact broad query with empty guarded text (production path 1)
# ─────────────────────────────────────────────────────────────────────────────


def test_broad_query_empty_guarded_text_gets_one_grounded_text_reply() -> None:
    with incident_ctx(
        brain_return=_brain_return(reply=COMPOSED_CATALOG_ANSWER),
        script=_script_accept_all,
        guard_empties_reply=True,
    ) as ev:
        trace = run_turn(ev, text=INPUT_BROAD, event_id="wamid.in.broad.1")

    # Guard stayed intact: the arbitrary first-candidate card was rejected.
    assert ev.catalog_sends == [], "no product card may be sent for the broad query"
    assert ev.last_outcome.get("fail_closed_visual_suppression_reason") == (
        "stale_or_unrelated_product"
    )

    # Exactly one provider call: the deterministic grounded plain-text recovery.
    assert ev.provider.types == ["text"], ev.provider.types
    body = ev.provider.text_bodies()[0]
    for row in _candidates():
        assert row["title"] in body
        assert row["price"] in body
    assert "http" not in body, "no URL may be invented in the grounded list"
    assert body.count("فستان") == 2, "same-title products stay distinguishable"

    # Provider accepted it; one wamid recorded on the persisted outbound row.
    assert ev.accepted_wamids() == ["wamid.accepted.1"]
    assert len(ev.outbound_rows) == 1
    assert ev.outbound_rows[0]["body"] == body
    row_meta = ev.outbound_rows[0]["extra_metadata"]
    assert row_meta.get("compose_source") == "fallback_deterministic"
    assert row_meta.get("fallback_reason")
    assert row_meta.get("fallback_action_type") == "catalog_grounded_text_recovery"
    block = ev.last_stamp_block()
    assert block and block["status"] == "sent" and block["wamid"] == "wamid.accepted.1"

    # Delivery outcome + lifecycle are explicit, not a generic end_ok.
    out = ev.last_outcome
    assert out["product_reply_outcome"] == "suppressed_text_recovered"
    assert out["text_recovery_attempts"] == 1
    assert out["text_recovery_wamid"] == "wamid.accepted.1"
    assert out["final_delivery_mode"] == "text_only"
    assert trace.final_token == "end_delivery_recovered"
    assert "delivery_text_recovered" in _trace_events(trace)
    assert EVENT_END_OK not in _trace_events(trace)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Repeated legitimate customer input (distinct provider event ids)
# ─────────────────────────────────────────────────────────────────────────────


def test_repeated_legitimate_input_gets_its_own_single_reply() -> None:
    with incident_ctx(
        brain_return=_brain_return(reply=COMPOSED_CATALOG_ANSWER),
        script=_script_accept_all,
        guard_empties_reply=True,
    ) as ev:
        t1 = run_turn(ev, text=INPUT_BROAD, event_id="wamid.in.broad.A")
        t2 = run_turn(ev, text=INPUT_BROAD, event_id="wamid.in.broad.B")
        # A webhook replay of event B (same inbound id, same body) must not
        # reach the provider again.
        t3 = run_turn(ev, text=INPUT_BROAD, event_id="wamid.in.broad.B")

    assert ev.provider.types == ["text", "text"], ev.provider.types
    assert ev.accepted_wamids()[:2] == ["wamid.accepted.1", "wamid.accepted.2"]
    assert t1.final_token == "end_delivery_recovered"
    assert t2.final_token == "end_delivery_recovered"
    assert ev.outcomes[0]["text_recovery_wamid"] == "wamid.accepted.1"
    assert ev.outcomes[1]["text_recovery_wamid"] == "wamid.accepted.2"
    # Replay: outbound dedup returned the prior wamid without a new POST.
    assert len(ev.provider.calls) == 2
    assert ev.outcomes[2]["text_recovery_attempts"] == 1
    assert ev.outcomes[2].get("text_recovery_duplicate_suppressed") is True
    assert t3.final_token == "end_delivery_recovered"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Duplicate product titles — builder + wire-boundary dedup
# ─────────────────────────────────────────────────────────────────────────────


def test_pick_buttons_dedupe_visible_titles_preserving_order_and_ids() -> None:
    buttons = build_standard_pick_buttons(_candidates())
    titles = [b["reply"]["title"] for b in buttons]
    ids = [b["reply"]["id"] for b in buttons]
    assert titles == ["فستان", "جاكيت"], titles
    assert ids == ["pick_1", "pick_3"], ids
    assert len(set(ids)) == len(ids)


def test_pick_buttons_scan_past_duplicates_for_next_unique_candidate() -> None:
    rows = _candidates() + [{"id": 14, "title": "حذاء رياضي أبيض", "price": "150"}]
    buttons = build_standard_pick_buttons(rows)
    titles = [b["reply"]["title"] for b in buttons]
    # The existing label compactor may normalise Arabic letterforms; the
    # evidence here is uniqueness + order + candidate index, not spelling.
    assert titles[:2] == ["فستان", "جاكيت"]
    assert titles[2].startswith("حذاء رياضي")
    assert len(set(titles)) == 3
    assert [b["reply"]["id"] for b in buttons] == ["pick_1", "pick_3", "pick_4"]


def test_pick_buttons_treat_whitespace_and_unicode_variants_as_duplicates() -> None:
    rows = [
        {"id": 1, "title": "فستان"},
        {"id": 2, "title": "  فستان   "},
        {"id": 3, "title": "فُستان"},      # diacritics
        {"id": 4, "title": "جاكيت"},
    ]
    buttons = build_standard_pick_buttons(rows)
    assert [b["reply"]["id"] for b in buttons] == ["pick_1", "pick_4"]


def test_wire_boundary_never_sends_duplicate_normalized_titles() -> None:
    from core.wa_link_buttons import whatsapp_reply_buttons_payload

    wire = whatsapp_reply_buttons_payload([
        {"type": "reply", "reply": {"id": "pick_1", "title": "فستان"}},
        {"type": "reply", "reply": {"id": "pick_2", "title": " فستان "}},
        {"type": "reply", "reply": {"id": "pick_3", "title": "جاكيت"}},
    ])
    assert [b["reply"]["title"] for b in wire] == ["فستان", "جاكيت"]
    assert [b["reply"]["id"] for b in wire] == ["pick_1", "pick_3"]


def test_wire_boundary_keeps_distinct_titles_and_order() -> None:
    from core.wa_link_buttons import whatsapp_reply_buttons_payload

    src = [
        {"type": "reply", "reply": {"id": "a", "title": "قميص قطني أزرق"}},
        {"type": "reply", "reply": {"id": "b", "title": "عطر ورد 100ml"}},
        {"type": "reply", "reply": {"id": "c", "title": "حذاء رياضي أبيض"}},
    ]
    assert whatsapp_reply_buttons_payload(src) == src


# ─────────────────────────────────────────────────────────────────────────────
# 11. Exact production-shaped reproduction of path 2 (duplicate titles)
# ─────────────────────────────────────────────────────────────────────────────


def test_what_do_you_have_duplicate_titles_never_reach_meta() -> None:
    """Meta-like provider: rejects only real duplicate titles. After the fix the
    interactive payload is unique, so the first attempt is accepted."""
    brain = _brain_return(
        reply=GROUNDED_TEXT, buttons=build_standard_pick_buttons(_candidates()),
    )
    with incident_ctx(brain_return=brain, script=_script_meta_duplicate_title_check) as ev:
        trace = run_turn(ev, text=INPUT_WHAT, event_id="wamid.in.what.1")

    assert ev.provider.types == ["interactive"], ev.provider.types
    btns = ev.provider.calls[0]["interactive"]["action"]["buttons"]
    titles = [b["reply"]["title"] for b in btns]
    ids = [b["reply"]["id"] for b in btns]
    assert len(titles) == len(set(titles)), titles
    assert titles == ["فستان", "جاكيت"]
    assert ids == ["pick_1", "pick_3"]
    assert ev.accepted_wamids() == ["wamid.accepted.1"]
    assert ev.last_outcome["product_reply_outcome"] == "rich_accepted"
    assert trace.final_token == EVENT_END_OK


# ─────────────────────────────────────────────────────────────────────────────
# 4. Definitive Meta rejection → exactly one plain-text recovery
# ─────────────────────────────────────────────────────────────────────────────


def test_definitive_interactive_rejection_recovers_with_guarded_text_once() -> None:
    brain = _brain_return(
        reply=GROUNDED_TEXT, buttons=build_standard_pick_buttons(_candidates()),
    )
    with incident_ctx(brain_return=brain, script=_script_reject_interactive) as ev:
        trace = run_turn(ev, text=INPUT_WHAT, event_id="wamid.in.what.2")

    # Exactly: one interactive attempt, then one plain-text attempt.
    assert ev.provider.types == ["interactive", "text"], ev.provider.types
    assert ev.provider.text_bodies() == [GROUNDED_TEXT], "reuse the guarded text verbatim"
    assert ev.catalog_sends == []

    # The accepted plain-text wamid is persisted on the same outbound row.
    assert len(ev.outbound_rows) == 1, "no duplicate outbound rows"
    assert ev.accepted_wamids() == ["wamid.accepted.2"]
    block = ev.last_stamp_block()
    assert block and block["status"] == "sent" and block["wamid"] == "wamid.accepted.2"

    # The original provider error is preserved and NOT classified as invalid_phone.
    out = ev.last_outcome
    assert out["product_reply_outcome"] == "rich_rejected_text_recovered"
    assert out["text_recovery_attempts"] == 1
    assert out["text_recovery_wamid"] == "wamid.accepted.2"
    err = out["original_provider_error"]
    assert err["classification"] == "non_2xx"
    assert err["http_status"] == 400
    assert err["code"] == 100
    assert DUP_TITLE_DETAIL in str(err["detail"])
    assert err["key"] == "invalid_payload"
    assert err["key"] != "invalid_phone"
    first_stamp = build_provider_send_block(
        classification=ev.stamps[0]["classification"],
        response_body=ev.stamps[0]["response_body"],
        wamid=ev.stamps[0]["wamid"],
        operation=ev.stamps[0]["operation"],
    )
    assert first_stamp["status"] == "failed"
    assert first_stamp["error"]["key"] == "invalid_payload"

    # Lifecycle: distinct successful recovery, not a generic end_ok.
    assert trace.final_token == "end_delivery_recovered"
    assert EVENT_END_OK not in _trace_events(trace)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Ambiguous provider outcome — never resend
# ─────────────────────────────────────────────────────────────────────────────


def test_ambiguous_timeout_after_interactive_dispatch_never_resends() -> None:
    brain = _brain_return(
        reply=GROUNDED_TEXT, buttons=build_standard_pick_buttons(_candidates()),
    )
    with incident_ctx(brain_return=brain, script=_script_timeout) as ev:
        trace = run_turn(ev, text=INPUT_WHAT, event_id="wamid.in.what.3")

    assert ev.provider.types == ["interactive"], "no automatic resend after a timeout"
    assert ev.accepted_wamids() == []
    out = ev.last_outcome
    assert out["product_reply_outcome"] == "ambiguous_provider_outcome"
    assert out["text_recovery_attempts"] == 0
    assert out["original_provider_error"]["classification"] == "exception"
    assert "ReadTimeout" in str(out["original_provider_error"]["detail"])
    block = ev.last_stamp_block()
    assert block and block["status"] == "failed" and block["classification"] == "exception"
    assert trace.final_token == "end_delivery_failed"
    assert "ambiguous_provider_outcome" in _trace_detail(trace, "end_delivery_failed")
    assert EVENT_END_OK not in _trace_events(trace)


def test_ambiguous_timeout_on_text_recovery_is_not_retried() -> None:
    with incident_ctx(
        brain_return=_brain_return(reply=COMPOSED_CATALOG_ANSWER),
        script=_script_timeout,
        guard_empties_reply=True,
    ) as ev:
        trace = run_turn(ev, text=INPUT_BROAD, event_id="wamid.in.broad.timeout")

    assert ev.provider.types == ["text"], "exactly one recovery attempt"
    assert ev.accepted_wamids() == []
    out = ev.last_outcome
    assert out["product_reply_outcome"] == "ambiguous_provider_outcome"
    assert out["text_recovery_attempts"] == 1
    assert out["text_recovery_wamid"] is None
    assert trace.final_token == "end_delivery_failed"
    assert EVENT_END_OK not in _trace_events(trace)


# ─────────────────────────────────────────────────────────────────────────────
# 6. No eligible catalog candidates — nothing fabricated
# ─────────────────────────────────────────────────────────────────────────────


def test_no_eligible_candidates_fabricates_nothing() -> None:
    brain = _brain_return(reply=COMPOSED_CATALOG_ANSWER, catalog_product_ids=[])
    with incident_ctx(
        brain_return=brain, script=_script_accept_all, brain_state=_brain_state([]),
        guard_empties_reply=True,
    ) as ev:
        trace = run_turn(ev, text=INPUT_BROAD, event_id="wamid.in.broad.empty")

    assert ev.provider.calls == [], "no product or commercial fact may be invented"
    assert ev.catalog_sends == []
    assert ev.outbound_rows == []
    out = ev.last_outcome
    assert out["product_reply_outcome"] == "terminal_delivery_failure"
    assert out["text_recovery_attempts"] == 0
    assert out["text_recovery_skip_reason"] == "no_eligible_candidates"
    assert trace.final_token == "end_delivery_failed"
    assert EVENT_END_OK not in _trace_events(trace)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Social-message regression — text-only delivery unchanged
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("inbound", [INPUT_SALAM, INPUT_HOWDY])
def test_social_messages_keep_single_text_delivery(inbound: str) -> None:
    social_reply = "وعليكم السلام ورحمة الله، حياك الله"
    brain = _brain_return(
        reply=social_reply, decision_action="social_reply", intent="greeting",
        catalog_product_ids=[], chosen_path="social_compose",
    )
    state = _brain_state([])
    state.update({"last_intent": "greeting", "last_action": "social_reply"})
    with incident_ctx(brain_return=brain, script=_script_accept_all, brain_state=state) as ev:
        trace = run_turn(ev, text=inbound, event_id=f"wamid.in.social.{len(inbound)}")

    assert ev.provider.types == ["text"], ev.provider.types
    # Existing presentation policies (e.g. marketing emoji) may decorate the
    # body; the delivered text must still be the persona reply, sent once.
    assert social_reply in ev.provider.text_bodies()[0]
    assert ev.accepted_wamids() == ["wamid.accepted.1"]
    assert ev.catalog_sends == []
    assert ev.outcomes == [], "social turns never enter product-reply recovery"
    assert trace.final_token == EVENT_END_OK


# ─────────────────────────────────────────────────────────────────────────────
# 8. Specific-product request keeps its correctly matched card
# ─────────────────────────────────────────────────────────────────────────────


def test_specific_product_request_still_sends_matching_card() -> None:
    card = {
        "kind": "product_card", "id": 11, "title": "فستان", "media_type": "image",
        "file_url": "https://example.test/images/11.jpg", "caption": "فستان\nالسعر: 250 ر.س",
        "product_url": "https://example.test/products/11", "price": "250",
        "in_stock": True, "external_id": "p-11", "confidence": "fts",
        "dispatch_source": "single_resolved_presentation",
    }
    state = _brain_state([_candidates()[0]])
    state["current_product_focus"] = {"id": 11, "title": "فستان", "set_turn": 3}
    specific_text = "فستان متوفر بسعر 250 ر.س"
    brain = _brain_return(
        reply=specific_text, product_cards=[card], catalog_product_ids=[11],
    )
    with incident_ctx(brain_return=brain, script=_script_accept_all, brain_state=state) as ev:
        trace = run_turn(ev, text=INPUT_SPECIFIC, event_id="wamid.in.specific.1")

    assert len(ev.catalog_sends) == 1
    assert ev.catalog_sends[0]["attachment"]["title"] == "فستان"
    assert ev.provider.types == ["text"], "text + card; no text recovery duplicate"
    assert ev.provider.text_bodies() == [specific_text]
    assert ev.last_outcome["product_reply_outcome"] == "rich_accepted"
    assert ev.last_outcome["text_recovery_attempts"] == 0
    assert trace.final_token == EVENT_END_OK


# ─────────────────────────────────────────────────────────────────────────────
# 9. Stale / unrelated card stays rejected even when text was delivered
# ─────────────────────────────────────────────────────────────────────────────


def test_stale_card_rejected_and_delivered_text_is_not_duplicated() -> None:
    brain = _brain_return(reply=GROUNDED_TEXT)
    with incident_ctx(brain_return=brain, script=_script_accept_all) as ev:
        trace = run_turn(ev, text=INPUT_BROAD, event_id="wamid.in.broad.text")

    assert ev.catalog_sends == [], "stale_or_unrelated_product card must stay rejected"
    assert ev.provider.types == ["text"], "grounded text delivered once, no recovery duplicate"
    assert ev.provider.text_bodies() == [GROUNDED_TEXT]
    out = ev.last_outcome
    assert out["fail_closed_visual_suppression_reason"] == "stale_or_unrelated_product"
    assert out["text_recovery_attempts"] == 0
    assert out["product_reply_outcome"] == "text_accepted"
    assert trace.final_token == EVENT_END_OK


def test_stale_card_rejection_still_blocks_arbitrary_cta_rescue_after_recovery() -> None:
    with incident_ctx(
        brain_return=_brain_return(reply=COMPOSED_CATALOG_ANSWER),
        script=_script_accept_all,
        guard_empties_reply=True,
    ) as ev:
        run_turn(ev, text=INPUT_BROAD, event_id="wamid.in.broad.cta")

    # Only the plain-text recovery; no "عرض المنتج" CTA for an unselected product.
    assert ev.provider.types == ["text"]
    for payload in ev.provider.calls:
        assert payload.get("type") != "interactive"
