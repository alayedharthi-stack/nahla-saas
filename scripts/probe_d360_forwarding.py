#!/usr/bin/env python3
"""scripts/probe_d360_forwarding.py
───────────────────────────────────
Wave 2.2-INV (May 2026) — Read-only probe for "verdict A" cases.

When ``scripts/triage_inbound_drop.py`` returns ``A`` for multiple
senders, the message never reached the Nahla instance at all
(no ``[D360_RAW_INBOUND]``, no ``[INBOUND_LIFECYCLE]``). The likely
cause lives BETWEEN Meta and our webhook handler:

  A1 channel webhook URL not configured at 360dialog
  A2 channel webhook points at a stale BACKEND_URL / wrong host
  A3 phone_number_id drift (360dialog rotated the channel)
  A4 WABA-level webhook missing or wrong
  A5 override_all=False left siblings on a stale per-channel URL
  A8 edge (Railway/proxy) drops requests before they reach ASGI
  A9 public BACKEND_URL changed without re-configure
  A10 Meta quality_rating RED throttles delivery

This probe runs Probes 1–4 from the W2.2-INV plan:

  Probe 1  Find stale ``whatsapp_connections`` rows  (DB)
  Probe 2  For each candidate, snapshot 360dialog channel + WABA
           webhook config                              (D360 Channel API)
  Probe 3  Compute drift / URL mismatch / scope flags  (pure compute)
  Probe 4  Optional: query Partner Hub for channel metadata
                                                       (D360 Partner Hub)

Run
---

::

    export DATABASE_URL='postgres://...'                # required
    export D360_API_BASE_URL='https://waba-v2.360dialog.io'
    export D360_PARTNER_HUB_BASE='https://hub.360dialog.com'
    export D360_PARTNER_API_KEY='...'                  # optional (Probe 4)
    export D360_PARTNER_ID='...'                       # optional (Probe 4)
    export BACKEND_URL='https://api.nahlah.ai'         # for expected-url calc

    python scripts/probe_d360_forwarding.py            # all stale tenants
    python scripts/probe_d360_forwarding.py --tenant 33
    python scripts/probe_d360_forwarding.py --silent-minutes 30 --include-healthy

Guarantees
----------

* **Read-only.** No INSERT / UPDATE / DELETE on the DB. No POST to
  360dialog. Only GET requests.
* **Stdlib only.** Uses ``psycopg2`` (already a repo dep) and
  ``urllib.request``. Runs on any operator laptop with no extra
  ``pip install``.
* **Phone numbers are masked** to last-4 digits. API keys are masked
  to last-4 characters. Secrets are reported as ``present|absent``.
* **Per-tenant isolation.** A failure on one tenant never aborts the
  rest of the scan.

Exit code is 0 when no problems were found, 2 when at least one
tenant shows a silent-loss verdict (A1–A5/A8/A9), 1 on unrecoverable
input (missing DSN, etc.).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
except ImportError:
    print(
        "[probe_d360] psycopg2 is required (pip install psycopg2-binary).",
        file=sys.stderr,
    )
    sys.exit(1)


# ── Defaults ─────────────────────────────────────────────────────────

DEFAULT_D360_BASE = "https://waba-v2.360dialog.io"
DEFAULT_HUB_BASE = "https://hub.360dialog.com"
DEFAULT_SILENT_MIN = 60
DEFAULT_TIMEOUT = 8.0


# ── Helpers ──────────────────────────────────────────────────────────


def _mask_tail(value: Optional[str], *, keep: int = 4) -> str:
    if not value:
        return "-"
    s = str(value)
    if len(s) <= keep:
        return "*" + s
    return "*" + s[-keep:]


def _http_get_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[int, Dict[str, Any]]:
    """Read-only GET that returns ``(status_code, parsed_body)``. Never
    raises — errors are returned in the body under ``error``."""
    req = urllib.request.Request(url=url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        status = exc.code
    except urllib.error.URLError as exc:
        return (0, {"error": f"network_error: {exc.reason!r}"})
    except Exception as exc:  # noqa: BLE001
        return (0, {"error": f"{type(exc).__name__}: {exc}"})
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(body, dict):
            body = {"raw": body}
    except Exception:
        body = {"raw": raw[:500].decode("utf-8", errors="replace")}
    return (status, body)


def _expected_channel_url(backend_url: str) -> str:
    return f"{backend_url.rstrip('/')}/webhook/whatsapp/360dialog"


def _normalize_url(value: Optional[str]) -> str:
    if not value:
        return ""
    return str(value).strip().rstrip("/")


# ── DB layer ────────────────────────────────────────────────────────


_STALE_SQL = """
SELECT
    wc.tenant_id,
    wc.id                                AS connection_id,
    wc.phone_number_id,
    wc.phone_number,
    wc.whatsapp_business_account_id      AS waba_id,
    wc.provider,
    wc.connection_type,
    wc.status,
    wc.sending_enabled,
    wc.webhook_verified,
    wc.access_token,
    wc.last_webhook_received_at,
    wc.webhook_coexistence_received_at,
    wc.webhook_status_received_at,
    wc.meta_quality_rating,
    wc.meta_messaging_limit,
    wc.extra_metadata,
    EXTRACT(EPOCH FROM (NOW() - wc.last_webhook_received_at))/60 AS silent_min,
    NOW() AT TIME ZONE 'UTC'             AS now_utc
