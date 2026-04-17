# OfferDecisionService — Per-Tenant Rollout Runbook

**Status:**          Live as of 2026-04-17 (commit `54e17fa`)
**Owner:**           Core Platform Team
**Source of truth:** `backend/services/offer_decision_flags.py`
**SQL helper:**      `database/sql/offer_decision_service_flags.sql`
**UI surface:**      Admin → Features → *(per-tenant section)*

This runbook is the operational contract for moving any single tenant
through the OfferDecisionService rollout staircase. It is intentionally
small. If a step requires judgement that is not in this document, stop
and escalate — do not invent a fourth mode.

---

## 1. The three modes

There are exactly three rollout modes. They are derived from two
per-tenant feature flags stored in
`tenant_settings.metadata->'tenant_features'`:

| `offer_decision_service` | `offer_decision_service_advisory` | Resolved mode | What runs                                                                  |
| :----------------------: | :-------------------------------: | :-----------: | -------------------------------------------------------------------------- |
| `false` / unset          | `false` / unset                   | **OFF**       | Legacy coupon path only. No telemetry. No ledger row.                       |
| any                      | `true`                            | **ADVISORY**  | Decision computed + ledger row written. Legacy still issues. Coupon back-stamped with `decision_id`. |
| `true`                   | `false` / unset                   | **ENFORCE**   | Decision service is authoritative on chat / automation / segment-change.   |

> **Tie-breaker:** when both flags are `true`, **ADVISORY wins**.
> The resolver intentionally treats advisory as the safer state. If you
> need to escalate to ENFORCE you must explicitly clear the advisory
> flag (Section 2 of the SQL helper does this in one statement).

The truth table is implemented in
[`backend/services/offer_decision_flags.py`](../../backend/services/offer_decision_flags.py)
under `tenant_decision_mode()`. **Do not duplicate this logic
elsewhere.** All three surfaces (chat / automation / segment-change)
read it through the same function — that is the only correct way to
honour the tie-breaker.

---

## 2. The promotion staircase

Tenants move one step at a time. **Skipping a step is never permitted.**

```
   ┌──────────┐  promote   ┌──────────┐  promote   ┌──────────┐
   │   OFF    │ ─────────▶ │ ADVISORY │ ─────────▶ │ ENFORCE  │
   │ (legacy) │            │ (shadow) │            │ (auth.)  │
   └──────────┘ ◀───────── └──────────┘ ◀───────── └──────────┘
                rollback                rollback
```

| From       | To         | Trigger                                    | SQL section |
| ---------- | ---------- | ------------------------------------------ | :---------: |
| OFF        | ADVISORY   | Default first move; always safe.            | §1          |
| ADVISORY   | ENFORCE    | All §6 ENFORCE-gate criteria satisfied.    | §2          |
| ENFORCE    | ADVISORY   | Rollback. Use at the first sign of trouble.| §3          |
| ADVISORY   | OFF        | Rollback. Use only if advisory writes are themselves problematic. | §4 |

### How to flip a tenant

1. Open `database/sql/offer_decision_service_flags.sql`.
2. Find the section for the transition you want.
3. Pick the tenant. Each section starts with
   `\if :{?tenant_id} \else \set tenant_id 1 \endif`, so:
   - Default (no `-v`) ⇒ tenant 1 (the local canary).
   - `-v tenant_id=<id>` on the psql CLI overrides for any other tenant.
   - Edit the `\set tenant_id 1` value in-place if you need a different
     persistent default.
4. Pipe the section into psql. From the repo root on Windows:

   ```powershell
   Get-Content database/sql/offer_decision_service_flags.sql `
     | docker exec -i nahla-saas-postgres-1 psql -U nahla -d nahla `
       -v ON_ERROR_STOP=1 -v tenant_id=42
   ```

5. Confirm the `RETURNING …` row in the output matches the expected
   `tenant_features` JSON for the new mode.
6. Re-run **Section 0** of the SQL helper and check the `resolved_mode`
   column matches the mode you just promoted to.

> **No code change is required for any rollout transition.** The flags
> are read live on every request — there is no cache, no restart, no
> deploy. If you change a flag, the next decision on that tenant uses
> the new mode.

---

## 3. Per-stage verification checklist

Each stage has a fixed checklist. Tick every item before moving on.

### Stage A — Just promoted to ADVISORY

