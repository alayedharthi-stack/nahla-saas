"""
backend/tests/test_outbound_body_sync.py
─────────────────────────────────────────
Regression suite for ``sync_outbound_body_to_final`` (May 2026 #32).

Production case
───────────────
Tenant 33 customer asked "رابط المتجر الأساسي للعسل". The merchant
saw "هذا متجرنا 🌷 نكمل إنشاء طلب المنتج الآن؟" in the Nahla inbox
with NO link, while the customer's WhatsApp received the link
(injected by ``apply_store_link_safety_net``).

Root cause
──────────
``StateManager.save_message(direction="outbound")`` is called from
``whatsapp_webhook.py:5883`` BEFORE the post-LLM safety nets touch
the reply. The dashboard reads the persisted row verbatim, so the
merchant sees the brain's raw pre-safety-net text while the
customer receives the post-safety-net version.

Fix
───
After every safety net and scrubber has run (and just before the
WhatsApp send branches), call ``sync_outbound_body_to_final`` to
update the persisted body so the dashboard matches what the
customer will actually receive. The function uses the same
``(tenant, recipient, queued)`` lookup as
``stamp_outbound_send_status`` so it always touches the right row.
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ── Stubs ───────────────────────────────────────────────────────────────────


class _FakeRow:
    """Minimal stand-in for a ``MessageEvent`` row with the columns
    ``sync_outbound_body_to_final`` reads/writes."""
    def __init__(
        self,
        *,
        row_id: int,
        tenant_id: int,
        body: str,
        direction: str = "outbound",
        extra_metadata: Optional[Dict[str, Any]] = None,
        created_at: Optional[datetime] = None,
    ) -> None:
        self.id = row_id
        self.tenant_id = tenant_id
        self.body = body
        self.direction = direction
        self.extra_metadata = dict(extra_metadata or {})
        self.created_at = created_at or datetime.utcnow()


class _FakeQuery:
    """Tiny stand-in for the SQLAlchemy query chain the helper uses.
    All filter/order_by calls are no-ops; the final ``.first()`` just
    returns whichever row we configured."""
    def __init__(self, row: Optional[_FakeRow]) -> None:
        self._row = row

    def filter(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
        return self

    def order_by(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
        return self

    def first(self) -> Optional[_FakeRow]:
        return self._row


class _FakeDB:
    """Minimal SQLAlchemy-session stand-in. Tracks every commit so we
    can assert the helper actually persisted its work."""
    def __init__(self, row: Optional[_FakeRow] = None) -> None:
        self.row = row
        self.committed = 0
        self.flushed = 0
        self.rolled_back = 0
        self.added: List[Any] = []
        self.nested = 0

    def query(self, _model: Any) -> _FakeQuery:
        return _FakeQuery(self.row)

    def begin_nested(self) -> None:
        self.nested += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def flush(self) -> None:
        self.flushed += 1

    def commit(self) -> None:
        self.committed += 1

    def add(self, obj: Any) -> None:
        self.added.append(obj)


@pytest.fixture
def install_stubs(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub the lookup so tests focus on the mutation contract
    (body rewrite, audit history, idempotence). The real
    ``_find_queued_outbound_row`` is covered by the integration test
    suite — here we want to assert the helper's BEHAVIOUR given a
    row, not its lookup mechanics.
    """
    # ``flag_modified`` is a no-op in tests — we don't have a real
    # SQLAlchemy session, so the dirty-bit signal is irrelevant.
    fake_orm_attrs = types.ModuleType("sqlalchemy.orm.attributes")
    fake_orm_attrs.flag_modified = lambda *_a, **_k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "sqlalchemy.orm.attributes", fake_orm_attrs,
    )

    # Patch the lookup so it returns whichever row the test provides.
    import core.outbound_send_status as oss  # noqa: PLC0415

    def _builder(row: Optional[_FakeRow]) -> _FakeDB:
        def _fake_lookup(_db: Any, *, tenant_id: int, recipient: str) -> Any:
            return row
        monkeypatch.setattr(oss, "_find_queued_outbound_row", _fake_lookup)
        return _FakeDB(row=row)
    return _builder


# ── Tests ───────────────────────────────────────────────────────────────────


def test_body_sync_replaces_persisted_body_with_final_reply(
    install_stubs: Any,
) -> None:
    """The Tenant 33 case: brain wrote "هذا متجرنا 🌷" to the row;
    safety net injected the store URL; sync must rewrite the row's
    body so the dashboard matches WhatsApp."""
    from core.outbound_send_status import sync_outbound_body_to_final

    row = _FakeRow(
        row_id=42,
        tenant_id=33,
        body="هذا متجرنا 🌷 نكمل إنشاء طلب المنتج الآن؟",
        extra_metadata={
            "phone": "+966555555555",
            "provider_send": {"status": "queued"},
        },
    )
    db = install_stubs(row)

    result = sync_outbound_body_to_final(
        db,
        tenant_id=33,
        recipient="+966555555555",
        final_body="تفضل رابط متجرنا 🌷\nhttps://mystore.example.sa",
        reason="post_safety_nets_pre_send",
    )

    assert result == 42
    assert row.body == "تفضل رابط متجرنا 🌷\nhttps://mystore.example.sa"
    assert db.committed == 1, "sync must commit so dashboard sees the change"

    # Audit trail in extra_metadata so support can reconstruct the
    # divergence later.
    history = row.extra_metadata.get("body_sync_history") or []
    assert len(history) == 1
    entry = history[0]
    assert entry["reason"] == "post_safety_nets_pre_send"
    assert entry["len_before"] == len(
        "هذا متجرنا 🌷 نكمل إنشاء طلب المنتج الآن؟"
    )
    assert entry["len_after"] == len(
        "تفضل رابط متجرنا 🌷\nhttps://mystore.example.sa"
    )
    assert "هذا متجرنا" in entry["preview_from"]
    assert "تفضل رابط متجرنا" in entry["preview_to"]


