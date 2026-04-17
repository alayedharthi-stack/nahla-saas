-- ════════════════════════════════════════════════════════════════════════════
--  OfferDecisionService — Tenant rollout helper
--  ─────────────────────────────────────────────
--  Owner          : Core Platform Team
--  Companion doc  : docs/runbooks/offer_decision_service_rollout.md
--  Source of truth: backend/services/offer_decision_flags.py
--                   (DecisionMode + tenant_decision_mode resolver)
--
--  Purpose
--  -------
--  Idempotent psql snippets for moving a single tenant between the three
--  rollout modes (OFF / ADVISORY / ENFORCE) and for verifying that the
--  move actually took effect.
--
--  Mode truth table (must match offer_decision_flags.py):
--
--    service=false + advisory=false  → OFF      (legacy only, no telemetry)
--    service=*     + advisory=true   → ADVISORY (compute + ledger, legacy issues)
--    service=true  + advisory=false  → ENFORCE  (decision service is authoritative)
--
--  Usage
--  -----
--    1. Pick the section you want (one transition per section).
--    2. Set :tenant_id either by:
--         a) editing the `\set tenant_id …` line at the top of the section, OR
--         b) passing `-v tenant_id=<id>` on the psql command line, e.g.:
--              psql -U nahla -d nahla -v tenant_id=42 -f offer_decision_service_flags.sql
--       The CLI -v wins. Each section uses `\if :{?tenant_id}` so the in-file
--       default (1) only fires when no override is supplied.
--    3. Pipe the section into psql, e.g. with `Get-Content` on Windows:
--         Get-Content database/sql/offer_decision_service_flags.sql `
--           | docker exec -i nahla-saas-postgres-1 psql -U nahla -d nahla `
--             -v ON_ERROR_STOP=1 -v tenant_id=42
--
--  Every UPDATE preserves all other keys in tenant_settings.metadata and in
--  metadata->'tenant_features'. The merge is a single atomic statement; no
--  read-modify-write race window.
-- ════════════════════════════════════════════════════════════════════════════


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 0 — Read current state (always run this first)
-- ════════════════════════════════════════════════════════════════════════════
-- Shows the raw flag pair and the resolved mode label. Run before AND after
-- any transition. The resolved mode column is computed inline so it stays in
-- sync with the truth table above without depending on backend code.

\if :{?tenant_id} \else \set tenant_id 1 \endif

SELECT
  ts.tenant_id,
  t.name                                                       AS tenant_name,
  COALESCE((ts.metadata->'tenant_features'->>'offer_decision_service')::bool,          false) AS service_enforce,
  COALESCE((ts.metadata->'tenant_features'->>'offer_decision_service_advisory')::bool, false) AS service_advisory,
  CASE
    WHEN COALESCE((ts.metadata->'tenant_features'->>'offer_decision_service_advisory')::bool, false) THEN 'ADVISORY'
    WHEN COALESCE((ts.metadata->'tenant_features'->>'offer_decision_service')::bool,          false) THEN 'ENFORCE'
    ELSE 'OFF'
  END                                                          AS resolved_mode
FROM tenant_settings ts
JOIN tenants t ON t.id = ts.tenant_id
WHERE ts.tenant_id = :tenant_id;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 1 — Promote OFF → ADVISORY  (safe; default first move)
-- ════════════════════════════════════════════════════════════════════════════
-- Effect after this runs:
--   service=false, advisory=true  → resolved mode = ADVISORY
-- Behaviour change:
--   * Each surface (chat / automation / segment-change) computes a decision
--     and writes a row into offer_decisions.
--   * Legacy coupon issuance is unchanged. Customers still get the same
--     codes they would have gotten with the flag off.
--   * Issued coupons are back-stamped with `decision_id` + `decision_mode`
--     in their JSON metadata.

\if :{?tenant_id} \else \set tenant_id 1 \endif

UPDATE tenant_settings
SET metadata = COALESCE(metadata, '{}'::jsonb)
            || jsonb_build_object(
                 'tenant_features',
                 COALESCE(metadata->'tenant_features', '{}'::jsonb)
                 || jsonb_build_object(
                      'offer_decision_service',          false,
                      'offer_decision_service_advisory', true
                    )
               )
WHERE tenant_id = :tenant_id
RETURNING tenant_id, metadata->'tenant_features' AS tenant_features;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 2 — Promote ADVISORY → ENFORCE  (decision service becomes authoritative)
-- ════════════════════════════════════════════════════════════════════════════
-- Only run after the ADVISORY checklist in the runbook has passed.
-- Effect:
--   service=true, advisory=false  → resolved mode = ENFORCE
-- Behaviour change:
--   * Decision service now decides what to issue. Legacy code is bypassed
--     on all three surfaces.
--   * Same offer_decisions ledger row gets written, but `attributed` /
--     `chosen_*` reflect the service's decision, not legacy heuristics.

\if :{?tenant_id} \else \set tenant_id 1 \endif