FROM whatsapp_connections wc
WHERE wc.provider = 'dialog360'
  {tenant_filter}
ORDER BY silent_min DESC NULLS FIRST
"""


@dataclass
class ConnectionRow:
    tenant_id: int
    connection_id: int
    phone_number_id_local: Optional[str]
    phone_number: Optional[str]
    waba_id: Optional[str]
    provider: str
    connection_type: Optional[str]
    status: Optional[str]
    sending_enabled: bool
    webhook_verified: bool
    access_token: Optional[str]
    last_webhook_received_at: Any
    webhook_coexistence_received_at: Any
    webhook_status_received_at: Any
    meta_quality_rating: Optional[str]
    meta_messaging_limit: Optional[str]
    extra_metadata: Dict[str, Any]
    silent_min: Optional[float]

    @property
    def channel_id_at_hub(self) -> Optional[str]:
        pd = (self.extra_metadata or {}).get("provider_details") or {}
        return pd.get("channel_id") or pd.get("channel")

    @property
    def coexistence_secret_present(self) -> bool:
        em = self.extra_metadata or {}
        return bool(str(em.get("coexistence_internal_secret") or "").strip())


def fetch_connections(
    dsn: str,
    *,
    tenant_id: Optional[int] = None,
) -> List[ConnectionRow]:
    tenant_filter = ""
    params: List[Any] = []
    if tenant_id is not None:
        tenant_filter = "AND wc.tenant_id = %s"
        params = [int(tenant_id)]
    sql = _STALE_SQL.format(tenant_filter=tenant_filter)
    out: List[ConnectionRow] = []
    with psycopg2.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                em = r["extra_metadata"]
                if not isinstance(em, dict):
                    try:
                        em = json.loads(em) if em else {}
                    except Exception:
                        em = {}
                out.append(ConnectionRow(
                    tenant_id=r["tenant_id"],
                    connection_id=r["connection_id"],
                    phone_number_id_local=r["phone_number_id"],
                    phone_number=r["phone_number"],
                    waba_id=r["waba_id"],
                    provider=r["provider"],
                    connection_type=r["connection_type"],
                    status=r["status"],
                    sending_enabled=bool(r["sending_enabled"]),
                    webhook_verified=bool(r["webhook_verified"]),
                    access_token=r["access_token"],
                    last_webhook_received_at=r["last_webhook_received_at"],
                    webhook_coexistence_received_at=r["webhook_coexistence_received_at"],
                    webhook_status_received_at=r["webhook_status_received_at"],
                    meta_quality_rating=r["meta_quality_rating"],
                    meta_messaging_limit=r["meta_messaging_limit"],
                    extra_metadata=em or {},
                    silent_min=(float(r["silent_min"]) if r["silent_min"] is not None else None),
                ))
    return out


# ── 360dialog probes ────────────────────────────────────────────────


@dataclass
class WebhookConfigSnapshot:
    status: int
    url: str
    matches_expected: bool
    raw: Dict[str, Any]


def probe_channel_webhook(
    *,
    api_key: str,
    base_url: str,
    expected_url: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> WebhookConfigSnapshot:
    code, body = _http_get_json(
        url=f"{base_url.rstrip('/')}/v1/configs/webhook",
        headers={"D360-API-KEY": api_key},
        timeout=timeout,
    )
    url = ""
    if isinstance(body, dict):
        url = (
            str(body.get("url") or "")
            or str((body.get("webhook") or {}).get("url") or "")
            or str((body.get("data") or {}).get("url") or "")
        )
    return WebhookConfigSnapshot(
        status=code,
        url=url,
        matches_expected=(_normalize_url(url) == _normalize_url(expected_url)),
        raw=body,
    )


def probe_waba_webhook(
    *,
    api_key: str,
    base_url: str,
    expected_url: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[WebhookConfigSnapshot, List[str], Optional[str]]:
    """Returns (snapshot, numbers_on_this_waba, waba_id_returned)."""
    code, body = _http_get_json(
        url=f"{base_url.rstrip('/')}/waba_webhook",
        headers={"D360-API-KEY": api_key},
        timeout=timeout,
    )
    url = ""
    waba_id = None
    numbers: List[str] = []
    if isinstance(body, dict):
        url = (
            str(body.get("url") or "")
            or str((body.get("webhook") or {}).get("url") or "")
        )
        waba_id = body.get("waba_id") or (body.get("data") or {}).get("waba_id")
        raw_numbers = (
            body.get("numbers_on_this_waba")
            or (body.get("data") or {}).get("numbers_on_this_waba")
            or []
        )
        if isinstance(raw_numbers, list):
            numbers = [str(n) for n in raw_numbers if n]
    return (
        WebhookConfigSnapshot(
            status=code,
            url=url,
            matches_expected=(_normalize_url(url) == _normalize_url(expected_url)),
            raw=body,
        ),
        numbers,
        waba_id,
    )


def probe_partner_hub_channel(
    *,
    partner_key: str,
    partner_id: str,
    channel_id: str,
    hub_base: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[int, Dict[str, Any]]:
    return _http_get_json(
        url=f"{hub_base.rstrip('/')}/api/v2/partners/{partner_id}/channels/{channel_id}",
        headers={"Authorization": f"Bearer {partner_key}"},
        timeout=timeout,
    )


# ── Verdict logic ───────────────────────────────────────────────────


@dataclass
class TenantVerdict:
    tenant_id: int
    suspects: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    silent_loss: bool = False


def classify(
    row: ConnectionRow,
    *,
    channel: Optional[WebhookConfigSnapshot],
    waba: Optional[WebhookConfigSnapshot],
    waba_numbers: List[str],
    hub_status: Optional[int],
    hub_body: Optional[Dict[str, Any]],
    expected_url: str,
    silent_min_threshold: float,
) -> TenantVerdict:
    v = TenantVerdict(tenant_id=row.tenant_id)

    # ── A10 quality rating ────────────────────────────────────────
    if (row.meta_quality_rating or "").upper() == "RED":
        v.suspects.append("A10")
        v.notes.append("meta_quality_rating=RED — Meta may be throttling delivery.")
        v.silent_loss = True

    # ── A1 / A2 channel webhook ───────────────────────────────────
    if channel is None:
        v.suspects.append("A1")
        v.notes.append("No D360 channel webhook check possible (missing api_key).")
        v.silent_loss = True
    else:
        if not channel.url:
            v.suspects.append("A1")
            v.notes.append(
                f"D360 channel webhook is NOT configured "
                f"(GET /v1/configs/webhook status={channel.status})."
            )
            v.silent_loss = True
        elif not channel.matches_expected:
            v.suspects.append("A2")
            v.notes.append(
                f"D360 channel webhook URL mismatch: "
                f"stored={channel.url!r} expected={expected_url!r}."
            )
            v.silent_loss = True

    # ── A4 WABA webhook ───────────────────────────────────────────
    if waba is None:
        v.suspects.append("A4")
        v.notes.append("No D360 WABA webhook check possible (missing api_key).")
        v.silent_loss = True
    else:
        if not waba.url:
            v.suspects.append("A4")
            v.notes.append(
                f"D360 WABA-level webhook is NOT configured "
                f"(GET /waba_webhook status={waba.status}). "
                "This is the fallback when channel webhook is orphaned."
            )
            v.silent_loss = True
        elif not waba.matches_expected:
            v.suspects.append("A4")
            v.notes.append(
                f"D360 WABA webhook URL mismatch: "
                f"stored={waba.url!r} expected={expected_url!r}."
            )
            v.silent_loss = True

    # ── A3 phone_number_id drift ─────────────────────────────────
    if hub_body and isinstance(hub_body, dict):
        hub_pid = (
            hub_body.get("phone_number_id")
            or hub_body.get("phone_number")
            or (hub_body.get("data") or {}).get("phone_number_id")
        )
        if hub_pid and row.phone_number_id_local and str(hub_pid) != str(row.phone_number_id_local):
            v.suspects.append("A3")
            v.notes.append(
                f"phone_number_id drift: local={row.phone_number_id_local!r} "
                f"hub={hub_pid!r}. 360dialog rotated the channel; the "
                "old per-channel webhook is orphaned."
            )
            v.silent_loss = True

    # ── A5 sibling numbers on WABA we don't know about ────────────
    if waba_numbers:
        unknown = [
            n for n in waba_numbers
            if str(n) != str(row.phone_number_id_local or "")
            and str(n) != str(row.phone_number or "")
        ]
        if unknown:
            v.notes.append(
                f"WABA carries additional numbers: {unknown!r}. If any "
                "still has a stale per-channel webhook, inbound from "
                "that number may bypass Nahla (set with override_all=True)."
            )

    # ── A9 BACKEND_URL drift sanity ──────────────────────────────
    if channel and channel.url and not channel.matches_expected and (
        "nahlah" not in channel.url.lower() and "nahla" not in channel.url.lower()
    ):
        v.suspects.append("A9")
        v.notes.append(
            "Channel webhook URL does not even point at a Nahla domain — "
            "likely BACKEND_URL changed or the channel was rebound to "
            "a different receiver."
        )
        v.silent_loss = True

    # ── Silence vs activity ──────────────────────────────────────
    if row.silent_min is None:
        v.notes.append("last_webhook_received_at is NULL — channel never received anything.")
    elif row.silent_min >= silent_min_threshold:
        v.notes.append(
            f"silent for {row.silent_min:.0f} minutes "
            f"(threshold={silent_min_threshold:.0f})."
        )

    # ── No issues found → likely Region 1/3 elsewhere, or A8 edge ──
    if not v.suspects:
        if row.silent_min is not None and row.silent_min >= silent_min_threshold:
            v.suspects.append("A8")
            v.notes.append(
                "Channel + WABA URLs match, phone_id stable, but no webhook "
                "received recently. Likely edge-layer drop (Railway/proxy "
                "5xx) or 360dialog forwarding outage. Verify with curl."
            )
            v.silent_loss = True

    return v


# ── Reporting ───────────────────────────────────────────────────────


def _fmt_dt(value: Any) -> str:
    if value is None:
        return "never"
    try:
        return value.isoformat(sep=" ", timespec="seconds")
    except Exception:
        return str(value)


def render_tenant(
    row: ConnectionRow,
    verdict: TenantVerdict,
    *,
    channel: Optional[WebhookConfigSnapshot],
    waba: Optional[WebhookConfigSnapshot],
    waba_numbers: List[str],
    hub_status: Optional[int],
    expected_url: str,
) -> str:
    lines: List[str] = []
    lines.append("-" * 72)
    severity = "SILENT_LOSS" if verdict.silent_loss else "OK_OR_DOWNSTREAM"
    lines.append(
        f"  TENANT {row.tenant_id:>4}  |  status={row.status}  "
        f"sending_enabled={row.sending_enabled}  webhook_verified={row.webhook_verified}  "
        f"|  {severity}"
    )
    lines.append("-" * 72)
    lines.append(
        f"    phone_number_id (local):     {row.phone_number_id_local or '-'}"
    )
    lines.append(
        f"    phone_number (masked):       {_mask_tail(row.phone_number)}"
    )
    lines.append(
        f"    waba_id:                     {row.waba_id or '-'}"
    )
    lines.append(
        f"    connection_type:             {row.connection_type or '-'}"
    )
    lines.append(
        f"    access_token (tail):         {_mask_tail(row.access_token)}"
    )
    lines.append(
        f"    coexistence_secret:          "
        f"{'present' if row.coexistence_secret_present else 'absent'}"
    )
    lines.append(
        f"    channel_id at hub:           {row.channel_id_at_hub or '-'}"
    )
    lines.append(
        f"    meta_quality_rating:         {row.meta_quality_rating or '-'}"
    )
    lines.append(
        f"    meta_messaging_limit:        {row.meta_messaging_limit or '-'}"
    )
    lines.append("")
    lines.append(
        f"    last_webhook_received_at:        {_fmt_dt(row.last_webhook_received_at)}"
    )
    lines.append(
        f"    webhook_coexistence_received_at: {_fmt_dt(row.webhook_coexistence_received_at)}"
    )
    lines.append(
        f"    webhook_status_received_at:      {_fmt_dt(row.webhook_status_received_at)}"
    )
    if row.silent_min is not None:
        lines.append(
            f"    silent_minutes:              {row.silent_min:.0f}"
        )
    lines.append("")
    lines.append("    expected_channel_url:        " + expected_url)
    if channel is not None:
        lines.append(
            f"    D360 channel webhook URL:    {channel.url or '-'}   "
            f"matches={channel.matches_expected}  (HTTP {channel.status})"
        )
    else:
        lines.append("    D360 channel webhook:        SKIPPED (no api_key)")
    if waba is not None:
        lines.append(
            f"    D360 WABA webhook URL:       {waba.url or '-'}   "
            f"matches={waba.matches_expected}  (HTTP {waba.status})"
        )
    else:
        lines.append("    D360 WABA webhook:           SKIPPED (no api_key)")
    if waba_numbers:
        lines.append(
            f"    numbers_on_this_waba:        {waba_numbers!r}"
        )
    if hub_status is not None:
        lines.append(
            f"    Partner Hub channel meta:    HTTP {hub_status}"
        )
    lines.append("")
    lines.append(f"    SUSPECTS: {', '.join(verdict.suspects) or '(none)'}")
    for n in verdict.notes:
        lines.append(f"      - {n}")
    lines.append("")
    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_d360_forwarding",
        description=(
            "Read-only probe for Meta -> 360dialog -> Nahla webhook "
            "forwarding gaps (W2.2-INV verdict A)."
        ),
    )
    p.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL", ""),
        help="Postgres DSN (defaults to DATABASE_URL env).",
    )
    p.add_argument(
        "--tenant",
        type=int,
        default=None,
        help="Scan a single tenant id (default: all dialog360 connections).",
    )
    p.add_argument(
        "--silent-minutes",
        type=float,
        default=float(os.environ.get("PROBE_SILENT_MIN", DEFAULT_SILENT_MIN)),
        help="Threshold to flag a connection as stale (default 60).",
    )
    p.add_argument(
        "--include-healthy",
        action="store_true",
        help="Also render tenants that look healthy.",
    )
    p.add_argument(
        "--backend-url",
        default=os.environ.get("BACKEND_URL", ""),
        help="Public Nahla URL to compute expected webhook URL "
             "(defaults to BACKEND_URL env).",
    )
    p.add_argument(
        "--d360-base",
        default=os.environ.get("D360_API_BASE_URL", DEFAULT_D360_BASE),
    )
    p.add_argument(
        "--hub-base",
        default=os.environ.get("D360_PARTNER_HUB_BASE", DEFAULT_HUB_BASE),
    )
    p.add_argument(
        "--partner-key",
        default=os.environ.get("D360_PARTNER_API_KEY", ""),
        help="Optional — enables Partner Hub channel-meta probe (A3 drift).",
    )
    p.add_argument(
        "--partner-id",
        default=os.environ.get("D360_PARTNER_ID", ""),
    )
    p.add_argument(
        "--no-network",
        action="store_true",
        help="Skip all HTTP probes (DB-only Probe 1).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON per tenant (machine-readable).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Per-request timeout in seconds (default 8).",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.dsn:
        print(
            "[probe_d360] DATABASE_URL is required (env or --dsn).",
            file=sys.stderr,
        )
        return 1
    if not args.backend_url:
        print(
            "[probe_d360] WARNING: BACKEND_URL not set — expected-url "
            "comparison will be empty.",
            file=sys.stderr,
        )
    expected_url = _expected_channel_url(args.backend_url or "")

    try:
        rows = fetch_connections(args.dsn, tenant_id=args.tenant)
    except Exception as exc:
        print(f"[probe_d360] DB scan failed: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("[probe_d360] no dialog360 connections matched the filter.")
        return 0

    any_silent_loss = False
    rendered: List[Dict[str, Any]] = []

    for row in rows:
        is_stale = (
            row.silent_min is None
            or row.silent_min >= args.silent_minutes
        )
        if not is_stale and not args.include_healthy:
            continue

        channel_snap: Optional[WebhookConfigSnapshot] = None
        waba_snap: Optional[WebhookConfigSnapshot] = None
        waba_numbers: List[str] = []
        hub_status: Optional[int] = None
        hub_body: Optional[Dict[str, Any]] = None

        if not args.no_network:
            if row.access_token:
                try:
                    channel_snap = probe_channel_webhook(
                        api_key=row.access_token,
                        base_url=args.d360_base,
                        expected_url=expected_url,
                        timeout=args.timeout,
                    )
                except Exception as exc:
                    channel_snap = WebhookConfigSnapshot(
                        status=0, url="",
                        matches_expected=False,
                        raw={"error": f"channel_probe_failed: {exc}"},
                    )
                try:
                    waba_snap, waba_numbers, _ = probe_waba_webhook(
                        api_key=row.access_token,
                        base_url=args.d360_base,
                        expected_url=expected_url,
                        timeout=args.timeout,
                    )
                except Exception as exc:
                    waba_snap = WebhookConfigSnapshot(
                        status=0, url="",
                        matches_expected=False,
                        raw={"error": f"waba_probe_failed: {exc}"},
                    )
            if (
                args.partner_key
                and args.partner_id
                and row.channel_id_at_hub
            ):
                try:
                    hub_status, hub_body = probe_partner_hub_channel(
                        partner_key=args.partner_key,
                        partner_id=args.partner_id,
                        channel_id=str(row.channel_id_at_hub),
                        hub_base=args.hub_base,
                        timeout=args.timeout,
                    )
                except Exception as exc:
                    hub_status = 0
                    hub_body = {"error": f"hub_probe_failed: {exc}"}

        verdict = classify(
            row,
            channel=channel_snap,
            waba=waba_snap,
            waba_numbers=waba_numbers,
            hub_status=hub_status,
            hub_body=hub_body,
            expected_url=expected_url,
            silent_min_threshold=args.silent_minutes,
        )
        if verdict.silent_loss:
            any_silent_loss = True

        if args.json:
            rendered.append({
                "tenant_id": row.tenant_id,
                "phone_number_id_local": row.phone_number_id_local,
                "phone_number_masked": _mask_tail(row.phone_number),
                "waba_id": row.waba_id,
                "silent_min": row.silent_min,
                "expected_url": expected_url,
                "channel_url": getattr(channel_snap, "url", None),
                "channel_matches": getattr(channel_snap, "matches_expected", None),
                "channel_status": getattr(channel_snap, "status", None),
                "waba_url": getattr(waba_snap, "url", None),
                "waba_matches": getattr(waba_snap, "matches_expected", None),
                "waba_status": getattr(waba_snap, "status", None),
                "waba_numbers": waba_numbers,
                "hub_status": hub_status,
                "suspects": verdict.suspects,
                "notes": verdict.notes,
                "silent_loss": verdict.silent_loss,
            })
        else:
            print(render_tenant(
                row, verdict,
                channel=channel_snap,
                waba=waba_snap,
                waba_numbers=waba_numbers,
                hub_status=hub_status,
                expected_url=expected_url,
            ))

    if args.json:
        print(json.dumps({
            "expected_channel_url": expected_url,
            "tenants_scanned":      len(rows),
            "tenants_reported":     len(rendered),
            "any_silent_loss":      any_silent_loss,
            "results":              rendered,
        }, indent=2, default=str))
    else:
        print("=" * 72)
        print(
            f"  SUMMARY: scanned={len(rows)}  silent_loss_detected="
            f"{any_silent_loss}"
        )
        print("=" * 72)

    return 2 if any_silent_loss else 0


if __name__ == "__main__":
    sys.exit(main())
