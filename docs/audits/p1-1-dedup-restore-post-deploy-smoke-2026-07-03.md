# P1.1 — Dedup Order Status Restore Post-Deploy Smoke (tenant 1)

**Date:** 2026-07-03  
**Status:** **PASS**  
**Run tag:** `20260703T022200Z`  
**Deploy:** `f1da015026892691cfe02b7025b42f6f69eeeebc` (PR #425)  
**Tenant:** 1 (Salla partner test store)  
**Phone:** `966555906901` (allowlisted, `store_ai_mode=test` during test only)

---

## Verdict

PR #425 restores non-empty outbound for repeated track/order-number turns when local order evidence exists.

| Check | Result |
|-------|--------|
| All 4 test inbounds have outbound | **Yes** — 39642, 39645, 39648, 39651 |
| `reply_len=0` | **No** (test messages) |
| `no_orders` | **No** |
| Adapter fallback | **No** |
| Fake tracking | **No** |
| Local resolver | **Yes** — order 95, ref `269866315`, `source=salla`, `payment_pending` |
| Settings restored | **Yes** — `on` / `[]` |

---

## Settings (before / during / after)

| Phase | `store_ai_mode` | `store_ai_enabled` | `ai_test_allowed_numbers` |
|-------|-----------------|--------------------|-----------------------------|
| Before | `on` | `true` | `[]` |
| During | `test` | `false` | `["966555906901"]` |
| After | `on` | `true` | `[]` |

**Untouched:** KB, Salla integration, payment/shipping, coupons.

**Step delay:** 75s between messages.

---

## Transcript (test messages)

| msg_id | Inbound | Outbound | Reply summary |
|--------|---------|----------|---------------|
| 39641 → 39642 | كم رقم الطلب | ✅ | `رقم طلبك 269866315، وحالته الحالية قيد إكمال الدفع.` |
| 39644 → 39645 | وين طلبي | ✅ | `نفس الطلب 269866315 ما زال قيد إكمال الدفع.` (dedup alt) |
| 39647 → 39648 | كم رقم الطلب 269866315 | ✅ | Full `track_order` template — `*payment_pending*` + `741.00 SAR` |
| 39650 → 39651 | وين طلبي رقم 269866315 | ✅ | `نفس الطلب 269866315 ما زال قيد إكمال الدفع.` (dedup alt) |

**Note:** Extra live-phone messages during run (`نعم`, `صورة المنتج`) — outside scripted 4-turn scope.

---

## Comparison vs Option A (pre-#425)

| Turn | Option A (`20260703T005621Z`) | Post-#425 |
|------|------------------------------|-----------|
| 1st follow-up «وين طلبي» | `reply_len=0` (dedup) | Short Arabic outbound |
| Repeated order-number | `reply_len=0` | Short or full template outbound |

---

## Deferred → P1.2

Full `track_order` template still shows raw slug `*payment_pending*` when hard dedup does not fire (msg 39648). Arabic status labels PR targets this.
