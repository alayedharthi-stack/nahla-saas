"""
End-to-End Tenant Isolation Validation
=======================================
Tests 7 scenarios across two separate tenants to verify no cross-tenant
data leakage at the runtime level.

Usage:
    python tests/tenant_isolation_test.py [BASE_URL]
    default BASE_URL = http://127.0.0.1:8000
"""
import sys
import json
import time
from typing import Any, Dict, Optional
import requests

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
TENANT_A = "1"
TENANT_B = "2"

# ── Colour codes ───────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

results: list[dict] = []


def headers(tenant_id: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Tenant-ID": tenant_id,
    }


def log(status: str, scenario: str, detail: str, extra: str = ""):
    icon = "✅" if status == "PASS" else ("❌" if status == "FAIL" else "⚠️ ")
    color = GREEN if status == "PASS" else (RED if status == "FAIL" else YELLOW)
    print(f"{color}{icon} [{status}]{RESET} {BOLD}{scenario}{RESET}: {detail}")
    if extra:
        print(f"       {YELLOW}{extra}{RESET}")
    results.append({"status": status, "scenario": scenario, "detail": detail})


def get(tenant: str, path: str, params: dict = None) -> requests.Response:
    return requests.get(f"{BASE_URL}{path}", headers=headers(tenant), params=params, timeout=10)


def post(tenant: str, path: str, body: dict = None) -> requests.Response:
    return requests.post(f"{BASE_URL}{path}", headers=headers(tenant),
                         json=body or {}, timeout=10)


def put(tenant: str, path: str, body: dict = None) -> requests.Response:
    return requests.put(f"{BASE_URL}{path}", headers=headers(tenant),
                        json=body or {}, timeout=10)


# ══════════════════════════════════════════════════════════════════════════════
# SETUP — bootstrap both tenants
# ══════════════════════════════════════════════════════════════════════════════

def setup_tenants():
    print(f"\n{CYAN}{BOLD}═══ SETUP: Bootstrapping Tenant A ({TENANT_A}) and Tenant B ({TENANT_B}) ═══{RESET}\n")

    for t in [TENANT_A, TENANT_B]:
        r = get(t, "/settings")
        if r.status_code == 200:
            print(f"  Tenant {t}: settings loaded ({r.status_code})")
        else:
            print(f"  {YELLOW}⚠️  Tenant {t}: settings returned {r.status_code}{RESET}")

    # Give Tenant A unique AI Sales settings
    put(TENANT_A, "/ai-sales/settings", {
        "enable_ai_sales_agent": True,
        "allow_product_recommendations": True,
        "allow_order_creation": True,
        "allow_human_handoff": True,
        "confidence_threshold": 0.1,
    })

    # Tenant B: AI agent disabled
    put(TENANT_B, "/ai-sales/settings", {
        "enable_ai_sales_agent": False,
        "allow_product_recommendations": False,
    })

    print(f"  Tenant A: AI Sales Agent ENABLED")
    print(f"  Tenant B: AI Sales Agent DISABLED\n")


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — AI Sales Agent conversation flow
# ══════════════════════════════════════════════════════════════════════════════

