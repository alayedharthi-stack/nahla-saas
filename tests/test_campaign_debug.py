"""tests/test_campaign_debug.py
─────────────────────────────────
Coverage for the campaign-diagnostic surface:

  * GET  /campaigns/{id}/debug            — full state snapshot
  * POST /campaigns/{id}/dispatch-now     — synchronous dispatcher kick
  * _classify_campaign_lifecycle helper   — granular merchant verb

We focus on the contract the UI relies on (lifecycle keys, hint text,
``ok``/``skipped`` shape on dispatch-now) rather than re-testing the
underlying dispatch_campaign which is exercised by
test_campaign_send_log.py + test_campaign_dispatcher.py.

The tests deliberately avoid TestClient because the rest of this repo
historically hits ``Session`` thread issues with SQLite-in-memory under
TestClient. We call the router handlers directly with ``asyncio.run``
(same pattern used in tests/test_manual_segments.py).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    Base, Campaign, CampaignSendLog, Customer, Tenant, WhatsAppTemplate,
)
from routers import campaigns as campaigns_router  # noqa: E402


def _make_db():
    """Fresh SQLite-backed engine that mirrors prod model definitions
    (JSONB is downgraded to JSON for SQLite)."""
    engine = create_engine("sqlite:///:memory:")
    _saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed(db, *, status="active", audience_count=0, sent_count=0):
    t = Tenant(name="T", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    tpl = WhatsAppTemplate(
        tenant_id=t.id, name="tpl_promo", language="ar",
        category="MARKETING", status="APPROVED",
        components=[{"type": "BODY", "text": "Hi {{1}}"}],
    )
    db.add(tpl); db.commit(); db.refresh(tpl)
    c = Campaign(
        tenant_id=t.id, name="C", campaign_type="broadcast",
        template_id=str(tpl.id), template_name=tpl.name,
        template_language="ar", template_category="MARKETING",
        audience_type="all", status=status,
        audience_count=audience_count, sent_count=sent_count,
        created_at=datetime.now(timezone.utc),
        launched_at=datetime.now(timezone.utc) if status == "active" else None,
    )
    db.add(c); db.commit(); db.refresh(c)
    return t, tpl, c


class _FakeReq:
    headers: dict = {}
    cookies: dict = {}
    state = type("S", (), {})()


def _call_debug(db, tenant_id, campaign_id):
    """Call the debug handler in-process with a stubbed tenant
    resolver so we don't depend on JWT/session middleware."""
    original = campaigns_router.resolve_tenant_id
    campaigns_router.resolve_tenant_id = (
        lambda request, db=None: tenant_id  # type: ignore
    )
    try:
        return asyncio.run(
            campaigns_router.debug_campaign(
                campaign_id=campaign_id, request=_FakeReq(), db=db,
            )
        )
    finally:
        campaigns_router.resolve_tenant_id = original


def _call_dispatch_now(
    db, tenant_id, campaign_id, *, bypass_frequency_cap: bool = False,
):
    original = campaigns_router.resolve_tenant_id
    campaigns_router.resolve_tenant_id = (
        lambda request, db=None: tenant_id  # type: ignore
    )
    try:
        return asyncio.run(
            campaigns_router.dispatch_campaign_now(
                campaign_id=campaign_id,
                request=_FakeReq(),
                db=db,
                bypass_frequency_cap=bypass_frequency_cap,
            )
        )
    finally:
        campaigns_router.resolve_tenant_id = original


# ── 1. _classify_campaign_lifecycle ─────────────────────────────────


