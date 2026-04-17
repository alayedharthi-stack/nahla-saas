"""
tests/test_whatsapp_send_tenant_propagation.py
──────────────────────────────────────────────
Locks down the fix for "tenant_id=None at send time".

Production was logging:

    [Engine]      tenant=1
    [WA token]    op=send_message tenant=None source=platform
    [SEND_DEBUG]  tenant_id=None  store=unknown ...

…even though the engine had `tenant=1` cleanly resolved upstream. The
root cause was that the deterministic CTA / interactive helpers
(`_send_checkout_cta`, `_send_cta_url`, `_send_welcome_menu`,
`_send_trial_cta`, `_send_plans_message`, `_send_interactive_reply`)
did NOT accept `_tenant_id` / `_db` and called `_post_wa(phone_id,
payload)` with no tenant context — so `_post_wa` could not load the
merchant's `WhatsAppConnection` row and fell back to the platform
system-user token. Meta then rejected the send with code=100,
subcode=33 ("Object … does not exist or you don't have permissions").

These tests guarantee:

  1. Every helper now forwards `_tenant_id` and `_db` through to
     `provider_send_message`.
  2. When a caller forgets to pass `_tenant_id`, `_post_wa` self-
     resolves the WhatsAppConnection by `phone_number_id` so we still
     pick up the merchant token instead of falling back to platform.
  3. On the canonical 400 / GraphMethodException (code=100,
     subcode=33), `_post_wa` attempts `POST /{phone_id}/register` once
     per process and retries the send. If the phone was simply not
     registered yet, this self-heals the send without operator
     intervention.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from models import Base, Tenant, WhatsAppConnection  # noqa: E402
from routers import whatsapp_webhook as wh  # noqa: E402


# ─────────────────────────── helpers ───────────────────────────

def _make_db() -> tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    saved: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig_type in saved:
        col.type = orig_type
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db, *, tenant_id: int = 7) -> Tenant:
    t = Tenant(id=tenant_id, name=f"T{tenant_id}", is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_wa_conn(
    db, *, tenant_id: int, phone_number_id: str = "PHONE-X",
    waba_id: str = "WABA-X", access_token: str = "merchant-tok-abc",
    connection_type: str = "cloud_api",
) -> WhatsAppConnection:
    kwargs = dict(
        tenant_id                    = tenant_id,
        phone_number_id              = phone_number_id,
        whatsapp_business_account_id = waba_id,
        access_token                 = access_token,
        connection_type              = connection_type,
        status                       = "connected",
        webhook_verified             = True,
        phone_number                 = "+966500000000",
        business_display_name        = "Test Store",
    )
    if hasattr(WhatsAppConnection, "sending_enabled"):
        kwargs["sending_enabled"] = True
    conn = WhatsAppConnection(**kwargs)
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


class _CapturingCtx:
    """Minimal stand-in for WhatsAppTokenContext."""
    def __init__(self, source: str = "merchant_oauth", token: str = "TOK") -> None:
        self.source = source
        self.token = token
        self.token_status = "healthy"
        self.expires_at = None
        self.oauth_session_status = "healthy"
        self.oauth_session_message = None


def _patch_send(monkeypatch, *, response: dict | None = None,
                track: list | None = None,
                ctx: _CapturingCtx | None = None):
    """Replace `provider_send_message` with a recorder that captures the
    arguments _post_wa actually passes (tenant_id, conn, phone_id) and
    returns whatever response we want to simulate."""
    captured = track if track is not None else []
    fake_ctx = ctx or _CapturingCtx()
    fake_resp = response or {"messages": [{"id": "wamid.X"}]}

    async def fake_provider(db, conn, *, tenant_id, operation, phone_id,
                            payload, prefer_platform=False, timeout=15):
        captured.append({
            "tenant_id":      tenant_id,
            "operation":      operation,
            "phone_id":       phone_id,
            "conn_tenant_id": getattr(conn, "tenant_id", None),
            "conn_phone_id":  getattr(conn, "phone_number_id", None),
        })
        return fake_resp, fake_ctx

    monkeypatch.setattr(wh, "provider_send_message", fake_provider)
    # Disable the in-process throttler — we want every call to go through.
    import observability.rate_limiter as rl  # noqa: PLC0415
    monkeypatch.setattr(rl, "check_rate_limit", lambda *_a, **_kw: True)
    return captured


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────── 1. helpers forward tenant_id ───────────────────────

class TestHelpersForwardTenantContext:
    @pytest.mark.parametrize("helper_name,extra_kwargs", [
        ("_send_checkout_cta", {}),
        ("_send_trial_cta",    {}),
        ("_send_welcome_menu", {}),
        ("_send_interactive_reply", {
            "body_text": "x",
            "buttons":   [{"type": "reply", "reply": {"id": "a", "title": "A"}}],
        }),
        ("_send_cta_url", {
            "body_text": "x", "btn_label": "L", "btn_url": "https://x",
        }),
        ("_send_whatsapp_message", {"text": "hi"}),
    ])
    def test_helper_passes_tenant_id_through(self, monkeypatch,
                                             helper_name, extra_kwargs) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db, tenant_id=42)
            _seed_wa_conn(db, tenant_id=t.id, phone_number_id="PHONE-A")
            captured = _patch_send(monkeypatch)

            helper = getattr(wh, helper_name)
            kwargs = {
                "phone_id": "PHONE-A",
                "to":       "966555000000",
                "_tenant_id": t.id,
                "_db":        db,
                **extra_kwargs,
            }
            _run(helper(**kwargs))

            # Every helper should result in at least one provider call,
            # and every call must carry tenant_id=42 + the merchant conn.
            assert captured, f"{helper_name} did not produce any provider call"
            for call in captured:
                assert call["tenant_id"] == 42, (
                    f"{helper_name} dropped tenant_id (got {call['tenant_id']})"
                )
                assert call["conn_tenant_id"] == 42, (
                    f"{helper_name} did not load the merchant connection "
                    f"(conn.tenant_id={call['conn_tenant_id']})"
                )
                assert call["conn_phone_id"] == "PHONE-A"
        finally:
            db.close()
            engine.dispose()

    def test_plans_message_forwards_tenant_through_both_sub_sends(
        self, monkeypatch
    ) -> None:
        """`_send_plans_message` chains a text send and a CTA send — both
        must carry the tenant_id."""
        db, engine = _make_db()
        try:
            t = _seed_tenant(db, tenant_id=11)
            _seed_wa_conn(db, tenant_id=t.id, phone_number_id="PHONE-P")
            captured = _patch_send(monkeypatch)

            _run(wh._send_plans_message(
                phone_id="PHONE-P", to="966555000000",
                db=db, _tenant_id=t.id,
            ))
            assert len(captured) == 2
            for call in captured:
                assert call["tenant_id"] == 11
                assert call["conn_tenant_id"] == 11
        finally:
            db.close()
            engine.dispose()


# ─────────────────── 2. _post_wa self-resolves by phone_id ───────────────────

class TestPostWaSelfResolvesByPhoneId:
    def test_no_tenant_id_falls_back_to_phone_id_lookup(self, monkeypatch) -> None:
        """When a (legacy) caller invokes `_post_wa` without any tenant
        context, we should still find the merchant connection by
        `phone_number_id` instead of degrading to the platform token.
        This is the safety-net for `tenant_id=None` regressions."""
        db, engine = _make_db()
        try:
            _seed_tenant(db, tenant_id=99)
            _seed_wa_conn(db, tenant_id=99, phone_number_id="PHONE-Z")
            captured = _patch_send(monkeypatch)
            # Make sure the fallback get_db() does not return a real session
            # — we want the inner code to use the resolved conn from our db
            # via the explicit _db kw we pass.
            monkeypatch.setattr(wh, "get_db", lambda: iter([db]))

            _run(wh._post_wa(
                phone_id="PHONE-Z",
                payload={"messaging_product": "whatsapp", "to": "x",
                         "type": "text", "text": {"body": "hi"}},
                # _tenant_id intentionally OMITTED
                _db=db,
            ))
            assert captured, "no provider call was made"
            call = captured[0]
            assert call["tenant_id"] == 99, (
                "phone_id self-resolution failed — "
                f"expected tenant_id=99, got {call['tenant_id']}"
            )
            assert call["conn_tenant_id"] == 99
            assert call["conn_phone_id"] == "PHONE-Z"
        finally:
            db.close()
            engine.dispose()


# ────────────── 3. auto-register self-heal on code=100/subcode=33 ──────────────

class TestAutoRegisterSelfHeal:
    def test_400_code100_subcode33_triggers_register_and_retry(
        self, monkeypatch
    ) -> None:
        # Reset the per-process cache so this test is order-independent.
        wh._AUTO_REREGISTERED_PHONE_IDS.clear()
        db, engine = _make_db()
        try:
            _seed_tenant(db, tenant_id=5)
            _seed_wa_conn(db, tenant_id=5, phone_number_id="PHONE-NEW")

            calls: list[dict] = []
            ctx = _CapturingCtx(source="merchant_oauth", token="T-NEW")
            attempts = {"n": 0}

            async def fake_provider(db, conn, *, tenant_id, operation,
                                    phone_id, payload, prefer_platform=False,
                                    timeout=15):
                attempts["n"] += 1
                calls.append({
                    "tenant_id": tenant_id,
                    "operation": operation,
                    "phone_id":  phone_id,
                })
                if attempts["n"] == 1:
                    return ({
                        "error": {
                            "message": "Unsupported post request. Object with "
                                       f"ID '{phone_id}' does not exist, cannot "
                                       "be loaded due to missing permissions, or "
                                       "does not support this operation.",
                            "type":          "GraphMethodException",
                            "code":          100,
                            "error_subcode": 33,
                            "fbtrace_id":    "abc",
                        },
                    }, ctx)
                return ({"messages": [{"id": "wamid.OK"}]}, ctx)

            monkeypatch.setattr(wh, "provider_send_message", fake_provider)
            import observability.rate_limiter as rl  # noqa: PLC0415
            monkeypatch.setattr(rl, "check_rate_limit", lambda *_a, **_kw: True)

            register_calls: list[tuple] = []
            def fake_register(phone_id, token, tenant_id):
                register_calls.append((phone_id, token, tenant_id))
                return True, None

            import services.whatsapp_connection_service as wa_svc  # noqa: PLC0415
            monkeypatch.setattr(wa_svc, "register_phone_number", fake_register)

            _run(wh._send_whatsapp_message(
                phone_id="PHONE-NEW", to="966555000000", text="hi",
                _tenant_id=5, _db=db,
            ))

            assert len(register_calls) == 1, (
                "auto-register was not invoked on code=100/subcode=33 — got "
                f"{len(register_calls)} register attempts"
            )
            assert register_calls[0][0] == "PHONE-NEW"
            assert register_calls[0][2] == 5  # tenant_id flowed through
            assert attempts["n"] == 2, (
                "send was not retried after successful auto-register — "
                f"attempts={attempts['n']}"
            )
            assert calls[1]["operation"] == "send_message_retry"
        finally:
            db.close()
            engine.dispose()

    def test_register_attempted_only_once_per_phone_id(
        self, monkeypatch
    ) -> None:
        """Even if the same broken phone_id sees many failing sends, we
        must only call /register once per process — otherwise we DDoS
        Meta on a token-permissions issue we cannot fix from code."""
        wh._AUTO_REREGISTERED_PHONE_IDS.clear()
        db, engine = _make_db()
        try:
            _seed_tenant(db, tenant_id=8)
            _seed_wa_conn(db, tenant_id=8, phone_number_id="PHONE-LOOP")

            ctx = _CapturingCtx(source="platform", token="PLAT")

            async def always_fail(db, conn, *, tenant_id, operation, phone_id,
                                  payload, prefer_platform=False, timeout=15):
                return ({
                    "error": {"code": 100, "error_subcode": 33,
                              "type": "GraphMethodException",
                              "message": "boom"},
                }, ctx)

            monkeypatch.setattr(wh, "provider_send_message", always_fail)
            import observability.rate_limiter as rl  # noqa: PLC0415
            monkeypatch.setattr(rl, "check_rate_limit", lambda *_a, **_kw: True)

            register_calls: list[tuple] = []
            import services.whatsapp_connection_service as wa_svc  # noqa: PLC0415
            monkeypatch.setattr(
                wa_svc, "register_phone_number",
                lambda pid, tok, tid: (register_calls.append((pid, tid)) or (False, "no-perm")),
            )

            for _ in range(3):
                _run(wh._send_whatsapp_message(
                    phone_id="PHONE-LOOP", to="966555000000", text="hi",
                    _tenant_id=8, _db=db,
                ))

            assert len(register_calls) == 1, (
                f"register was retried for the same phone_id ({len(register_calls)} times)"
            )
        finally:
            db.close()
            engine.dispose()