UPDATE tenant_settings
SET metadata = COALESCE(metadata, '{}'::jsonb)
            || jsonb_build_object(
                 'tenant_features',
                 COALESCE(metadata->'tenant_features', '{}'::jsonb)
                 || jsonb_build_object(
                      'offer_decision_service',          true,
                      'offer_decision_service_advisory', false
                    )
               )
WHERE tenant_id = :tenant_id
RETURNING tenant_id, metadata->'tenant_features' AS tenant_features;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 3 — ROLLBACK: ENFORCE → ADVISORY  (back to shadow mode)
-- ════════════════════════════════════════════════════════════════════════════
-- Use this if ENFORCE produced unexpected discounts, customer complaints,
-- or attribution gaps. Switching back to ADVISORY immediately restores
-- legacy issuance while keeping the decision telemetry flowing.
-- ADVISORY wins over ENFORCE in the resolver, so this is safe even if
-- you forget to flip the enforce flag back to false first.

\if :{?tenant_id} \else \set tenant_id 1 \endif

UPDATE tenant_settings
SET metadata = COALESCE(metadata, '{}'::jsonb)
            || jsonb_build_object(
                 'tenant_features',
                 COALESCE(metadata->'tenant_features', '{}'::jsonb)
                 || jsonb_build_object(
                      'offer_decision_service',          false,
                      'offer_decision_service_advisory', true
                    )
               )
WHERE tenant_id = :tenant_id
RETURNING tenant_id, metadata->'tenant_features' AS tenant_features;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 4 — ROLLBACK: ADVISORY → OFF  (turn telemetry off entirely)
-- ════════════════════════════════════════════════════════════════════════════
-- Use this only if you need to fully detach a tenant from the decision
-- service (e.g. you suspect the advisory write itself is causing problems).
-- After this, no new offer_decisions rows will be written for the tenant;
-- existing rows are kept (no DELETE).

\if :{?tenant_id} \else \set tenant_id 1 \endif

UPDATE tenant_settings
SET metadata = COALESCE(metadata, '{}'::jsonb)
            || jsonb_build_object(
                 'tenant_features',
                 COALESCE(metadata->'tenant_features', '{}'::jsonb)
                 || jsonb_build_object(
                      'offer_decision_service',          false,
                      'offer_decision_service_advisory', false
                    )
               )
WHERE tenant_id = :tenant_id
RETURNING tenant_id, metadata->'tenant_features' AS tenant_features;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 5 — Verify: ledger rows by surface (last 7 days)
-- ════════════════════════════════════════════════════════════════════════════
-- Confirms each surface (chat / automation / segment_change) is producing
-- decisions. In ADVISORY mode you expect at least one row per actively
-- exercised surface; OFF tenants should return zero rows for the window.

\if :{?tenant_id} \else \set tenant_id 1 \endif

SELECT
  surface,
  COUNT(*)                                            AS rows,
  COUNT(DISTINCT chosen_source)                       AS distinct_sources,
  ROUND(AVG(discount_value)::numeric, 2)              AS avg_discount,
  MAX(created_at)                                     AS most_recent
FROM offer_decisions
WHERE tenant_id  = :tenant_id
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY surface
ORDER BY surface;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 6 — Verify: top reason codes (sanity-check the policy)
-- ════════════════════════════════════════════════════════════════════════════
-- Mirrors the `reason_codes` field in /offers/decisions/breakdown.
-- Useful when validating ENFORCE: the set of reason codes should look
-- materially different from ADVISORY (e.g. you should see fewer
-- `legacy_step_coupon_override` rows once the service is authoritative).

\if :{?tenant_id} \else \set tenant_id 1 \endif

SELECT
  reason::text                                        AS reason_code,
  COUNT(*)                                            AS rows
FROM offer_decisions,
     LATERAL jsonb_array_elements_text(reason_codes::jsonb) AS reason
WHERE tenant_id  = :tenant_id
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY reason::text
ORDER BY rows DESC, reason_code
LIMIT 20;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 7 — Verify: attribution + revenue (the gate for ENFORCE)
-- ════════════════════════════════════════════════════════════════════════════
-- The redemption_rate_pct and attributed_revenue here must roughly match
-- the headline KPIs returned by GET /offers/decisions/summary?days=7.
-- A non-zero `offers_attributed` count is the strongest signal that the
-- end-to-end pipe (decision → coupon → order webhook → attribution) is
-- working before you flip ENFORCE.

\if :{?tenant_id} \else \set tenant_id 1 \endif

WITH window_rows AS (
  SELECT *
  FROM offer_decisions
  WHERE tenant_id  = :tenant_id
    AND created_at >= NOW() - INTERVAL '7 days'
)
SELECT
  COUNT(*)                                            AS decisions_total,
  COUNT(*) FILTER (WHERE chosen_source <> 'no_offer') AS offers_issued,
  COUNT(*) FILTER (WHERE attributed IS TRUE)          AS offers_attributed,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE attributed IS TRUE)
          / NULLIF(COUNT(*) FILTER (WHERE chosen_source <> 'no_offer'), 0),
    2
  )                                                   AS redemption_rate_pct,
  COALESCE(SUM(revenue_amount) FILTER (WHERE attributed IS TRUE), 0) AS attributed_revenue
FROM window_rows;