class TestLifecycleClassifier:
    """The merchant-friendly verb that replaces the raw status pill.

    The classifier is pure (no DB), so we exercise every status × count
    combination the UI is allowed to encounter, and pin the keys the
    frontend LIFECYCLE_META map relies on.
    """

    def _verb(self, status, **counts):
        camp = Campaign(status=status, sent_count=counts.pop("sent", 0))
        return campaigns_router._classify_campaign_lifecycle(camp, counts)

    def test_active_with_zero_counts_is_pending_dispatch(self):
        # The exact case we're trying to surface: campaign is
        # ``active`` (immediate launch) but the asyncio task hasn't
        # snapshotted any recipient yet (or died silently).
        assert self._verb("active") == "pending_dispatch"

    def test_active_with_queued_rows_is_sending(self):
        assert self._verb("active", queued=4) == "sending"

    def test_completed_with_only_sends_is_sent(self):
        camp = Campaign(status="completed", sent_count=4)
        assert campaigns_router._classify_campaign_lifecycle(
            camp, {"sent": 4},
        ) == "sent"

    def test_completed_with_only_failures_is_failed_all(self):
        camp = Campaign(status="completed", sent_count=0)
        assert campaigns_router._classify_campaign_lifecycle(
            camp, {"failed": 4},
        ) == "failed_all"

    def test_completed_with_mixed_is_partial(self):
        camp = Campaign(status="completed", sent_count=2)
        assert campaigns_router._classify_campaign_lifecycle(
            camp, {"sent": 2, "failed": 1},
        ) == "partial"

    def test_completed_with_no_audience_is_completed_empty(self):
        # Genuinely empty audience (segment matched 0 customers).
        camp = Campaign(status="completed", sent_count=0, audience_count=0)
        assert campaigns_router._classify_campaign_lifecycle(
            camp, {},
        ) == "completed_empty"

    def test_completed_with_audience_but_zero_logs_is_excluded_before_send(self):
        # The exact bug the merchant hit: audience_count=4 but 0 rows
        # in campaign_send_logs (every customer was filtered upstream).
        # Must NOT collapse to ``completed_empty`` — the merchant needs
        # the explicit "all your customers were excluded" verdict so
        # they fix data, not retry.
        camp = Campaign(status="completed", sent_count=0, audience_count=4)
        assert campaigns_router._classify_campaign_lifecycle(
            camp, {},
        ) == "excluded_before_send"

    def test_scheduled_is_waiting_scheduler(self):
        assert self._verb("scheduled") == "waiting_scheduler"

    def test_failed_status_passes_through(self):
        assert self._verb("failed") == "failed"

    def test_draft_passes_through(self):
        assert self._verb("draft") == "draft"


# ── 2. /campaigns/{id}/debug ────────────────────────────────────────