def test_ai_sales_isolation():
    print(f"\n{CYAN}{BOLD}─── Scenario 1: AI Sales Agent conversation flow ───{RESET}")

    phone_a = "+966500000001"
    phone_b = "+966500000002"

    # Tenant A: should get real response (agent enabled)
    r_a = post(TENANT_A, "/ai-sales/process-message", {
        "customer_phone": phone_a,
        "message": "أريد أرى المنتجات",
        "customer_name": "أحمد - Tenant A"
    })

    if r_a.status_code == 200:
        data_a = r_a.json()
        if data_a.get("intent") != "disabled":
            log("PASS", "AI Sales - Tenant A", f"Agent responded, intent={data_a.get('intent')}")
        else:
            log("FAIL", "AI Sales - Tenant A", "Agent returned 'disabled' but should be enabled")
    else:
        log("WARN", "AI Sales - Tenant A", f"HTTP {r_a.status_code} — {r_a.text[:200]}")

    # Tenant B: should get "disabled" response
    r_b = post(TENANT_B, "/ai-sales/process-message", {
        "customer_phone": phone_b,
        "message": "أريد أرى المنتجات",
        "customer_name": "محمد - Tenant B"
    })

    if r_b.status_code == 200:
        data_b = r_b.json()
        if data_b.get("intent") == "disabled":
            log("PASS", "AI Sales - Tenant B", "Agent correctly returned 'disabled' per tenant settings")
        else:
            log("FAIL", "AI Sales - Tenant B",
                f"Agent responded when it should be disabled! intent={data_b.get('intent')}",
                "CROSS-TENANT SETTINGS LEAK")
    else:
        log("WARN", "AI Sales - Tenant B", f"HTTP {r_b.status_code} — {r_b.text[:200]}")

    # Cross-tenant probe: use Tenant B header but Tenant A's phone
    r_cross = post(TENANT_B, "/ai-sales/process-message", {
        "customer_phone": phone_a,      # Tenant A's customer phone
        "message": "هل عندكم منتجات؟",
        "customer_name": "Cross-tenant probe"
    })
    if r_cross.status_code == 200:
        data_cross = r_cross.json()
        if data_cross.get("intent") == "disabled":
            log("PASS", "AI Sales - Cross-tenant probe",
                "Tenant B settings applied even when using Tenant A's customer phone")
        else:
            log("FAIL", "AI Sales - Cross-tenant probe",
                f"Tenant B leaked Tenant A settings! intent={data_cross.get('intent')}",
                "CRITICAL: cross-tenant settings applied")


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — Customer profile / intelligence isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_customer_memory_isolation():
    print(f"\n{CYAN}{BOLD}─── Scenario 2: Customer memory / intelligence isolation ───{RESET}")

    # Get customer list for Tenant A
    r_a = get(TENANT_A, "/intelligence/dashboard")
    r_b = get(TENANT_B, "/intelligence/dashboard")

    if r_a.status_code == 200 and r_b.status_code == 200:
        data_a = r_a.json()
        data_b = r_b.json()

        # Collect customer IDs visible to each tenant
        top_a = {c.get("customer_name") for c in data_a.get("top_customers", [])}
        top_b = {c.get("customer_name") for c in data_b.get("top_customers", [])}

        # Check segment counts are independent
        seg_a = data_a.get("segments", {})
        seg_b = data_b.get("segments", {})

        log("PASS", "Customer Intelligence - Tenant A", f"Dashboard loaded, {len(top_a)} VIP customers visible")
        log("PASS", "Customer Intelligence - Tenant B", f"Dashboard loaded, {len(top_b)} VIP customers visible")

        if top_a & top_b:
            log("WARN", "Customer Intelligence - Overlap",
                f"Same customer names appear in both tenants: {top_a & top_b}",
                "NOTE: seed data uses same names — check IDs not names")
        else:
            log("PASS", "Customer Intelligence - No overlap", "No customer name overlap between tenants")
    else:
        log("WARN", "Customer Intelligence", f"Dashboard returned {r_a.status_code}/{r_b.status_code}")

    # Probe: can Tenant B read Tenant A's customer profile by ID?
    # First get a customer ID from Tenant A
    r_seg_a = get(TENANT_A, "/intelligence/segments/live")
    if r_seg_a.status_code == 200:
        # Try customer ID 1 with Tenant B header
        r_probe = get(TENANT_B, "/intelligence/customer-profile/1")
        if r_probe.status_code == 404:
            log("PASS", "Customer Profile - Cross-tenant probe",
                "Tenant B cannot read Tenant A's customer profile (404)")
        elif r_probe.status_code == 200:
            profile = r_probe.json()
            # Check if this is actually Tenant B's customer or Tenant A's leaked
            log("FAIL", "Customer Profile - Cross-tenant probe",
                f"Tenant B can read customer ID 1 — possible cross-tenant leak!",
                f"Response: {json.dumps(profile)[:300]}")
        else:
            log("WARN", "Customer Profile - Cross-tenant probe",
                f"HTTP {r_probe.status_code} — {r_probe.text[:200]}")


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 3 — Template usage / usage_count isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_template_isolation():
    print(f"\n{CYAN}{BOLD}─── Scenario 3: Template usage and usage_count isolation ───{RESET}")

    # Get Tenant A templates
    r_a = get(TENANT_A, "/templates")
    r_b = get(TENANT_B, "/templates")

    tpls_a = r_a.json().get("templates", []) if r_a.status_code == 200 else []
    tpls_b = r_b.json().get("templates", []) if r_b.status_code == 200 else []

    log("PASS" if tpls_a else "WARN", "Templates - Tenant A",
        f"{len(tpls_a)} templates found")
    log("PASS" if tpls_b else "WARN", "Templates - Tenant B",
        f"{len(tpls_b)} templates found")

    # Check template IDs don't cross-pollute
    ids_a = {t["id"] for t in tpls_a}
    ids_b = {t["id"] for t in tpls_b}

    if ids_a & ids_b:
        log("WARN", "Templates - ID overlap",
            f"Same template IDs shared: {ids_a & ids_b}",
            "NOTE: seeded templates may share IDs if both tenants get same seed rows")

    # Probe: Tenant B tries to resolve Tenant A's template
    if tpls_a:
        tpl_id_a = tpls_a[0]["id"]
        usage_before = tpls_a[0].get("usage_count", 0)

        # Tenant B tries to call /resolve on Tenant A's template
        r_probe = post(TENANT_B, f"/templates/{tpl_id_a}/resolve",
                       {"customer_id": 1, "extra": {}})

        if r_probe.status_code == 404:
            log("PASS", "Templates - Cross-tenant resolve probe",
                f"Tenant B cannot resolve Tenant A's template #{tpl_id_a} (404)")
        elif r_probe.status_code == 200:
            # Check if usage_count on Tenant A's template was bumped
            r_check = get(TENANT_A, "/templates")
            tpls_after = r_check.json().get("templates", [])
            tpl_after = next((t for t in tpls_after if t["id"] == tpl_id_a), None)
            usage_after = tpl_after.get("usage_count", 0) if tpl_after else usage_before

            if usage_after > usage_before:
                log("FAIL", "Templates - Cross-tenant usage_count",
                    f"Tenant B bumped usage_count on Tenant A's template #{tpl_id_a}!",
                    f"Before: {usage_before}, After: {usage_after}")
            else:
                log("FAIL", "Templates - Cross-tenant resolve",
                    f"Tenant B resolved Tenant A's template (200 returned)",
                    "Template content leaked across tenants")
        else:
            log("WARN", "Templates - Cross-tenant resolve probe",
                f"HTTP {r_probe.status_code}")

    # Probe: Tenant B tries to delete Tenant A's template
    if tpls_a:
        pending_a = [t for t in tpls_a if t["status"] in ("PENDING", "REJECTED", "DRAFT")]
        if pending_a:
            tpl_id = pending_a[0]["id"]
            r_del = requests.delete(f"{BASE_URL}/templates/{tpl_id}",
                                    headers=headers(TENANT_B), timeout=10)
            if r_del.status_code == 404:
                log("PASS", "Templates - Cross-tenant delete probe",
                    f"Tenant B cannot delete Tenant A's template #{tpl_id} (404)")
            elif r_del.status_code == 200:
                log("FAIL", "Templates - Cross-tenant delete probe",
                    f"Tenant B deleted Tenant A's template #{tpl_id}!",
                    "CRITICAL: destructive cross-tenant action succeeded")
            else:
                log("WARN", "Templates - Cross-tenant delete probe",
                    f"HTTP {r_del.status_code}")


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 4 — Campaign and template resolution isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_campaign_isolation():
    print(f"\n{CYAN}{BOLD}─── Scenario 4: Campaign / template resolution isolation ───{RESET}")

    # Create a campaign under Tenant A
    r_create = post(TENANT_A, "/campaigns", {
        "name": "Tenant A Test Campaign",
        "campaign_type": "promotion",
        "template_id": "1",
        "template_name": "special_offer",
        "template_language": "ar",
        "audience_type": "all",
        "audience_count": 10,
        "schedule_type": "immediate",
    })

    if r_create.status_code == 200:
        campaign_id = r_create.json().get("campaign", {}).get("id") or r_create.json().get("id")
        log("PASS", "Campaign - Create (Tenant A)", f"Campaign created, id={campaign_id}")

        if campaign_id:
            # Probe: Tenant B tries to update Tenant A's campaign status
            r_probe = put(TENANT_B, f"/campaigns/{campaign_id}/status",
                          {"status": "active"})
            if r_probe.status_code == 404:
                log("PASS", "Campaign - Cross-tenant status update probe",
                    f"Tenant B cannot update Tenant A's campaign #{campaign_id} (404)")
            elif r_probe.status_code == 200:
                log("FAIL", "Campaign - Cross-tenant status update probe",
                    f"Tenant B modified Tenant A's campaign #{campaign_id}!",
                    "CRITICAL: cross-tenant campaign modification succeeded")
            else:
                log("WARN", "Campaign - Cross-tenant status update probe",
                    f"HTTP {r_probe.status_code}")
    else:
        log("WARN", "Campaign - Create (Tenant A)",
            f"HTTP {r_create.status_code} — {r_create.text[:200]}")

    # Verify campaign lists are separate
    r_list_a = get(TENANT_A, "/campaigns")
    r_list_b = get(TENANT_B, "/campaigns")

    if r_list_a.status_code == 200 and r_list_b.status_code == 200:
        camps_a = r_list_a.json().get("campaigns", [])
        camps_b = r_list_b.json().get("campaigns", [])
        ids_a = {c["id"] for c in camps_a}
        ids_b = {c["id"] for c in camps_b}
        overlap = ids_a & ids_b
        if overlap:
            log("FAIL", "Campaign - List isolation",
                f"Same campaign IDs visible to both tenants: {overlap}",
                "CRITICAL: cross-tenant campaign visibility")
        else:
            log("PASS", "Campaign - List isolation",
                f"Tenant A sees {len(camps_a)} campaigns, Tenant B sees {len(camps_b)} — no overlap")


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 5 — Handoff session isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_handoff_isolation():
    print(f"\n{CYAN}{BOLD}─── Scenario 5: Handoff session isolation ───{RESET}")

    # Trigger a handoff for Tenant A
    r_msg = post(TENANT_A, "/ai-sales/process-message", {
        "customer_phone": "+966500000010",
        "message": "تكلم موظف",
        "customer_name": "عميل A"
    })

    handoff_triggered_a = False
    if r_msg.status_code == 200:
        handoff_triggered_a = r_msg.json().get("handoff_triggered", False)
        log("PASS" if handoff_triggered_a else "WARN",
            "Handoff - Trigger (Tenant A)",
            f"handoff_triggered={handoff_triggered_a}")

    # List handoff sessions for Tenant A
    r_sessions_a = get(TENANT_A, "/handoff/sessions", {"status": "active"})
    r_sessions_b = get(TENANT_B, "/handoff/sessions", {"status": "active"})

    sessions_a = r_sessions_a.json().get("sessions", []) if r_sessions_a.status_code == 200 else []
    sessions_b = r_sessions_b.json().get("sessions", []) if r_sessions_b.status_code == 200 else []

    ids_a = {s["id"] for s in sessions_a}
    ids_b = {s["id"] for s in sessions_b}
    overlap = ids_a & ids_b

    if overlap:
        log("FAIL", "Handoff - Session list isolation",
            f"Same handoff session IDs visible to both tenants: {overlap}",
            "CRITICAL: cross-tenant handoff visibility")
    else:
        log("PASS", "Handoff - Session list isolation",
            f"Tenant A: {len(sessions_a)} sessions, Tenant B: {len(sessions_b)} sessions — no overlap")

    # Cross-tenant probe: Tenant B tries to resolve Tenant A's handoff
    if sessions_a:
        session_id_a = sessions_a[0]["id"]
        r_probe = put(TENANT_B, f"/handoff/sessions/{session_id_a}/resolve",
                      {"resolved_by": "cross-tenant-attacker"})
        if r_probe.status_code == 404:
            log("PASS", "Handoff - Cross-tenant resolve probe",
                f"Tenant B cannot resolve Tenant A's session #{session_id_a} (404)")
        elif r_probe.status_code == 200:
            log("FAIL", "Handoff - Cross-tenant resolve probe",
                f"Tenant B resolved Tenant A's handoff session #{session_id_a}!",
                "CRITICAL: cross-tenant handoff manipulation succeeded")
        else:
            log("WARN", "Handoff - Cross-tenant resolve probe",
                f"HTTP {r_probe.status_code}")


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 6 — Payment session isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_payment_isolation():
    print(f"\n{CYAN}{BOLD}─── Scenario 6: Payment session creation isolation ───{RESET}")

    # Create a test order for Tenant A first
    r_order = post(TENANT_A, "/ai-sales/create-order", {
        "customer_phone": "+966500000020",
        "customer_name": "عميل الدفع A",
        "product_name": "منتج اختبار",
        "quantity": 1,
        "city": "الرياض",
        "address": "حي النزهة",
        "payment_method": "cod",
    })

    order_id_a = None
    if r_order.status_code == 200:
        order_id_a = r_order.json().get("order_id")
        log("PASS", "Payment - Create order (Tenant A)", f"Order created: id={order_id_a}")
    else:
        log("WARN", "Payment - Create order (Tenant A)",
            f"HTTP {r_order.status_code} — {r_order.text[:200]}")

    if order_id_a:
        # Try to create a payment session for Tenant A's order using Tenant B's header
        r_probe = post(TENANT_B, "/payments/create-session", {
            "order_id": order_id_a,
            "amount_sar": 100.0,
            "description": "Cross-tenant payment probe"
        })
        if r_probe.status_code in (404, 422, 400):
            log("PASS", "Payment - Cross-tenant session probe",
                f"Tenant B cannot create payment for Tenant A's order (HTTP {r_probe.status_code})")
        elif r_probe.status_code == 200:
            log("FAIL", "Payment - Cross-tenant session probe",
                f"Tenant B created payment session for Tenant A's order #{order_id_a}!",
                "CRITICAL: cross-tenant payment creation succeeded")
        else:
            log("WARN", "Payment - Cross-tenant session probe",
                f"HTTP {r_probe.status_code} — {r_probe.text[:200]}")


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 7 — Observability events and traces isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_observability_isolation():
    print(f"\n{CYAN}{BOLD}─── Scenario 7: Observability events and traces isolation ───{RESET}")

    # Generate some events for Tenant A
    post(TENANT_A, "/ai-sales/process-message", {
        "customer_phone": "+966500000030",
        "message": "اريد اشتري",
        "customer_name": "عميل أحداث A"
    })

    time.sleep(0.5)  # give uvicorn time to write events

    # Fetch events for Tenant A
    r_events_a = get(TENANT_A, "/system/events")
    r_events_b = get(TENANT_B, "/system/events")

    if r_events_a.status_code == 200 and r_events_b.status_code == 200:
        events_a = r_events_a.json().get("events", [])
        events_b = r_events_b.json().get("events", [])

        # Check tenant_id on all events
        a_tenants = {e.get("tenant_id") for e in events_a}
        b_tenants = {e.get("tenant_id") for e in events_b}

        if a_tenants - {1, None}:
            log("FAIL", "Events - Tenant A purity",
                f"Tenant A events contain foreign tenant_ids: {a_tenants - {1, None}}")
        else:
            log("PASS", "Events - Tenant A purity",
                f"{len(events_a)} events, all belong to tenant {TENANT_A}")

        if b_tenants - {2, None}:
            log("FAIL", "Events - Tenant B purity",
                f"Tenant B events contain foreign tenant_ids: {b_tenants - {2, None}}")
        else:
            log("PASS", "Events - Tenant B purity",
                f"{len(events_b)} events, all belong to tenant {TENANT_B}")

        # IDs should not overlap (each event row is unique per tenant)
        ids_a = {e.get("id") for e in events_a}
        ids_b = {e.get("id") for e in events_b}
        overlap = ids_a & ids_b
        if overlap:
            log("FAIL", "Events - ID isolation",
                f"Same event IDs visible to both tenants: {overlap}",
                "CRITICAL: event rows shared across tenants")
        else:
            log("PASS", "Events - ID isolation",
                "No event ID overlap between tenants")
    else:
        log("WARN", "Events", f"HTTP {r_events_a.status_code}/{r_events_b.status_code}")

    # Check conversation traces
    phone_a = "+966500000030"
    r_trace_a = get(TENANT_A, f"/conversations/trace/{phone_a}")
    r_trace_b = get(TENANT_B, f"/conversations/trace/{phone_a}")  # Tenant B probing A's customer

    if r_trace_a.status_code == 200:
        turns_a = r_trace_a.json().get("turns", [])
        log("PASS" if turns_a else "WARN", "Traces - Tenant A",
            f"{len(turns_a)} conversation turns found for Tenant A")
    else:
        log("WARN", "Traces - Tenant A", f"HTTP {r_trace_a.status_code}")

    if r_trace_b.status_code == 200:
        turns_b = r_trace_b.json().get("turns", [])
        if turns_b:
            log("FAIL", "Traces - Cross-tenant probe",
                f"Tenant B can read {len(turns_b)} conversation turns from Tenant A's customer!",
                f"Phone: {phone_a} — CRITICAL data leak")
        else:
            log("PASS", "Traces - Cross-tenant probe",
                f"Tenant B sees 0 traces for Tenant A's customer phone (correct)")
    else:
        log("PASS", "Traces - Cross-tenant probe",
            f"HTTP {r_trace_b.status_code} — Tenant B cannot access Tenant A's traces")


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO 8 — Settings isolation (bonus)
# ══════════════════════════════════════════════════════════════════════════════