def test_body_sync_noop_when_body_unchanged(install_stubs: Any) -> None:
    """The safety nets often DON'T modify the reply (when the LLM
    already shipped a URL, when the intent isn't link-related, etc).
    The sync must not bump the DB for a no-op — we'd be paying a
    write for no observable benefit."""
    from core.outbound_send_status import sync_outbound_body_to_final

    body = "أهلاً بك 🌷 كيف نقدر نخدمك؟"
    row = _FakeRow(
        row_id=7,
        tenant_id=33,
        body=body,
        extra_metadata={
            "phone": "+966500000000",
            "provider_send": {"status": "queued"},
        },
    )
    db = install_stubs(row)

    result = sync_outbound_body_to_final(
        db,
        tenant_id=33,
        recipient="+966500000000",
        final_body=body,
    )

    assert result == 7
    assert row.body == body
    # Identical-body branch returns early without committing.
    assert db.committed == 0
    assert "body_sync_history" not in row.extra_metadata


def test_body_sync_returns_none_when_no_candidate_row(
    install_stubs: Any,
) -> None:
    """When the wire layer is called BEFORE the persistence (a path
    we don't currently take but defend against) the sync must
    silently return without raising."""
    from core.outbound_send_status import sync_outbound_body_to_final

    db = install_stubs(None)
    result = sync_outbound_body_to_final(
        db,
        tenant_id=33,
        recipient="+966555555555",
        final_body="anything",
    )
    assert result is None
    assert db.committed == 0


def test_body_sync_silent_on_empty_recipient() -> None:
    """Helper must defend against junk inputs without raising — the
    send path can call it from many code paths and we cannot let an
    edge-case ``recipient=""`` crash the send."""
    from core.outbound_send_status import sync_outbound_body_to_final

    # No stubs needed — the helper exits early on the input check
    # before touching DB-aware code.
    result = sync_outbound_body_to_final(
        None, tenant_id=33, recipient="", final_body="ignored",
    )
    assert result is None


def test_body_sync_silent_on_none_tenant() -> None:
    from core.outbound_send_status import sync_outbound_body_to_final

    result = sync_outbound_body_to_final(
        None, tenant_id=None, recipient="+966555555555",
        final_body="ignored",
    )
    assert result is None


def test_body_sync_history_caps_at_three_entries(install_stubs: Any) -> None:
    """The audit trail is meant to debug recent divergences — we cap
    history at the last 3 entries so the JSONB column doesn't bloat
    for chatty conversations."""
    from core.outbound_send_status import sync_outbound_body_to_final

    row = _FakeRow(
        row_id=1,
        tenant_id=33,
        body="initial",
        extra_metadata={
            "phone": "+966500000000",
            "provider_send": {"status": "queued"},
            "body_sync_history": [
                {"reason": "old_1", "at": "t", "len_before": 1, "len_after": 1,
                 "preview_from": "a", "preview_to": "b"},
                {"reason": "old_2", "at": "t", "len_before": 1, "len_after": 1,
                 "preview_from": "a", "preview_to": "b"},
                {"reason": "old_3", "at": "t", "len_before": 1, "len_after": 1,
                 "preview_from": "a", "preview_to": "b"},
            ],
        },
    )
    db = install_stubs(row)

    sync_outbound_body_to_final(
        db, tenant_id=33, recipient="+966500000000",
        final_body="updated",
        reason="post_safety_nets_pre_send",
    )

    history = row.extra_metadata["body_sync_history"]
    assert len(history) == 3, "history must be capped"
    # The newest entry is the one we just added.
    assert history[-1]["reason"] == "post_safety_nets_pre_send"
    # Oldest of the three pre-existing entries was rotated out.
    assert all(h["reason"] != "old_1" for h in history)


def test_body_sync_preserves_other_metadata_fields(
    install_stubs: Any,
) -> None:
    """The sync must NOT clobber unrelated metadata — provider_send,
    is_ai, deterministic_path, handoff flags, etc. all live in the
    same JSONB blob."""
    from core.outbound_send_status import sync_outbound_body_to_final

    row = _FakeRow(
        row_id=99,
        tenant_id=33,
        body="brain raw",
        extra_metadata={
            "phone": "+966500000000",
            "provider_send": {"status": "queued", "operation": "whatsapp"},
            "is_ai": True,
            "handoff_active": False,
            "deterministic_path": "merchant_reply",
        },
    )
    db = install_stubs(row)

    sync_outbound_body_to_final(
        db, tenant_id=33, recipient="+966500000000",
        final_body="post-safety-net",
    )

    meta = row.extra_metadata
    assert meta["provider_send"]["status"] == "queued"
    assert meta["provider_send"]["operation"] == "whatsapp"
    assert meta["is_ai"] is True
    assert meta["handoff_active"] is False
    assert meta["deterministic_path"] == "merchant_reply"
    assert "body_sync_history" in meta  # audit trail added