class TestDebugEndpoint:
    def test_returns_full_snapshot_for_active_campaign(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        result = _call_debug(db, t.id, c.id)

        # Top-level shape — the UI relies on these keys.
        assert set(result.keys()) >= {
            "campaign", "recipients", "sample_failed", "sample_sent",
            "template", "wa_connection", "scheduler", "hints", "errors",
            # New funnel + exclusion fields (see TestAudienceFunnel below).
            "audience_funnel", "excluded_reasons_summary",
            "excluded_before_send_count",
            "frequency_cap", "failure_summary",
            "sample_excluded_before_send",
        }
        fc = result["frequency_cap"]
        assert fc["capped_count"] == 0
        assert "frequency_cap_source_rows" in fc
        assert "cap_days" in fc
        assert result["campaign"]["id"] == c.id
        assert result["campaign"]["lifecycle"] == "pending_dispatch"
        assert result["campaign"]["audience_count"] == 4

        # No recipients yet → all counters zero, no failed/sent samples.
        assert result["recipients"]["total"] == 0
        assert result["sample_failed"] == []
        assert result["sample_sent"] == []

        # Template is approved.
        assert result["template"]["approved"] is True

        # Scheduler section always present.
        assert result["scheduler"]["campaign_dispatcher_enabled"] in (True, False)
        # Hints include the "asyncio task died" diagnostic because
        # audience_count > 0 but no recipients were ever snapshotted.
        assert any("asyncio" in h or "Railway" in h for h in result["hints"])

    def test_surfaces_kill_switch_hint_for_non_immediate(self):
        # Set the kill switch and ensure the hint fires for a
        # scheduled campaign (the kill switch only affects scheduled
        # / delayed campaigns — immediate ones go through
        # asyncio.create_task and bypass the scheduler).
        os.environ["NAHLA_DISABLE_SCHEDULERS"] = "1"
        try:
            db, _ = _make_db()
            t, tpl, c = _seed(db, status="scheduled", audience_count=0)
            c.schedule_type = "scheduled"
            db.commit()
            result = _call_debug(db, t.id, c.id)
        finally:
            os.environ.pop("NAHLA_DISABLE_SCHEDULERS", None)
        assert result["scheduler"]["kill_switch_set"] is True
        assert any("NAHLA_DISABLE_SCHEDULERS" in h for h in result["hints"])

    def test_unapproved_template_surfaces_hint(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        tpl.status = "PENDING"
        db.commit()
        result = _call_debug(db, t.id, c.id)
        assert result["template"]["approved"] is False
        assert any("APPROVED" in h for h in result["hints"])

    def test_missing_template_does_not_crash(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        # Reassign a non-existent template id and verify the
        # endpoint still returns a structured response.
        c.template_id = "999999"
        db.commit()
        result = _call_debug(db, t.id, c.id)
        # Template lookup returns None — but the response must still
        # be a fully-formed dict.
        assert result["template"] is None
        # And the helpful hint surfaces.
        assert any("APPROVED" in h for h in result["hints"])

    def test_recipient_counters_aggregated_correctly(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        for status, phone in [
            ("sent", "+966500000001"),
            ("sent", "+966500000002"),
            ("failed", "+966500000003"),
            ("queued", "+966500000004"),
        ]:
            db.add(CampaignSendLog(
                tenant_id=t.id, campaign_id=c.id,
                customer_phone_e164=phone, status=status,
                error_message="boom" if status == "failed" else None,
                sent_at=datetime.now(timezone.utc) if status == "sent" else None,
            ))
        db.commit()

        result = _call_debug(db, t.id, c.id)
        r = result["recipients"]
        assert r["total"] == 4
        assert r["sent"] == 2
        assert r["failed"] == 1
        assert r["queued"] == 1
        # Sample arrays surface the most-recent rows.
        assert len(result["sample_sent"]) == 2
        assert len(result["sample_failed"]) == 1
        # Each failed sample now carries a classified Meta error
        # (canonical key + Arabic label + severity).
        sf = result["sample_failed"][0]
        assert sf["error_technical"] == "boom"
        assert sf["error_code"]      # canonical key (e.g. "unknown")
        assert sf["error_label_ar"]  # Arabic label
        assert sf["severity"] in ("minor", "major", "blocking")
        # Phone is masked.
        assert result["sample_sent"][0]["phone"].endswith("0001") or \
               result["sample_sent"][0]["phone"].endswith("0002")
        assert "•" in result["sample_sent"][0]["phone"]


# ── 3. /campaigns/{id}/dispatch-now ─────────────────────────────────


class TestDispatchNow:
    """The dispatch-now endpoint is fire-and-forget: it must return
    immediately (the dispatcher has 1.5s+ pauses between sends and
    would blow past our 25s frontend HTTP timeout for any real
    audience). These tests pin the contract."""

    def test_skipped_for_completed_campaign(self):
        # Completed campaign with sent_count>0 must not be re-kicked
        # — we return a clear "skipped:completed" so the merchant
        # doesn't think the manual button silently no-op'd.
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4, sent_count=4)
        result = _call_dispatch_now(db, t.id, c.id)
        assert result["skipped"] is True
        assert result["reason"] == "completed"
        assert result["ok"] is True

    def test_returns_immediately_with_kicked_flag(self, monkeypatch):
        # The endpoint must spawn the dispatcher in the background
        # via asyncio.create_task and return ``kicked: true`` BEFORE
        # the dispatcher has done any work. We assert this by
        # patching create_task to record the call.
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=2)

        spawned: list = []
        import asyncio as _aio
        original_create_task = _aio.create_task

        def _capture(coro, *args, **kwargs):
            spawned.append(coro)
            # Schedule the coroutine on the running loop so it does
            # not raise "coroutine was never awaited" warnings; the
            # test event loop closes before the dispatcher runs.
            return original_create_task(coro, *args, **kwargs)

        monkeypatch.setattr(_aio, "create_task", _capture)

        # Stub the actual dispatch so the background coroutine is
        # cheap (we're testing the endpoint, not the dispatcher).
        async def _fake(_db, cid):
            return {"campaign_id": cid, "status": "completed", "sent": 2,
                    "failed": 0, "queued": 0, "errors": []}
        import services.campaign_dispatcher as cd
        monkeypatch.setattr(cd, "dispatch_campaign", _fake)

        result = _call_dispatch_now(db, t.id, c.id)
        assert result["ok"] is True
        assert result["kicked"] is True
        # Status pre-flipped to 'active' so the next list refresh
        # immediately shows "جاري الإرسال".
        assert result["status"] == "active"
        # A background coroutine was actually scheduled.
        assert len(spawned) == 1

    def test_pre_flips_status_to_active(self, monkeypatch):
        # A campaign that was stuck in 'draft' should flip to
        # 'active' synchronously so the next /campaigns refresh
        # surfaces the new lifecycle pill ("جاري الإرسال") within
        # the polling window — without waiting for the dispatcher
        # to do it ≈3s later.
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="draft", audience_count=2)
        c.launched_at = None
        db.commit()

        async def _noop(_db, cid):
            return {"campaign_id": cid, "status": "completed", "sent": 0,
                    "failed": 0, "queued": 0, "errors": []}
        import services.campaign_dispatcher as cd
        monkeypatch.setattr(cd, "dispatch_campaign", _noop)

        result = _call_dispatch_now(db, t.id, c.id)
        assert result["ok"] is True
        db.refresh(c)
        assert c.status == "active"
        assert c.launched_at is not None

    def test_dispatch_now_sets_bypass_flag_without_running_background_task(
            self, monkeypatch,
    ):
        """``bypass_frequency_cap=true`` persists ``_bypass_frequency_cap``
        on the campaign row before the async task starts; the dispatcher
        consumes it as a one-shot flag."""
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="draft", audience_count=2)
        c.launched_at = None
        db.commit()

        spawned: list = []

        def _capture_only(coro):  # noqa: ANN001
            try:
                coro.close()
            except (RuntimeError, GeneratorExit):
                pass
            spawned.append(coro)

            class _Dummy:
                def cancel(self):
                    pass

            return _Dummy()

        monkeypatch.setattr(asyncio, "create_task", _capture_only)

        async def _noop(_db, cid):
            return {"campaign_id": cid, "status": "completed", "sent": 0,
                    "failed": 0, "queued": 0, "errors": []}
        import services.campaign_dispatcher as cd  # noqa: PLC0415
        monkeypatch.setattr(cd, "dispatch_campaign", _noop)

        result = _call_dispatch_now(db, t.id, c.id, bypass_frequency_cap=True)
        assert result["ok"] is True
        assert result["bypass_frequency_cap"] is True
        assert len(spawned) == 1

        db.refresh(c)
        assert (c.template_variables or {}).get("_bypass_frequency_cap") == "true"
        assert c.status == "active"