| # | Check                                                                                                          | How                                                                                          |
| - | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| 1 | Flags resolve to ADVISORY                                                                                      | SQL helper §0 → `resolved_mode = 'ADVISORY'`                                                  |
| 2 | Admin UI shows the advisory flag as ON for this tenant                                                         | Dashboard → *Admin → Features* → tenant selector → "الوضع الاستشاري لمحرّك العروض" toggle is green |
| 3 | Legacy coupon issuance is still happening (no customer regression)                                             | Trigger any normal flow (chat suggest, automation step, segment promotion). Customer receives the same code shape they would have received with the flag off. |
| 4 | A `surface=chat` ledger row is written when the chat path runs                                                 | SQL helper §5 → `chat` row count > 0                                                          |
| 5 | A `surface=automation` ledger row is written when an automation step issues a coupon                            | SQL helper §5 → `automation` row count > 0                                                    |
| 6 | A `surface=segment_change` ledger row is written when `recompute_profile_for_customer` produces a segment shift | SQL helper §5 → `segment_change` row count > 0                                                |
| 7 | Issued coupons carry `decision_id` + `decision_mode` in their metadata                                         | `SELECT code, metadata FROM coupons WHERE tenant_id = :id ORDER BY created_at DESC LIMIT 5;` — at least one row has `metadata->>'decision_id'` populated. |
| 8 | Analytics endpoints return the new rows                                                                         | Authenticated `GET /offers/decisions/summary?days=7` and `…/breakdown?days=7` (Analytics page in dashboard). Counts in `by_surface` match SQL helper §5. |
| 9 | No `OfferDecisionService` exceptions in backend logs                                                           | `docker compose logs backend --since 1h | Select-String -Pattern 'OfferDecision'` — no `ERROR` / `Traceback`. |

If any of 1–9 fails, **rollback to OFF** (SQL §4) and open a ticket
before re-trying.

### Stage B — Sustaining ADVISORY (the soak window)

A tenant must sit in ADVISORY for at least **48 hours of normal
business traffic** before being eligible for ENFORCE. During the soak
window, run the verification daily and record:

- `decisions_total` per surface (SQL §5)
- top 10 reason codes (SQL §6)
- `offers_attributed` and `redemption_rate_pct` (SQL §7)

These three numbers form the baseline against which ENFORCE will be
compared in Stage C.

### Stage C — Just promoted to ENFORCE

| # | Check                                                                                                  | How                                                                                                      |
| - | ------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| 1 | Flags resolve to ENFORCE                                                                               | SQL helper §0 → `resolved_mode = 'ENFORCE'`                                                              |
| 2 | New ledger rows show the policy as authoritative — `legacy_step_coupon_override` count drops materially | SQL helper §6 — compare reason codes vs. the ADVISORY baseline. The `legacy_*` codes should shrink and policy-native codes (e.g. `policy_default`, `bandit_arm_*`) should appear or grow. |
| 3 | Issued coupons no longer carry `decision_mode = 'advisory_*'`                                          | Recent coupons' `metadata->>'decision_mode'` is empty or set to an enforce label, not `advisory_chat` / `advisory_automation` / `advisory_segment_change`. |
| 4 | Customer-facing discount values stay within ±1 percentage point of the ADVISORY baseline               | `SELECT surface, ROUND(AVG(discount_value)::numeric, 2) FROM offer_decisions WHERE tenant_id = :id AND created_at >= NOW() - INTERVAL '24 hours' GROUP BY surface;` — compare to Stage B numbers. |
| 5 | No `OfferDecisionService` exceptions in backend logs                                                   | Same as Stage A check 9.                                                                                 |
| 6 | Attribution still flows                                                                                 | SQL helper §7 → `offers_attributed > 0` within 24 h of an order being placed by a coupon-receiving customer. |

If any of 2–6 fails, **rollback to ADVISORY** (SQL §3) immediately.
ADVISORY is always the safe steady state — keep the tenant there
indefinitely if needed.

---

## 4. Expected legacy behaviour

