# Corrected dormant Shopify review bundle

Draft correction: https://github.com/alayedharthi-stack/nahla-saas/pull/1220

Exact deployable source R: `94da9e2bcb41248180808e791d98f384c46f5779` on `codex/shopify-live-nonce-baseline-20261011`. The final documentation commit
may follow R; its runtime inventory must stay byte-identical to R. This bundle
supersedes the prior e345ecf6 source/payload/command pins together.

The only release-data correction is `whatsapp_oauth_nonces.created_at`'s
expected server default, from `CURRENT_TIMESTAMP` to JSON `null`. Live read-only
metadata showed that exact no-default definition. The six other full table
definitions and all nonce constraints/indexes match. The unchanged ORM supplies
a Python timestamp; the unchanged raw SQL writer supplies `created_at` explicitly.
Migration 0103 accepts the existing table without requiring a server default.
No live ALTER, comparator change, skipped column or permissive default set exists.
Any added default remains drift.

The focused proof creates the adopted table before 0103 once and carries the
same disposable database through S0 startup, 0121/0122/0123 and S3 startup with
no fixture reset. It exercises insertion, expiry, tenant/mode/redirect rejection
and single-use consumption. Full prior application/ASGI proofs are carried;
only the bounded compatibility tests are rerun for this correction.
All five focused tests passed on PostgreSQL 18.6/Python 3.11.17 in
[run 38186099701](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38186099701).
See `../shopify-live-nonce-baseline-results.json` for exact results and log bytes.

Canonical Git-byte inventory: **143 files**, digest
`589ed9d8805d773ca4d5957038e69c9e0eb1684d341fff1e59e8165d64b4108d`. Windows working-tree line endings are not
used for release pins. The gate, contract, renderer, ORM, nonce writer and all
migrations are unchanged. See `generation.offline.json` and `manifest.json`.

Both `approval.*.UNAPPROVED.json` records remain unfilled execution records.
Their stock verdict is `approval_missing`; this does not contradict the
parent-held earlier human merge/deploy authorization. The parent reconciles
that scope against the exact reviewed release, targets and setting changes.
No new broad owner permission is requested by this bundle.

Readable bootstrap and one-line command previews are rendered from the new
payload bytes and verified against the matching sealed payload in each API
patch. They are review previews only and must not be applied. Neither gate nor
bootstrap is executed against live services during artifact generation.
The strict renderer refuses the current unfilled records.

After independent review and parent authorization reconciliation, complete the
S0 external record **outside the repository**, then rerender and review its
exact payload/command together:

```text
python scripts/operators/shopify_release_gate_render.py render --root <exact-R-checkout> --release-sha 94da9e2bcb41248180808e791d98f384c46f5779 --phase S0 --approval-file <owner-held-approved-S0.json> --project f0090862-0a40-4293-bd5d-e94df58762b5 --environment ede962ce-3042-4dae-94de-623837e83ed9 --service 686b36c5-a926-4e58-912a-5e9d13fbc2e7 --db postgres-ancu.railway.internal:5432/railway --backup-service b77b3d27-47b0-4a3a-83fd-44def66a3a84 --backup-volume 009bd0d5-85ed-4de4-99fc-94ea963c9d65
```

Dormant S0 changes only the API source/predeploy/approval record/five backend
flags and dashboard source/two Vite build flags, all flags `0`. Builders,
start commands, domains, volumes, regions, replicas and provider credentials
are preserved. Existing `NAHLA_SKIP_PREFLIGHT=1` is not flipped by this patch;
its unchanged predicates pass, as recorded in the read-only metadata.

Fresh metadata confirms exact S0 heads `{0111,0113,0120}`, zero Shopify
objects, PostgreSQL 18.6 primary, eligible completed/restorable backup
2026-10-11T16:54:10.145Z / 647 MB and both rollback bindings. Dashboard Vite
flags are absent from current service variables; historical compiled values
are not verified. The API gate cannot attest another service's compiled flags.

S0 allows `restore_verified=false`; it needs no clone, migration or S3
prerequisite. Recheck source/state/backup age/rollback at action time and verify
the new dashboard build flags separately. Source, API command, payload and
flags must be applied as one reviewed configuration change. Inspect staged
diffs and terminal deployment/gate/startup/health evidence before completion.

S3 remains later separate work: verified private restore rehearsal, enforced
deployment exclusion, explicit one-step migrations with per-step audits and
its own approved payload/command. `migration.NOT_AUTHORIZED.plan.json` keeps
the exact forward/branch-qualified rollback argv; no command is executed here.
No deployment at S1/S2, activation, live Shopify setting or new environment is
part of this correction. See `authorizations.json` for later scope.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