# ── 4. _campaign_to_dict carries lifecycle for the listing endpoint ──


class TestCampaignToDictLifecycle:
    def test_active_with_zero_recipients_renders_pending_dispatch(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        out = campaigns_router._campaign_to_dict(c)
        assert out["lifecycle"] == "pending_dispatch"
        assert out["status"] == "active"
        assert out["last_error"] is None
        assert out["last_error_ar"] is None

    def test_failed_carries_last_error_first_dispatch_error(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="failed", audience_count=4)
        c.template_variables = {"_dispatch_errors": "lookup timeout|secondary issue"}
        db.commit()
        out = campaigns_router._campaign_to_dict(c)
        assert out["lifecycle"] == "failed"
        assert out["last_error"] == "lookup timeout"
        # Best-effort Arabic translation — the message doesn't match
        # any known pattern so it falls onto the "unknown" bucket
        # which still has an Arabic label.
        assert out["last_error_ar"]
        assert out["dispatch_errors"] == ["lookup timeout", "secondary issue"]

    def test_last_error_with_canonical_suffix_is_translated(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="failed", audience_count=4)
        # Dispatcher writes errors as "<phone>: <Arabic label> [<key>]".
        c.template_variables = {
            "_dispatch_errors":
                "+966500000001: الرقم لا يملك حساب واتساب [not_on_whatsapp]",
        }
        db.commit()
        out = campaigns_router._campaign_to_dict(c)
        assert out["last_error_key"] == "not_on_whatsapp"
        assert "واتساب" in out["last_error_ar"]


# ── 5. Meta error classifier ────────────────────────────────────────


class TestMetaErrorClassifier:
    """Sanity checks for the canonical Meta-error mapping. We don't
    test every numeric code (that would just duplicate the table),
    only the categories the merchant actually sees."""

    def _classify(self, **kw):
        from services.meta_errors import classify_meta_error
        return classify_meta_error(**kw)

    def test_recipient_not_on_whatsapp_is_minor(self):
        c = self._classify(code=131026, message="Message undeliverable")
        assert c.key == "not_on_whatsapp"
        assert c.severity == "minor"
        assert c.is_recoverable is False
        assert "واتساب" in c.label_ar

    def test_rate_limit_is_blocking(self):
        c = self._classify(code=130429)
        assert c.key == "rate_limit"
        assert c.severity == "blocking"
        assert c.is_recoverable is True

    def test_template_paused_is_blocking(self):
        c = self._classify(code=132015)
        assert c.key == "template_paused"
        assert c.severity == "blocking"

    def test_text_fallback_for_24h_window(self):
        # Out-of-window often surfaces with a free-text "service
        # window" message; the regex layer should catch it even
        # without a numeric code.
        c = self._classify(message="Re-engagement message outside 24-hour window")
        assert c.key == "out_of_24h_window"

    def test_unknown_falls_back_to_unknown_bucket(self):
        c = self._classify(code=999999, message="something never seen")
        assert c.key == "unknown"
        # ``unknown`` still has an Arabic label so the merchant never
        # sees raw English jargon.
        assert c.label_ar
        assert c.advice_ar


class TestMetaTechnicalSerialisation:
    """The ``[code=X subcode=Y type=Z] msg`` shape is the contract
    between the dispatcher (writer) and the debug endpoint (reader).
    Round-tripping it MUST preserve every Meta field so the UI can
    show ``unknown`` errors with the raw payload underneath."""

    def test_format_then_parse_round_trips(self):
        from services.meta_errors import format_technical, parse_technical
        s = format_technical(
            code=132000, subcode=2494073, error_type="OAuthException",
            message="Template parameter mismatch",
        )
        assert s == (
            "[code=132000 subcode=2494073 type=OAuthException] "
            "Template parameter mismatch"
        )
        parsed = parse_technical(s)
        assert parsed["meta_error_code"] == "132000"
        assert parsed["meta_error_subcode"] == "2494073"
        assert parsed["meta_error_type"] == "OAuthException"
        assert parsed["meta_error_message"] == "Template parameter mismatch"

    def test_parse_handles_missing_fields(self):
        from services.meta_errors import parse_technical
        parsed = parse_technical("[code=131026] Message undeliverable")
        assert parsed["meta_error_code"] == "131026"
        assert parsed["meta_error_subcode"] is None
        assert parsed["meta_error_type"] is None
        assert parsed["meta_error_message"] == "Message undeliverable"

    def test_parse_handles_legacy_string_without_brackets(self):
        from services.meta_errors import parse_technical
        parsed = parse_technical("Meta accepted but no wamid returned")
        assert parsed["meta_error_code"] is None
        assert parsed["meta_error_message"] == (
            "Meta accepted but no wamid returned"
        )

    def test_parse_skips_none_placeholders(self):
        from services.meta_errors import parse_technical
        parsed = parse_technical("[code=None subcode=None type=None] boom")
        assert parsed["meta_error_code"] is None
        assert parsed["meta_error_subcode"] is None
        assert parsed["meta_error_type"] is None
        assert parsed["meta_error_message"] == "boom"


class TestRawMetaSamplesPersistence:
    """Capture-bucket invariants. Support relies on these to grow the
    classifier from production fingerprints."""

    def _campaign(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=4)
        return db, t, c

    def test_record_appends_sample_into_template_variables(self):
        from services.campaign_dispatcher import _record_raw_meta_sample
        db, t, c = self._campaign()
        _record_raw_meta_sample(
            campaign=c,
            recipient_phone="+966500000001",
            meta_code=132000, meta_subcode=2494073,
            meta_type="OAuthException",
            meta_message="Template parameter mismatch",
            request_payload={
                "to": "+966500000001",
                "template": {
                    "name": "nahla_special_offer_c874",
                    "language": {"code": "ar"},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": "n"}]}],
                },
            },
            response_payload={"error": {"code": 132000, "message": "boom"}},
            classified_key="unknown",
        )
        db.commit()
        db.refresh(c)
        import json as _json
        raw = (c.template_variables or {}).get("_raw_meta_error_samples")
        samples = _json.loads(raw)
        assert len(samples) == 1
        s = samples[0]
        # Phone is masked — never leak raw PII into stored JSON.
        assert "966500000001" not in s["recipient"]
        assert s["recipient"].endswith("0001")
        # Template name + language survive verbatim so support can
        # check ``template.name`` mismatch / language-code mismatch.
        assert s["request_payload"]["template"]["name"] == "nahla_special_offer_c874"
        assert s["request_payload"]["template"]["language"]["code"] == "ar"
        assert s["classified_key"] == "unknown"

    def test_sample_bucket_is_bounded_and_prefers_unknown(self):
        from services.campaign_dispatcher import (
            _record_raw_meta_sample, _MAX_RAW_META_SAMPLES,
        )
        db, t, c = self._campaign()
        for i in range(_MAX_RAW_META_SAMPLES + 4):
            classified = "unknown" if i % 2 == 0 else "rate_limit"
            _record_raw_meta_sample(
                campaign=c,
                recipient_phone=f"+96650000{i:04d}",
                meta_code=131026 + i,
                meta_subcode=None, meta_type=None,
                meta_message=f"err {i}",
                request_payload={"to": f"+96650000{i:04d}"},
                response_payload={"error": {"code": 131026 + i}},
                classified_key=classified,
            )
        db.commit()
        db.refresh(c)
        import json as _json
        samples = _json.loads(
            (c.template_variables or {})["_raw_meta_error_samples"]
        )
        assert len(samples) <= _MAX_RAW_META_SAMPLES
        # At least one ``unknown`` survived even though more known
        # samples followed — the bucket prefers unknowns.
        keys = [s["classified_key"] for s in samples]
        assert "unknown" in keys

    def test_debug_endpoint_exposes_raw_samples(self):
        from services.campaign_dispatcher import _record_raw_meta_sample
        db, t, c = self._campaign()
        _record_raw_meta_sample(
            campaign=c,
            recipient_phone="+966500000099",
            meta_code=999999, meta_subcode=None, meta_type=None,
            meta_message="fingerprint me",
            request_payload={"to": "+966500000099", "template": {"name": "T", "language": {"code": "ar"}}},
            response_payload={"error": {"code": 999999, "message": "fingerprint me"}},
            classified_key="unknown",
        )
        db.commit()

        result = _call_debug(db, t.id, c.id)
        samples = result["raw_meta_error_samples"]
        assert len(samples) == 1
        assert samples[0]["meta_error_code"] == "999999"
        assert samples[0]["meta_error_message"] == "fingerprint me"
        # Request payload made it through verbatim.
        assert samples[0]["request_payload"]["template"]["name"] == "T"
        # Recipient is masked.
        assert "966500000099" not in samples[0]["recipient"]

    def test_sample_failed_includes_parsed_meta_fields(self):
        from services.meta_errors import format_technical
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=2)
        technical = format_technical(
            code=132000, subcode=2494073,
            error_type="OAuthException",
            message="Template parameter mismatch",
        )
        db.add(CampaignSendLog(
            tenant_id=t.id, campaign_id=c.id,
            customer_phone_e164="+966500000111", status="failed",
            error_code="unknown", error_message=technical,
        ))
        db.commit()

        result = _call_debug(db, t.id, c.id)
        sf = result["sample_failed"]
        assert len(sf) == 1
        # Parsed Meta fields surface separately so the UI can render
        # them when the canonical key is ``unknown``.
        assert sf[0]["meta_error_code"] == "132000"
        assert sf[0]["meta_error_subcode"] == "2494073"
        assert sf[0]["meta_error_type"] == "OAuthException"
        assert sf[0]["meta_error_message"] == "Template parameter mismatch"