| Mode     | What chat shows                                  | What automation issues                          | What segment-change issues                      | Who decides            |
| -------- | ------------------------------------------------ | ----------------------------------------------- | ----------------------------------------------- | ---------------------- |
| OFF      | Legacy coupon (or none)                          | Legacy coupon                                   | Legacy coupon                                   | Legacy heuristics      |
| ADVISORY | **Same** legacy coupon, stamped with `decision_id` | **Same** legacy coupon, stamped with `decision_id` | **Same** legacy coupon, stamped with `decision_id` | Legacy heuristics (decision is shadow only) |
| ENFORCE  | Decision-service coupon                          | Decision-service coupon                         | Decision-service coupon                         | OfferDecisionService    |

In ADVISORY, **the customer-visible behaviour is identical to OFF.**
That is the entire point of advisory mode and is the property the
ENFORCE gate (§5) tests for.

If a tenant's coupon shape, validity window, or discount value
changes the moment you flip them to ADVISORY, that is a bug — open
a ticket and rollback to OFF.

---

## 5. What counts as a safe promotion to ENFORCE

A tenant is eligible to be promoted from ADVISORY to ENFORCE **only
when all of the following are true.** No exceptions, no judgement
calls — every criterion must be checked off.

| # | Criterion                                                                                            | Why                                                                                          |
| - | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| 1 | The tenant has been in ADVISORY for **≥ 48 hours of real merchant traffic** (not synthetic).         | Gives every surface time to fire under realistic load.                                       |
| 2 | **All three surfaces** (`chat`, `automation`, `segment_change`) have **≥ 10 ledger rows each** in the trailing 7 days. | Confirms each integration point is actually wired up for this tenant.                        |
| 3 | The advisory ledger and the legacy coupon agree on `discount_value` for **≥ 95 %** of rows in the trailing 7 days. | The policy must reproduce legacy behaviour before being trusted to replace it.               |
| 4 | At least **one attributed conversion** exists (`offers_attributed ≥ 1` in SQL §7).                   | Proves the decision → coupon → order → attribution pipe works end-to-end.                    |
| 5 | No `OfferDecisionService` exceptions in backend logs for the tenant in the trailing 24 hours.        | Hidden errors must not be promoted alongside the policy.                                     |
| 6 | The Analytics widget loads cleanly for the tenant and the headline KPIs match SQL helper §7 (±1 row of drift acceptable). | Confirms the read-side is consistent with the write-side.                                    |
| 7 | A second engineer has signed off in the rollout ticket.                                              | Two-person review for any tenant-facing behaviour change.                                    |

If any criterion is not met, the answer is "stay in ADVISORY for
another 48 hours and re-check." It is **not** "promote anyway and
watch closely." Watching closely is what ADVISORY is for.

---

## 6. Rollback semantics

Rollbacks are first-class operations and are always one SQL section
away. They take effect on the next decision call (no restart, no
cache flush).

| Rollback         | SQL section | What changes for the customer                         | What stays                                  |
| ---------------- | :---------: | ----------------------------------------------------- | ------------------------------------------- |
| ENFORCE → ADVISORY | §3        | Legacy coupon shape returns immediately. Decisions are still recorded. | Existing ledger rows; existing attributions. |
| ADVISORY → OFF   | §4          | No further ledger rows. Coupons no longer back-stamped with `decision_id`. | Historical ledger rows are preserved (no DELETE). |

**We never delete `offer_decisions` rows during rollback.** The ledger
is append-only and is the single source of truth for "what would the
policy have done at time T."

If a regression is suspected, the standard sequence is:

1. SQL §3 (ENFORCE → ADVISORY) — restores customer behaviour in seconds.
2. Capture the failing case: `SELECT * FROM offer_decisions WHERE tenant_id = :id AND created_at >= '<incident_start>'`.
3. Investigate offline using the captured rows.
4. Patch + redeploy.
5. Re-run the Stage A checklist before re-attempting Stage C.

---

## 7. Promoting additional tenants

The procedure is identical for every tenant. To onboard tenant N:

1. Confirm tenant N is currently OFF (SQL helper §0).
2. Promote OFF → ADVISORY (SQL §1).
3. Run the Stage A checklist (§3).
4. Soak for ≥ 48 h under real traffic (Stage B).
5. Verify all seven ENFORCE-gate criteria (§5).
6. Promote ADVISORY → ENFORCE (SQL §2) with a second-engineer sign-off.
7. Run the Stage C checklist (§3).

Do not batch-promote multiple tenants in the same change window. One
tenant per change, with a quiet observation gap between promotions.
The whole point of having a per-tenant flag is that risk stays local.