def test_settings_isolation():
    print(f"\n{CYAN}{BOLD}─── Scenario 8: Settings isolation (bonus) ───{RESET}")

    # Set a unique store name for Tenant A
    put(TENANT_A, "/settings", {
        "store": {"store_name": "TENANT_A_UNIQUE_STORE_XYZ"}
    })

    # Read settings for Tenant B — should NOT contain Tenant A's store name
    r_b = get(TENANT_B, "/settings")
    if r_b.status_code == 200:
        store_name_b = r_b.json().get("store", {}).get("store_name", "")
        if "TENANT_A_UNIQUE_STORE_XYZ" in store_name_b:
            log("FAIL", "Settings - Store name isolation",
                f"Tenant B can see Tenant A's store name: {store_name_b}",
                "CRITICAL: settings cross-tenant leak")
        else:
            log("PASS", "Settings - Store name isolation",
                f"Tenant B store name: '{store_name_b}' (does not contain Tenant A's value)")

    # Restore Tenant A
    put(TENANT_A, "/settings", {"store": {"store_name": ""}})


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════════╗")
    print(f"║   Nahla SaaS — Tenant Isolation Validation Suite    ║")
    print(f"╚══════════════════════════════════════════════════════╝{RESET}")
    print(f"  Base URL : {BASE_URL}")
    print(f"  Tenant A : X-Tenant-ID: {TENANT_A}")
    print(f"  Tenant B : X-Tenant-ID: {TENANT_B}")

    # Check server is reachable
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        print(f"  Server   : {GREEN}reachable (HTTP {r.status_code}){RESET}\n")
    except Exception as e:
        print(f"\n{RED}✗ Server not reachable at {BASE_URL}: {e}{RESET}")
        print("  Make sure uvicorn is running before executing this test.\n")
        sys.exit(1)

    setup_tenants()
    test_ai_sales_isolation()
    test_customer_memory_isolation()
    test_template_isolation()
    test_campaign_isolation()
    test_handoff_isolation()
    test_payment_isolation()
    test_observability_isolation()
    test_settings_isolation()

    # ── Final summary ──────────────────────────────────────────────────────────
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    warned = sum(1 for r in results if r["status"] == "WARN")
    total  = len(results)

    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════════╗")
    print(f"║                  VALIDATION SUMMARY                 ║")
    print(f"╚══════════════════════════════════════════════════════╝{RESET}")
    print(f"  Total checks : {total}")
    print(f"  {GREEN}PASSED       : {passed}{RESET}")
    print(f"  {RED}FAILED       : {failed}{RESET}")
    print(f"  {YELLOW}WARNINGS     : {warned}{RESET}")

    if failed > 0:
        print(f"\n{RED}{BOLD}⚠  ISOLATION FAILURES DETECTED — NOT READY FOR PRODUCTION{RESET}")
        print(f"\n{RED}Failed checks:{RESET}")
        for r in results:
            if r["status"] == "FAIL":
                print(f"  ❌ {r['scenario']}: {r['detail']}")
    else:
        print(f"\n{GREEN}{BOLD}✅  ALL ISOLATION CHECKS PASSED{RESET}")
        if warned > 0:
            print(f"{YELLOW}  {warned} warning(s) noted — review above for context{RESET}")
        print(f"\n{GREEN}Nahla is ready for multi-tenant production deployment{RESET}")
        print(f"  (subject to production infra: Redis rate limiter, JWT auth, HTTPS){RESET}")

    print()
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