# ── 6. Lifecycle: partial_minor + no_whatsapp_recipients ────────────


class TestLifecycleWithMetaErrors:
    """The merchant should NOT see "فشل الإرسال للجميع" if every
    failure is a recipient-side issue (e.g. all customers happen to
    not have WhatsApp). The classifier degrades to
    ``no_whatsapp_recipients`` (sent=0) or ``partial_minor`` (sent>0)
    when ``db`` is provided and every failed row has a minor key.
    """

    def _seed_failures(self, db, tenant_id, campaign_id, error_codes):
        for i, key in enumerate(error_codes):
            db.add(CampaignSendLog(
                tenant_id=tenant_id, campaign_id=campaign_id,
                customer_phone_e164=f"+96650000000{i+1}", status="failed",
                error_code=key, error_message="meta said no",
            ))
        db.commit()

    def test_all_minor_failures_yield_no_whatsapp_recipients(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4, sent_count=0)
        self._seed_failures(db, t.id, c.id, [
            "not_on_whatsapp", "not_on_whatsapp", "not_on_whatsapp", "user_not_opted_in",
        ])
        verb = campaigns_router._classify_campaign_lifecycle(
            c, {"failed": 4}, db=db,
        )
        assert verb == "no_whatsapp_recipients"

    def test_minor_mix_with_some_sent_yields_partial_minor(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4, sent_count=2)
        self._seed_failures(db, t.id, c.id, ["not_on_whatsapp", "not_on_whatsapp"])
        verb = campaigns_router._classify_campaign_lifecycle(
            c, {"sent": 2, "failed": 2}, db=db,
        )
        assert verb == "partial_minor"

    def test_blocking_failure_keeps_failed_all(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4, sent_count=0)
        # A single template_paused poisons the whole campaign — that
        # IS a real failure the merchant must see.
        self._seed_failures(db, t.id, c.id, [
            "not_on_whatsapp", "template_paused",
        ])
        verb = campaigns_router._classify_campaign_lifecycle(
            c, {"failed": 2}, db=db,
        )
        assert verb == "failed_all"


