"""
scripts/audit_tenant_assets.py
──────────────────────────────
Read-only diagnostic for a single tenant's "asset readiness" — i.e. for every
class of deterministic asset the AI may need to attach to a WhatsApp reply
(store URL, Google Maps URL, staff phones, payment barcodes, generic media),
what is stored where, what does the snapshot expose, and what would each
resolver chain return RIGHT NOW.

This script is the **G1** step from the May 2026 asset-resolution root-cause
investigation. It does NOT write anything to the DB. It does NOT call the
WhatsApp API. It only issues SELECT queries and replicates the resolver logic
the AI uses at runtime so the report reflects exactly what the AI sees.

Safety
──────
  * DSN is read from the env var ``DATABASE_URL`` only. Never CLI, never logs.
  * All queries are read-only (SELECT against application tables).
  * Free-text fields with potential PII (phones, addresses) are scanned only
    for length + pattern counts; raw bodies are previewed at most 120 chars
    and only when explicitly requested with ``--show-text``.

Usage (PowerShell)::

    $env:DATABASE_URL = "postgresql://user:pwd@host:port/db"
    python scripts/audit_tenant_assets.py --tenant-id 33
    python scripts/audit_tenant_assets.py --tenant-id 33 --format json > report.json
    Remove-Item Env:DATABASE_URL

Sections
────────
  [1]  Tenant summary
  [2]  Store-knowledge snapshot health
  [3]  store_url — full chain (snapshot → settings → integrations)
  [4]  Maps URL — full chain (NB: not currently in snapshot — design gap)
  [5]  Staff phones — free-text scan (NB: no schema — design gap)
  [6]  Payment assets — keyed media library + KB links
  [7]  General media library
  [8]  Merchant knowledge sections by kind
  [9]  Integrations summary
  [10] Auto-detected red flags
  [11] Canonical-ask resolver simulation
  [12] Audit summary

Each "chain" section ends with a resolver verdict that mirrors the
corresponding helper in ``backend/modules/ai/postprocess/safety_nets.py``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from collections import Counter
from typing import Any

try:
    import psycopg2
    import psycopg2.extras
except ImportError as exc:  # pragma: no cover
    print(f"ERROR: psycopg2 is required ({exc}). pip install psycopg2-binary.")
    sys.exit(2)

# Force UTF-8 on Windows consoles so Arabic prints cleanly.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


# ─── Helpers ───────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _h1(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _h2(title: str) -> None:
    print()
    print("─── " + title + " " + "─" * max(0, 70 - len(title)))


def _kv(label: str, value: Any, *, indent: int = 2) -> None:
    pad = " " * indent
    if value is None:
        rendered = "(none)"
    elif isinstance(value, bool):
        rendered = "yes" if value else "no"
    elif isinstance(value, (list, dict)):
        rendered = json.dumps(value, ensure_ascii=False)
    else:
        rendered = str(value)
    print(f"{pad}{label:<32} : {rendered}")


def _preview(text: str | None, *, limit: int = 120) -> str:
    if not text:
        return "(empty)"
    one_line = re.sub(r"\s+", " ", str(text)).strip()
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit] + "…"


def _safe_get_json(row: dict | None, column: str) -> dict:
    """JSONB columns come back as dicts when registered, or as JSON strings
    when not. Coerce defensively so callers never crash."""
    if not row:
        return {}
    raw = row.get(column)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return {}


# Phone-pattern regex — mirrors backend/services/call_resolver.py + the
# Saudi mobile pattern used by the outbound sanitizer. We deliberately keep
# it permissive in the audit so we surface MORE candidates rather than fewer.
_PHONE_RE = re.compile(
    r"(?:\+?966|00966|0)?5\d{8}|\+\d{7,15}"
)


# Trigger phrases the store-link safety net uses — mirrors
# backend/modules/ai/postprocess/safety_nets.py:_looks_like_store_link_request
_STORE_LINK_TRIGGERS = (
    "رابط المتجر", "رابط متجركم", "وين المتجر",
    "موقع المتجر", "موقعكم", "لينك المتجر", "store link", "store url",
)
_LOCATION_TRIGGERS = (
    "وين موقعكم", "وين الموقع", "الموقع", "لوكيشن", "خرايط",
    "خرائط", "google maps", "location", "address", "الفرع",
)
_STAFF_TRIGGERS = (
    "ابي اكلم", "أبي أكلم", "اكلم", "موظف", "أمين", "محاسب",
    "مسؤول", "خدمة العملاء",
)
_PAYMENT_TRIGGERS = (
    "باركود", "تحويل", "ايبان", "iban", "qr", "راجحي",
    "الراجحي", "اهلي", "الأهلي", "stc pay", "stcpay", "مدى",
)


# ─── Section runners ───────────────────────────────────────────────────────


def section_tenant(cur, tenant_id: int) -> dict:
    """[1] Tenant summary."""
    _h1(f"[1] TENANT SUMMARY — id={tenant_id}")
    cur.execute(
        """
        SELECT id, name, domain, is_active, created_at,
               google_maps_link, apple_maps_link, store_address,
               ai_blocked_numbers, branding
        FROM tenants
        WHERE id = %s
        """,
        (tenant_id,),
    )
    row = cur.fetchone()
    if not row:
        print(f"  ERROR: tenant id={tenant_id} not found")
        return {"exists": False}

    blocked = row.get("ai_blocked_numbers") or []
    if isinstance(blocked, str):
        try:
            blocked = json.loads(blocked)
        except Exception:  # noqa: BLE001
            blocked = []

    _kv("name", row["name"])
    _kv("domain", row["domain"])
    _kv("is_active", row["is_active"])
    _kv("created_at", row["created_at"])
    _kv("store_address (rarely read)", _preview(row["store_address"], limit=80))
    _kv("google_maps_link (UNUSED by AI)", _preview(row["google_maps_link"], limit=80))
    _kv("apple_maps_link  (UNUSED by AI)", _preview(row["apple_maps_link"], limit=80))
    _kv("ai_blocked_numbers count", len(blocked) if isinstance(blocked, list) else 0)

    return {
        "exists": True,
        "name": row["name"],
        "domain": row["domain"],
        "is_active": row["is_active"],
        "store_address": row["store_address"],
        "google_maps_link": row["google_maps_link"],
        "apple_maps_link": row["apple_maps_link"],
        "ai_blocked_numbers_count": len(blocked) if isinstance(blocked, list) else 0,
    }


def section_snapshot(cur, tenant_id: int) -> dict:
    """[2] Store-knowledge snapshot health."""
    _h1("[2] STORE KNOWLEDGE SNAPSHOT")
    cur.execute(
        """
        SELECT id, last_full_sync_at, last_incremental_sync_at, sync_version,
               store_profile, catalog_summary, shipping_summary,
               policy_summary, coupon_summary
        FROM store_knowledge_snapshots
        WHERE tenant_id = %s
        """,
        (tenant_id,),
    )
    row = cur.fetchone()
    if not row:
        _kv("exists", False)
        print("  ⚠️  no snapshot row — the AI is operating WITHOUT any structured store context.")
        return {"exists": False}

    age_hours = None
    last_full = row["last_full_sync_at"]
    if last_full:
        delta = _dt.datetime.now(_dt.timezone.utc) - last_full.replace(
            tzinfo=last_full.tzinfo or _dt.timezone.utc
        )
        age_hours = round(delta.total_seconds() / 3600.0, 2)

    is_fresh = (age_hours is not None) and age_hours <= 6.0
    profile = _safe_get_json(row, "store_profile")
    expected_profile_keys = {
        "store_name", "store_url", "logo_url", "description",
        "contact_phone", "contact_email", "pages",
    }
    present = set(profile.keys())
    missing = sorted(expected_profile_keys - present)
    extra = sorted(present - expected_profile_keys)

    _kv("exists", True)
    _kv("last_full_sync_at", last_full)
    _kv("last_incremental_sync_at", row["last_incremental_sync_at"])
    _kv("sync_version", row["sync_version"])
    _kv("age_hours", age_hours)
    _kv("is_fresh (≤6h)", is_fresh)
    _kv("store_profile keys present", sorted(present))
    _kv("store_profile keys missing (canonical 7)", missing or "(none)")
    _kv("store_profile extra keys", extra or "(none)")

    catalog = _safe_get_json(row, "catalog_summary")
    _kv("catalog_summary.total_products", catalog.get("total_products"))
    _kv("catalog_summary.top_products N", len(catalog.get("top_products") or []))

    return {
        "exists": True,
        "age_hours": age_hours,
        "is_fresh": is_fresh,
        "store_profile": profile,
        "missing_canonical_keys": missing,
    }


def section_store_url(cur, tenant_id: int, snapshot_profile: dict) -> dict:
    """[3] store_url full chain — mirrors safety_nets._lookup_tenant_store_url."""
    _h1("[3] STORE URL — full resolver chain")

    # Settings
    cur.execute(
        "SELECT store_settings, whatsapp_settings, ai_settings "
        "FROM tenant_settings WHERE tenant_id = %s",
        (tenant_id,),
    )
    srow = cur.fetchone() or {}
    store_settings = _safe_get_json(srow, "store_settings")
    wa_settings = _safe_get_json(srow, "whatsapp_settings")
    ai_settings = _safe_get_json(srow, "ai_settings")

    # Integrations
    cur.execute(
        """
        SELECT provider, enabled, external_store_id, config
        FROM integrations
        WHERE tenant_id = %s
        ORDER BY provider
        """,
        (tenant_id,),
    )
    integrations = cur.fetchall() or []

    snapshot_url = (snapshot_profile or {}).get("store_url") or ""
    settings_url = store_settings.get("store_url") or ""
    settings_button_url = wa_settings.get("store_button_url") or ""

    _h2("Layer A — snapshot.store_profile.store_url")
    _kv("value", _preview(snapshot_url, limit=120))

    _h2("Layer B — tenant_settings.store_settings.store_url  (read by resolver)")
    _kv("value", _preview(settings_url, limit=120))

    _h2("Layer C — tenant_settings.whatsapp_settings.store_button_url  (read by resolver, May 2026 #35)")
    _kv("value", _preview(settings_button_url, limit=120))

    _h2("Layer D — integrations.config.{store_url, domain, shop_domain, storefront_url}")
    integration_urls: list[tuple[str, str, str]] = []  # (provider, key, value)
    for ig in integrations:
        cfg = _safe_get_json(ig, "config")
        for key in ("store_url", "domain", "shop_domain", "storefront_url"):
            val = cfg.get(key)
            if val:
                integration_urls.append((ig["provider"], key, val))
        _kv(
            f"{ig['provider']} (enabled={ig['enabled']}, ext_id={ig['external_store_id']})",
            json.dumps(
                {k: cfg.get(k) for k in ("store_url", "domain", "shop_domain", "storefront_url")},
                ensure_ascii=False,
            ),
            indent=4,
        )

    # Resolver verdict — mirrors safety_nets._lookup_tenant_store_url
    # (May 2026 #35 chain order: snapshot → store_settings →
    # whatsapp_button → integrations).
    final_url, source = "", "none"
    if snapshot_url:
        final_url, source = snapshot_url, "snapshot"
    elif settings_url:
        final_url, source = settings_url, "store_settings"
    elif settings_button_url:
        final_url, source = settings_button_url, "whatsapp_button"
    else:
        for prov, key, val in integration_urls:
            final_url, source = val, f"integration:{prov}.{key}"
            break

    _h2("Resolver verdict (mirrors _lookup_tenant_store_url)")
    _kv("final_resolved", _preview(final_url, limit=120))
    _kv("source", source)
    _kv("would AI deliver on 'ابي رابط المتجر'?", bool(final_url))

    return {
        "snapshot": snapshot_url,
        "settings": settings_url,
        "store_button_url": settings_button_url,
        "integrations": integration_urls,
        "final_resolved": final_url,
        "source": source,
        "would_resolve": bool(final_url),
        "_store_settings": store_settings,
        "_wa_settings": wa_settings,
        "_ai_settings": ai_settings,
    }


def section_maps(cur, tenant_id: int, tenant: dict, store_settings: dict) -> dict:
    """[4] Maps URL — currently NO snapshot mirror (design gap)."""
    _h1("[4] LOCATION / MAPS URL — full chain")

    settings_maps = store_settings.get("google_maps_location") or ""

    _h2("Layer A — tenant_settings.store_settings.google_maps_location  (read by dashboard)")
    _kv("value", _preview(settings_maps, limit=120))

    _h2("Layer B — tenants.google_maps_link / apple_maps_link / store_address")
    _kv("tenants.google_maps_link  (NOT READ BY AI)", _preview(tenant.get("google_maps_link"), limit=120))
    _kv("tenants.apple_maps_link   (NOT READ BY AI)", _preview(tenant.get("apple_maps_link"), limit=120))
    _kv("tenants.store_address     (NOT READ BY AI)", _preview(tenant.get("store_address"), limit=120))

    _h2("Layer C — merchant_knowledge_sections (kind in branches/store_story/custom)")
    cur.execute(
        """
        SELECT id, kind, is_active, body
        FROM merchant_knowledge_sections
        WHERE tenant_id = %s
          AND kind IN ('branches', 'store_story', 'custom')
        ORDER BY kind, id
        """,
        (tenant_id,),
    )
    kb_rows = cur.fetchall() or []
    kb_with_url = 0
    for r in kb_rows:
        body = r.get("body") or ""
        has_url = bool(re.search(r"https?://\S+", body))
        if has_url:
            kb_with_url += 1
    _kv("kind=branches/store_story/custom rows", len(kb_rows))
    _kv("...of which body contains a URL", kb_with_url)

    _h2("Layer D — ai_media_library media_key='store_location_image'")
    cur.execute(
        """
        SELECT id, title, is_active, file_url
        FROM ai_media_library
        WHERE tenant_id = %s AND media_key = 'store_location_image'
        """,
        (tenant_id,),
    )
    location_images = cur.fetchall() or []
    _kv("count", len(location_images))
    for img in location_images:
        _kv(
            f"  #{img['id']} active={img['is_active']}",
            _preview(img.get("file_url"), limit=80),
            indent=4,
        )

    _h2("Resolver verdict — NO MAPS-URL RESOLVER EXISTS (design gap)")
    print("    ✗ AI cannot deterministically deliver a Google Maps URL today.")
    print("    Best AI can do: surface the URL only if it leaks into a KB section")
    print("    or via the manual_knowledge_base free-text overlay.")

    return {
        "settings_maps": settings_maps,
        "tenant_google_maps_link": tenant.get("google_maps_link"),
        "kb_rows_with_url": kb_with_url,
        "kb_rows_total": len(kb_rows),
        "location_images": len(location_images),
        "would_resolve": False,
    }


def section_staff(cur, tenant_id: int, wa_settings: dict, ai_settings: dict) -> dict:
    """[5] Staff phones — currently NO schema (design gap)."""
    _h1("[5] STAFF PHONES — full chain")

    owner_wa = wa_settings.get("owner_whatsapp_number") or ""
    owner_instr = ai_settings.get("owner_instructions") or ""
    manual_kb = ai_settings.get("manual_knowledge_base") or ""

    _h2("Layer A — tenant_settings.whatsapp_settings.owner_whatsapp_number")
    _kv("value", _preview(owner_wa, limit=80))

    _h2("Layer B — tenant_settings.ai_settings.owner_instructions  (free text)")
    _kv("length", len(owner_instr))
    owner_phones = _PHONE_RE.findall(owner_instr or "")
    _kv("phone-pattern matches", owner_phones[:10] if owner_phones else "(none)")

    _h2("Layer C — tenant_settings.ai_settings.manual_knowledge_base  (legacy)")
    _kv("length", len(manual_kb))
    manual_phones = _PHONE_RE.findall(manual_kb or "")
    _kv("phone-pattern matches", manual_phones[:10] if manual_phones else "(none)")

    _h2("Layer D — merchant_knowledge_sections kind in owner_identity/branches/custom")
    cur.execute(
        """
        SELECT id, kind, is_active, body
        FROM merchant_knowledge_sections
        WHERE tenant_id = %s
          AND kind IN ('owner_identity', 'branches', 'custom')
        ORDER BY kind, id
        """,
        (tenant_id,),
    )
    rows = cur.fetchall() or []
    section_phone_counts: list[tuple[int, str, int]] = []
    all_section_phones: list[str] = []
    for r in rows:
        body = r.get("body") or ""
        phones = _PHONE_RE.findall(body)
        section_phone_counts.append((r["id"], r["kind"], len(phones)))
        all_section_phones.extend(phones)
    _kv("section rows total", len(rows))
    for sid, kind, count in section_phone_counts:
        _kv(f"  section #{sid} ({kind})", f"phones found={count}", indent=4)

    distinct_phones = sorted(set(all_section_phones + owner_phones + manual_phones))

    _h2("Resolver verdict — NO STAFF-DIRECTORY SCHEMA (design gap)")
    _kv("distinct phones detected in free text", len(distinct_phones))
    if distinct_phones:
        _kv("sample", distinct_phones[:10])
    print("    ✗ AI cannot deterministically map 'ابي أكلم أمين' → a specific staff phone.")
    print("    LLM guesses from KB free text; no validation of tenant ownership.")

    return {
        "owner_whatsapp_number": owner_wa,
        "distinct_phones_in_freetext": distinct_phones,
        "would_resolve_by_name": False,
        "owner_only_resolves": bool(owner_wa),
    }


def section_payments(cur, tenant_id: int, store_settings: dict) -> dict:
    """[6] Payment assets — keyed media library + KB links."""
    _h1("[6] PAYMENT ASSETS — keyed media + KB links")

    payment_keys = (
        "payment_rajhi_barcode",
        "payment_alahli_barcode",
        "payment_barq_barcode",
        "payment_stcpay_qr",
        "payment_mobilypay_qr",
        "payment_bank_transfer_image",
    )

    _h2("Layer A — ai_media_library by registered payment media_key")
    cur.execute(
        """
        SELECT media_key, id, title, is_active, file_url, priority, media_type
        FROM ai_media_library
        WHERE tenant_id = %s
          AND media_key IS NOT NULL
        ORDER BY media_key, priority
        """,
        (tenant_id,),
    )
    keyed = cur.fetchall() or []
    by_key: dict[str, list[dict]] = {}
    for r in keyed:
        by_key.setdefault(r["media_key"], []).append(dict(r))
    payment_summary: dict[str, dict] = {}
    for key in payment_keys:
        rows = by_key.get(key) or []
        active = sum(1 for r in rows if r.get("is_active"))
        payment_summary[key] = {
            "count": len(rows),
            "active": active,
            "first_url": _preview((rows[0].get("file_url") if rows else ""), limit=80),
        }
        _kv(key, f"count={len(rows)} active={active}")
        for r in rows[:2]:
            _kv(
                f"  #{r['id']} active={r['is_active']} type={r['media_type']}",
                _preview(r.get("file_url"), limit=80),
                indent=4,
            )

    _h2("Layer B — ai_media_library rows with media_key=NULL (unkeyed)")
    cur.execute(
        "SELECT COUNT(*) AS n FROM ai_media_library "
        "WHERE tenant_id = %s AND media_key IS NULL",
        (tenant_id,),
    )
    unkeyed = (cur.fetchone() or {}).get("n", 0)
    _kv("count", unkeyed)
    _kv("ratio of total media", f"{unkeyed}/({unkeyed + len(keyed)})")

    _h2("Layer C — merchant_knowledge_media link_role='barcode'")
    # NOTE: merchant_knowledge_media has NO tenant_id column of its own —
    # the tenant scope is reached through the parent section.
    # See database/models.py:3130-3160.
    cur.execute(
        """
        SELECT mkm.id, mkm.media_id, mkm.link_role, m.media_key, m.is_active, mks.kind
        FROM merchant_knowledge_media mkm
        JOIN ai_media_library m ON m.id = mkm.media_id
        JOIN merchant_knowledge_sections mks ON mks.id = mkm.section_id
        WHERE mks.tenant_id = %s AND mkm.link_role = 'barcode'
        ORDER BY mks.kind, mkm.id
        """,
        (tenant_id,),
    )
    barcode_links = cur.fetchall() or []
    _kv("barcode link rows", len(barcode_links))
    for ln in barcode_links[:8]:
        _kv(
            f"  link #{ln['id']} media#{ln['media_id']} section.kind={ln['kind']}",
            f"media_key={ln['media_key']}",
            indent=4,
        )

    _h2("Layer D — store_settings.payment_methods (names only — NOT assets)")
    pm = store_settings.get("payment_methods") or []
    _kv("declared method names", pm if isinstance(pm, list) else "(invalid)")

    _h2("Layer E — merchant_knowledge_sections kind in payment_method/bank_transfer")
    cur.execute(
        """
        SELECT id, kind, is_active, body
        FROM merchant_knowledge_sections
        WHERE tenant_id = %s
          AND kind IN ('payment_method', 'bank_transfer')
        ORDER BY kind, id
        """,
        (tenant_id,),
    )
    payment_sections = cur.fetchall() or []
    _kv("count", len(payment_sections))
    for r in payment_sections[:5]:
        _kv(
            f"  #{r['id']} ({r['kind']}, active={r['is_active']})",
            _preview(r.get("body"), limit=80),
            indent=4,
        )

    _h2("Resolver verdict")
    has_any_payment = any(s["active"] > 0 for s in payment_summary.values())
    _kv("at least one ACTIVE payment asset?", has_any_payment)
    _kv("Rajhi resolvable?", payment_summary["payment_rajhi_barcode"]["active"] > 0)
    _kv("STC Pay resolvable?", payment_summary["payment_stcpay_qr"]["active"] > 0)
    _kv("Generic 'كيف أحول' resolvable?", has_any_payment)

    return {
        "by_key": payment_summary,
        "unkeyed_count": unkeyed,
        "barcode_links": len(barcode_links),
        "payment_sections": len(payment_sections),
        "has_any_payment_asset": has_any_payment,
    }


def section_media_library(cur, tenant_id: int) -> dict:
    """[7] General media library overview."""
    _h1("[7] GENERAL MEDIA LIBRARY")
    cur.execute(
        """
        SELECT media_type, is_active, media_key
        FROM ai_media_library
        WHERE tenant_id = %s
        """,
        (tenant_id,),
    )
    rows = cur.fetchall() or []
    by_type = Counter(r["media_type"] for r in rows)
    active_count = sum(1 for r in rows if r["is_active"])
    with_key = sum(1 for r in rows if r["media_key"])
    _kv("total rows", len(rows))
    _kv("active", active_count)
    _kv("with media_key", with_key)
    _kv("without media_key", len(rows) - with_key)
    _kv("by media_type", dict(by_type))
    return {
        "total": len(rows),
        "active": active_count,
        "with_key": with_key,
        "by_type": dict(by_type),
    }


def section_kb_sections(cur, tenant_id: int) -> dict:
    """[8] KB sections by kind."""
    _h1("[8] MERCHANT KNOWLEDGE SECTIONS — by kind")
    cur.execute(
        """
        SELECT kind,
               COUNT(*) AS n,
               COUNT(*) FILTER (WHERE is_active) AS n_active,
               COALESCE(SUM(LENGTH(body)), 0) AS body_chars
        FROM merchant_knowledge_sections
        WHERE tenant_id = %s
        GROUP BY kind
        ORDER BY kind
        """,
        (tenant_id,),
    )
    rows = cur.fetchall() or []
    out: dict[str, dict] = {}
    if not rows:
        print("  (no merchant_knowledge_sections rows for this tenant)")
    for r in rows:
        out[r["kind"]] = {
            "n": r["n"], "n_active": r["n_active"], "body_chars": r["body_chars"],
        }
        _kv(
            r["kind"],
            f"total={r['n']} active={r['n_active']} body_chars={r['body_chars']}",
        )
    return out


def section_integrations(cur, tenant_id: int) -> dict:
    """[9] Integrations summary."""
    _h1("[9] INTEGRATIONS")
    cur.execute(
        """
        SELECT provider, enabled, external_store_id, config
        FROM integrations
        WHERE tenant_id = %s
        ORDER BY provider
        """,
        (tenant_id,),
    )
    rows = cur.fetchall() or []
    summary: list[dict] = []
    for r in rows:
        cfg = _safe_get_json(r, "config")
        has_token = bool(cfg.get("refresh_token") or cfg.get("access_token") or cfg.get("api_key"))
        keys_with_values = sorted(k for k, v in cfg.items() if v)
        _kv(
            f"{r['provider']}",
            f"enabled={r['enabled']} ext_id={r['external_store_id']} has_token={has_token}",
        )
        _kv("  config keys with non-empty value", keys_with_values[:20], indent=4)
        summary.append({
            "provider": r["provider"],
            "enabled": r["enabled"],
            "has_token": has_token,
            "config_keys_filled": keys_with_values,
        })
    if not rows:
        print("  (no integrations rows)")
    return {"providers": summary}


def section_red_flags(state: dict) -> list[str]:
    """[10] Auto-detected red flags from the gathered state."""
    _h1("[10] RED FLAGS")
    flags: list[str] = []

    snap = state.get("snapshot") or {}
    store = state.get("store_url") or {}
    maps = state.get("maps") or {}
    payments = state.get("payments") or {}
    media = state.get("media") or {}
    integrations = state.get("integrations") or {"providers": []}

    if not snap.get("exists"):
        flags.append("Snapshot row is MISSING — AI is running without any structured context.")
    elif snap.get("age_hours") and snap["age_hours"] > 24:
        flags.append(
            f"Snapshot is STALE ({snap['age_hours']}h old) — exceeds the 6h freshness gate."
        )

    if store.get("settings") and not store.get("snapshot"):
        flags.append(
            "store_url set in settings BUT missing from snapshot — _rebuild_snapshot not run since edit."
        )
    if not store.get("would_resolve"):
        flags.append(
            "store_url cannot be resolved from ANY source — AI will always fall "
            "back to the clarifying-question line. Merchant must fill one of: "
            "store_settings.store_url, whatsapp_settings.store_button_url, "
            "or connect a Salla/Zid/Shopify/WooCommerce integration."
        )

    # Payment-asset URL hygiene: WhatsApp Cloud API rejects/struggles
    # with plain http:// for media downloads. Note: validate_media_for_send
    # auto-upgrades scheme + canonicalises managed-host hostnames at SEND
    # time when NAHLA_PUBLIC_BASE_URL is set (May 2026 #35), so flagging
    # the DB-row URL is informational rather than blocking.
    by_key = (payments.get("by_key") or {})
    for key, summary in by_key.items():
        first_url = (summary or {}).get("first_url") or ""
        if not first_url:
            continue
        if first_url.startswith("http://"):
            flags.append(
                f"DB has http:// for {key} ({first_url[:80]}) — auto-upgraded "
                "to https:// at send time. Consider re-uploading or running "
                "a backfill so the DB row reflects reality."
            )
        if "railway.app" in first_url and "api.nahlah.ai" not in first_url:
            flags.append(
                f"DB has raw Railway preview host for {key} ({first_url[:80]}) — "
                "auto-canonicalised to NAHLA_PUBLIC_BASE_URL at send time when "
                "the env var is configured."
            )

    if maps.get("settings_maps"):
        flags.append(
            "google_maps_location is set in settings BUT cannot be delivered — "
            "no snapshot mirror, no maps-URL resolver, no safety net. **DESIGN GAP.**"
        )

    if not state.get("staff", {}).get("owner_whatsapp_number") and not state.get("staff", {}).get("distinct_phones_in_freetext"):
        flags.append(
            "No staff/owner phones found ANYWHERE — AI cannot fulfil any 'أبي أكلم …' request."
        )
    elif not state.get("staff", {}).get("would_resolve_by_name"):
        flags.append(
            "Staff phones exist as free text but there's no structured directory — "
            "AI cannot deterministically pick the right person. **DESIGN GAP.**"
        )

    if not payments.get("has_any_payment_asset"):
        flags.append(
            "No active payment media keys — AI cannot send Rajhi/STC Pay/IBAN barcodes."
        )

    by_key = (payments.get("by_key") or {})
    if payments.get("unkeyed_count", 0) > 0 and any(v["active"] == 0 for v in by_key.values()):
        flags.append(
            f"{payments['unkeyed_count']} unkeyed media items exist while some registered "
            "payment keys have zero active rows — run the auto-link / backfill."
        )

    for ig in integrations.get("providers", []):
        if ig.get("enabled") and not ig.get("has_token"):
            flags.append(
                f"Integration '{ig['provider']}' is enabled but has NO token "
                "(api_key/access_token/refresh_token) — orphaned, will silently fail."
            )

    if not flags:
        print("  ✓ No automatic red flags detected.")
    else:
        for i, f in enumerate(flags, 1):
            print(f"  {i}. {f}")
    return flags


def section_canonical_asks(state: dict) -> dict:
    """[11] Canonical asks — what would the AI return RIGHT NOW?"""
    _h1("[11] CANONICAL ASK SIMULATION")

    store = state.get("store_url") or {}
    maps = state.get("maps") or {}
    payments = state.get("payments") or {}
    staff = state.get("staff") or {}

    sims = []

    def _sim(ask: str, would_resolve: bool, evidence: str, design_gap: bool = False) -> None:
        print()
        _kv("ask", ask)
        _kv("would resolve?", would_resolve)
        if design_gap:
            _kv("  note", "✗ DESIGN GAP — requires schema/resolver work")
        _kv("evidence", evidence)
        sims.append({
            "ask": ask, "would_resolve": would_resolve,
            "evidence": evidence, "design_gap": design_gap,
        })

    _sim(
        "ابي رابط المتجر",
        bool(store.get("would_resolve")),
        f"source={store.get('source')} url={_preview(store.get('final_resolved'), limit=80)}",
    )
    _sim(
        "وين موقعكم على الخرايط",
        False,
        f"settings.google_maps_location={_preview(maps.get('settings_maps'), limit=80)} — but no resolver wired",
        design_gap=True,
    )
    _sim(
        "ابي أكلم أمين / المحاسب",
        False,
        f"owner_whatsapp_number={_preview(staff.get('owner_whatsapp_number'), limit=40)} — "
        f"no name→phone mapping",
        design_gap=True,
    )
    _sim(
        "ابي باركود الراجحي",
        bool(payments.get("by_key", {}).get("payment_rajhi_barcode", {}).get("active", 0)),
        f"rajhi_active={payments.get('by_key', {}).get('payment_rajhi_barcode', {}).get('active', 0)}",
    )
    _sim(
        "ابي رقم حساب / IBAN",
        bool(payments.get("by_key", {}).get("payment_bank_transfer_image", {}).get("active", 0)),
        f"bank_transfer_active={payments.get('by_key', {}).get('payment_bank_transfer_image', {}).get('active', 0)}",
    )
    _sim(
        "كيف أحول لكم (generic)",
        bool(payments.get("has_any_payment_asset")),
        "any-payment-asset present" if payments.get("has_any_payment_asset") else "none",
    )

    return {"simulations": sims}


def section_summary(state: dict, flags: list[str]) -> None:
    """[12] Summary — pass/fail/design-gap counts."""
    _h1("[12] AUDIT SUMMARY")

    sims = state.get("canonical_asks", {}).get("simulations", [])
    total = len(sims)
    passed = sum(1 for s in sims if s["would_resolve"])
    design_gaps = sum(1 for s in sims if s["design_gap"])
    failed_data = sum(1 for s in sims if (not s["would_resolve"] and not s["design_gap"]))

    _kv("canonical asks total", total)
    _kv("would deliver TODAY", passed)
    _kv("blocked by DESIGN GAP", design_gaps)
    _kv("blocked by missing DATA only", failed_data)
    _kv("red flags raised", len(flags))

    if design_gaps == 0 and failed_data == 0:
        print("\n  ✓ This tenant is fully resolvable across all canonical asks.")
    else:
        print()
        print("  Next-action priority:")
        if design_gaps:
            print(f"   ▸ {design_gaps} ask(s) need ARCHITECTURE work (maps resolver, staff schema).")
        if failed_data:
            print(f"   ▸ {failed_data} ask(s) need MERCHANT DATA (settings / media upload).")


# ─── Entry point ───────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a tenant's asset readiness for the AI.")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="text = human-readable to stdout; json = machine-readable to stdout.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: env DATABASE_URL must be set (DSN never accepted on CLI).")
        return 2

    try:
        conn = psycopg2.connect(dsn, connect_timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"CONNECT_FAIL: {exc.__class__.__name__}: {exc}")
        return 3
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    state: dict = {"tenant_id": args.tenant_id, "audit_started_at_utc": _utcnow_iso()}

    # text format always prints headings as it runs; json captures the state
    # dict and dumps it at the end. Both share the same data-gathering path.
    if args.format == "text":
        print(f"audit_started_at_utc : {state['audit_started_at_utc']}")
        print(f"tenant_id            : {args.tenant_id}")

    state["tenant"] = section_tenant(cur, args.tenant_id)
    if not state["tenant"].get("exists"):
        if args.format == "json":
            print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
        return 1

    state["snapshot"] = section_snapshot(cur, args.tenant_id)
    snapshot_profile = state["snapshot"].get("store_profile") or {}
    state["store_url"] = section_store_url(cur, args.tenant_id, snapshot_profile)
    store_settings = state["store_url"].get("_store_settings") or {}
    wa_settings = state["store_url"].get("_wa_settings") or {}
    ai_settings = state["store_url"].get("_ai_settings") or {}
    state["maps"] = section_maps(cur, args.tenant_id, state["tenant"], store_settings)
    state["staff"] = section_staff(cur, args.tenant_id, wa_settings, ai_settings)
    state["payments"] = section_payments(cur, args.tenant_id, store_settings)
    state["media"] = section_media_library(cur, args.tenant_id)
    state["kb_sections"] = section_kb_sections(cur, args.tenant_id)
    state["integrations"] = section_integrations(cur, args.tenant_id)
    flags = section_red_flags(state)
    state["red_flags"] = flags
    state["canonical_asks"] = section_canonical_asks(state)
    section_summary(state, flags)

    # Strip internal-only fields before serialisation.
    if isinstance(state.get("store_url"), dict):
        for k in ("_store_settings", "_wa_settings", "_ai_settings"):
            state["store_url"].pop(k, None)

    if args.format == "json":
        print(json.dumps(state, ensure_ascii=False, indent=2, default=str))

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