# ── 7. Debug endpoint exposes failure_summary ───────────────────────


class TestFailureSummary:
    def test_failure_summary_groups_by_canonical_key(self):
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="active", audience_count=5)
        for i in range(3):
            db.add(CampaignSendLog(
                tenant_id=t.id, campaign_id=c.id,
                customer_phone_e164=f"+966500000{i:03d}", status="failed",
                error_code="not_on_whatsapp", error_message="Message undeliverable",
            ))
        db.add(CampaignSendLog(
            tenant_id=t.id, campaign_id=c.id,
            customer_phone_e164="+966500000099", status="failed",
            error_code="rate_limit", error_message="Throttled",
        ))
        db.commit()

        result = _call_debug(db, t.id, c.id)
        fs = result["failure_summary"]
        # Two distinct keys, sorted by count descending.
        assert len(fs) == 2
        assert fs[0]["error_code"] == "not_on_whatsapp"
        assert fs[0]["count"] == 3
        assert fs[0]["severity"] == "minor"
        assert fs[1]["error_code"] == "rate_limit"
        assert fs[1]["count"] == 1
        assert fs[1]["severity"] == "blocking"


# ── 8. Audience funnel + pre-send exclusion summary ─────────────────


class TestAudienceFunnel:
    """The exact bug pattern this surface fixes:

        UI: total=4, pending=0, sent=0, failed=0, skipped=0
            lifecycle = "completed_empty"

    That message is misleading — the audience matched 4 customers, but
    every one was filtered out before any send-log row was even written
    (no phone, all opt-out, etc.). The merchant has no way to fix it
    without knowing where the drop happened, which is what the funnel
    + ``excluded_reasons_summary`` provide.
    """

    def test_funnel_falls_back_when_dispatcher_did_not_persist(self):
        # Legacy campaign — never had _audience_funnel persisted.
        # The debug endpoint must still return a structured funnel so
        # the UI can render without optional-chaining everywhere.
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4)
        result = _call_debug(db, t.id, c.id)
        f = result["audience_funnel"]
        # Every key is present and numeric (no missing fields).
        for k in (
            "raw_audience", "after_reachable_filter",
            "materialized_rows", "queued_for_send",
            "skipped_at_snapshot", "frequency_cap_skipped",
            "audience_count_campaign",
        ):
            assert k in f and isinstance(f[k], int)
        # Campaign-level audience_count is always passed through.
        assert f["audience_count_campaign"] == 4

    def test_funnel_round_trips_persisted_payload(self):
        # The dispatcher writes _audience_funnel as a JSON string in
        # template_variables (the column is Dict[str, str]). The debug
        # endpoint must decode + surface it untouched so the UI sees
        # exactly what the dispatcher computed.
        import json as _json
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4)
        c.template_variables = {
            "_audience_funnel": _json.dumps({
                "raw_audience": 4,
                "after_reachable_filter": 0,
                "materialized_rows": 0,
                "queued_for_send": 0,
                "skipped_at_snapshot": 0,
                "frequency_cap_skipped": 0,
            }, ensure_ascii=False),
        }
        db.commit()
        result = _call_debug(db, t.id, c.id)
        f = result["audience_funnel"]
        assert f["raw_audience"] == 4
        assert f["after_reachable_filter"] == 0
        assert f["materialized_rows"] == 0

    def test_pre_snapshot_drop_inferred_from_funnel_delta(self):
        # raw_audience=4, after_reachable=0 → 4 customers were
        # silently dropped upstream (no phone, opt-out, etc.). The
        # exclusion summary must include an inferred row in Arabic so
        # the merchant sees "🚫 4 مستبعد قبل الإرسال" rather than the
        # empty "completed_empty".
        import json as _json
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4)
        c.template_variables = {
            "_audience_funnel": _json.dumps({
                "raw_audience": 4,
                "after_reachable_filter": 0,
                "materialized_rows": 0,
                "queued_for_send": 0,
                "skipped_at_snapshot": 0,
                "frequency_cap_skipped": 0,
            }),
        }
        db.commit()
        result = _call_debug(db, t.id, c.id)
        ex = result["excluded_reasons_summary"]
        assert any(r["count"] == 4 and r["status"] == "filtered_pre_snapshot" for r in ex)
        assert result["excluded_before_send_count"] >= 4
        # Lifecycle now distinguishes "audience matched but everyone
        # was excluded" from "audience was zero".
        assert result["campaign"]["lifecycle"] == "excluded_before_send"
        # Hint surfaces the breakdown explicitly.
        joined_hints = " | ".join(result["hints"])
        assert "الجمهور الأولي" in joined_hints

    def test_sample_excluded_before_send_introspects_each_customer(self):
        """Per-customer drill-down: for each excluded recipient we must
        return the actual field flags that drove the decision so support
        can see "all 4 are missing normalized_phone" instantly without
        paging through the customers list.

        Includes the tri-state ``has_whatsapp`` invariant — null
        (unknown) MUST be passed through as null, NOT coerced to false,
        because Meta is the source of truth and we'd have tried to send.
        """
        from sqlalchemy.orm.attributes import flag_modified
        from services.nahla_segments import build_unified_segment_query
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4)

        # Seed 4 customers exhibiting the four most common drop modes.
        # Customer 1: raw phone but no normalized_phone (import bug)
        # Customer 2: explicit unsubscribe
        # Customer 3: has_whatsapp=false confirmed by past Meta failure
        # Customer 4: has_whatsapp=null (UNKNOWN — must NOT block)
        cust_specs = [
            ("Layla",  "0501234567", None,           {}),
            ("Hisham", "+966500000002", "+966500000002",
             {"is_unsubscribed": True}),
            ("Sara",   "+966500000003", "+966500000003",
             {"has_whatsapp": False}),
            ("Khalid", "+966500000004", "+966500000004",
             {"has_whatsapp": None}),
        ]
        for name, phone, normalized, meta in cust_specs:
            cust = Customer(
                tenant_id=t.id, name=name, phone=phone,
                normalized_phone=normalized, extra_metadata=meta,
            )
            db.add(cust)
        db.commit()

        # Force the segment query to return all 4 customers via the
        # ``all`` audience type (no segment filter).
        c.audience_type = "all"
        # Persist a funnel that shows raw=4 / after_reachable=1 so the
        # debug endpoint computes excluded=3 (matches our seed: only
        # Khalid passes _reachable_filter — he has a normalized phone
        # and isn't opted out).
        # We don't actually need to set the funnel here because the
        # endpoint recomputes the sample from the raw query directly.
        flag_modified(c, "audience_type")
        db.commit()

        # Sanity check: the unified query (require_reachable=False)
        # returns all 4 customers. (require_reachable=True returns
        # only the 2 customers without is_unsubscribed AND with a
        # normalized_phone — Sara and Khalid.)
        raw_q = build_unified_segment_query("all", db, t.id, require_reachable=False)
        assert raw_q is not None
        raw_count = raw_q.count()
        assert raw_count == 4

        result = _call_debug(db, t.id, c.id)
        sample = result["sample_excluded_before_send"]
        # Every customer is "excluded before send" because no
        # campaign_send_logs rows exist yet.
        assert len(sample) >= 1
        by_name = {row["name"]: row for row in sample}

        # Layla: no normalized_phone but has raw phone → "phone_not_normalized"
        if "Layla" in by_name:
            r = by_name["Layla"]
            assert r["fields"]["has_phone"] is True
            assert r["fields"]["phone_normalized_valid"] is False
            assert r["reason_key"] == "phone_not_normalized"

        # Hisham: explicitly unsubscribed → "unsubscribed"
        if "Hisham" in by_name:
            r = by_name["Hisham"]
            assert r["fields"]["is_unsubscribed"] is True
            assert r["fields"]["whatsapp_opted_out"] is True
            assert r["reason_key"] == "unsubscribed"

        # Sara: has_whatsapp explicitly False (Meta confirmed)
        # → "no_whatsapp_confirmed"
        if "Sara" in by_name:
            r = by_name["Sara"]
            assert r["fields"]["has_whatsapp"] is False
            assert r["reason_key"] == "no_whatsapp_confirmed"

        # Khalid: has_whatsapp=null → MUST pass through as null AND
        # MUST NOT have reason_key="no_whatsapp_confirmed". This is
        # the critical invariant: we never block on unknown.
        if "Khalid" in by_name:
            r = by_name["Khalid"]
            assert r["fields"]["has_whatsapp"] is None, (
                "has_whatsapp=null MUST pass through as null — coercing it "
                "to false would silently exclude every customer Meta "
                "hasn't told us about yet"
            )
            assert r["reason_key"] != "no_whatsapp_confirmed"

        # Phones are masked so the debug endpoint never leaks PII
        # (consistent with sample_failed / sample_sent behaviour).
        for r in sample:
            if r["phone_masked"]:
                assert "•" in r["phone_masked"] or len(r["phone_masked"]) <= 4

    def test_sample_excluded_returns_empty_for_zero_audience(self):
        """``completed_empty`` (genuinely zero audience) must return an
        empty sample — there's no one to introspect, so the UI should
        hide the drill-down section entirely instead of rendering an
        empty card grid."""
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=0)
        c.audience_type = "all"
        db.commit()
        result = _call_debug(db, t.id, c.id)
        assert result["sample_excluded_before_send"] == []

    def test_skipped_log_rows_appear_in_exclusion_summary(self):
        # When the snapshot DOES write rows but they're all
        # ``skipped_*``, the exclusion summary must group them by
        # skip_reason (not by raw status) so "بدون رقم جوال" and
        # "ألغى الاشتراك" are visible separately.
        db, _ = _make_db()
        t, tpl, c = _seed(db, status="completed", audience_count=4)
        for i, (status_v, reason) in enumerate([
            ("skipped_unreachable", "no_phone"),
            ("skipped_unreachable", "no_phone"),
            ("skipped_unsubscribed", "unsubscribed"),
            ("skipped_invalid", "invalid_phone"),
        ]):
            db.add(CampaignSendLog(
                tenant_id=t.id, campaign_id=c.id,
                customer_phone_e164=f"__skipped__:{i}",
                status=status_v, skip_reason=reason,
            ))
        db.commit()

        result = _call_debug(db, t.id, c.id)
        ex = result["excluded_reasons_summary"]
        # Three distinct reasons, with the no-phone bucket aggregated.
        by_reason = {(r["skip_reason"]): r["count"] for r in ex}
        assert by_reason.get("no_phone") == 2
        assert by_reason.get("unsubscribed") == 1
        assert by_reason.get("invalid_phone") == 1
        # Every entry is Arabic-labelled so the UI doesn't need its
        # own translation table.
        assert all(r["label_ar"] for r in ex)
        # All four are in the totaliser.
        assert result["excluded_before_send_count"] == 4

