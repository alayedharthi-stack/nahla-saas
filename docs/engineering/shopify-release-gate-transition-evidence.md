# Shopify release gate: retained historical Claude validation report

**Superseded guard report.** These runs concern `2ab3042d` and the earlier
guard. Independent review subsequently changed its import boundary,
snapshot target validation and effective read-only check. The old PostgreSQL
passes do not establish validation of the modified guard. Current local
results and remaining blockers are in
[shopify-release-gate-independent-review-evidence.md](shopify-release-gate-independent-review-evidence.md).
Draft #1214 remains unaccepted pending that validation and independent review.

Tested source: commit `2ab3042d331ce9c7cbef77d75e4e834de25f40d5` (tree `d9a81d86084d07366856b04d28e4b3b29eba8989`), branch `claude/shopify-release-gate-transition-20261011`, stacked on PR #1211 at `8308c16e39a18745239e869e6996c7aeaf680fd4`. The working tree was clean (porcelain 0) before and after every run and the HEAD was re-verified after each run; this document is a docs-only addition on top of that commit and changes no tested file.

Disposable PostgreSQL servers only (synthetic role and data; databases created and dropped per case). No live target, no inherited `DATABASE_URL`, no platform or Shopify setting touched. PG16 server: `16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)`; PG18 server: `18.6`. Legacy checkout for the 462 cases: commit `462ede1aff602cad84e9569be859cca0087b190d` (porcelain 0).

## 1. Frozen tree inventory (what the rendered command would pin for this tree)

| Item | Value |
|---|---|
| `gate_version` | `2` |
| `gate_sha256` | `6a8b99a52f426ed8ba17134b7fefa5db897062da5421c2c0723b62bca7b54a42` |
| `inventory_count` | `143` |
| `inventory_digest` | `a144e8da23fc98c4ea5856409a1628b3fbca6d78683e4ace08bb9dc91f28dd4b` |
| `database_py_digest` | `b3fcf5d151a106a9068985118d6489c381844c1ba5553fbffc9d94ffe272aa50` |
| `unexpected_initializers` | `[]` |

The legacy rule (four fixed files + every `database/**/*.py`, sorted, `path NUL bytes NUL`) reproduces the legacy guard digest `12798863bd12348de4407263fd1a1fe86ef639a0df2bfac2fc94244bd1f714c4` over exactly 132 files at `462ede1aff602cad84e9569be859cca0087b190d` (DB-free case `test_legacy_guard_digest_is_reproduced_on_the_deployed_checkout_when_available`, run with the 462 checkout below).

## 2. Runs (sequential, frozen source)

| # | Run | Start (UTC) | End (UTC) | Result | rc | HEAD/tree re-verified after |
|---|---|---|---|---|---|---|
| 1 | DB-free (gate contract, topology audit script, bootstrap target, proofs runner) — run on `2c64ff9b6b96`, carried (see §2a) | 2026-10-11T18:15:32Z | 2026-10-11T18:17:00Z | 153 passed in 86.67s (0:01:26) | 0 | yes |
| 2 | Lint — no new silent excepts | — | — | no-silent-except: OK (624 pre-existing, all baselined) | 0 | n/a |
| 3 | GOV-002 scanner from BASE `8308c16e` blob (`--trusted-base-scanner`, base 8308c16e → head 2ab3042d331c) | — | — | FINDINGS=none | 0 | n/a |
| 4 | Constitution + GOV-001 guard — run on `2c64ff9b6b96`, carried (see §2a) | 2026-10-11T18:17:15Z | 2026-10-11T18:20:05Z | 98 passed in 168.51s (0:02:48) | 0 | yes |
| 5 | Gate PostgreSQL module on PG16 | 2026-10-11T18:52:23Z | 2026-10-11T18:57:24Z | 10 passed in 299.89s (0:04:59) | 0 | yes |
| 6 | Gate PostgreSQL module on PG18 | 2026-10-11T18:57:24Z | 2026-10-11T19:02:28Z | 10 passed in 303.15s (0:05:03) | 0 | yes |

Commands (from the retained runner): `pytest -q -p no:cacheprovider -rA tests/test_shopify_release_gate.py tests/test_alembic_topology_audit_script.py tests/test_normal_bootstrap_migration_target.py tests/commerce_reliability/test_required_postgres_proofs_runner.py` with `NAHLA_LEGACY_462_CHECKOUT` set; `python scripts/lint_no_silent_except.py`; the GOV-002 scanner extracted from the base commit's blob; `pytest -q -p no:cacheprovider backend/tests/test_constitution_compliance.py backend/tests/test_intelligence_non_interference_guard.py`; `pytest -q -p no:cacheprovider -rA backend/tests/test_shopify_release_gate_pg.py` with `NAHLA_RELIABILITY_REQUIRE_PG=1`, the disposable admin DSN, `NAHLA_RELEASE_GATE_PG_HOST_ALIAS` (a non-loopback-spelled name of the disposable server) and `NAHLA_LEGACY_462_CHECKOUT`, once per server.


### 2a. Carried runs and their proof

The DB-free and constitution runs were executed once on commit `2c64ff9b6b96365ee939c9b982febac06eeef49a` (tree `efabb0ddd52f8c8398023f2fde0327dbad5cea94`; clean before and after: `BEFORE frozen runs: HEAD=2c64ff9b6b96365ee939c9b982febac06eeef49a tree=efabb0ddd52f8c8398023f2fde0327dbad5cea94 expected=2c64ff9b6b96365ee939c9b982febac06eeef49a match=yes porcelain=0 branch=claude/shopify-release-gate-transition-20261011` / `AFTER frozen runs: HEAD=2c64ff9b6b96365ee939c9b982febac06eeef49a tree=efabb0ddd52f8c8398023f2fde0327dbad5cea94 porcelain=0`). The only file that differs between that commit and the tested head `2ab3042d331ce9c7cbef77d75e4e834de25f40d5` is `backend/tests/test_shopify_release_gate_pg.py` (a PostgreSQL test case); neither the DB-free collection nor the constitution suite reads it, so those results hold for the tested head byte for byte. Lint, GOV-002 and both PostgreSQL module runs were executed again on the tested head because they do read that file.

## 3. Per-case results of the PostgreSQL module

| Case | PG16 | PG18 |
|---|---|---|
| `test_expectations_file_equals_fresh_snapshots_on_this_server` | passed (23.4 s) | passed (22.8 s) |
| `test_legacy_462_bootstrap_refuses_a_shopify_state_without_mutating_it` | passed (29.4 s) | passed (29.5 s) |
| `test_legacy_lock_key_contention_timeouts_and_read_only_database` | passed (22.7 s) | passed (23.8 s) |
| `test_phase_matrix_deploys_only_at_s0_and_s3_and_bootstrap_target_is_a_noop` | passed (46.3 s) | passed (48.0 s) |
| `test_retained_definition_drift_is_refused_and_exact_restoration_passes` | passed (36.2 s) | passed (36.0 s) |
| `test_s0_refuses_any_shopify_object` | passed (20.1 s) | passed (19.3 s) |
| `test_s3_deploy_needs_a_verified_restore_and_s0_does_not` | passed (26.4 s) | passed (26.3 s) |
| `test_s3_footprint_is_checked_by_definition_across_every_user_schema` | passed (46.6 s) | passed (48.8 s) |
| `test_source_side_refusals_never_reach_the_database` | passed (27.7 s) | passed (26.7 s) |
| `test_version_rows_malformed_states_are_refused_and_never_echoed` | passed (20.4 s) | passed (21.3 s) |

## 4. Per-case results of the DB-free module

| Case | Result |
|---|---|
| `test_only_s0_and_s3_are_deployment_phases` | passed |
| `test_contract_constants_match_the_frozen_sources` | passed |
| `test_inventory_digest_is_sorted_path_nul_bytes_nul` | passed |
| `test_inventory_rule_on_the_repository_is_the_legacy_rule_plus_the_gate_files` | passed |
| `test_legacy_guard_digest_is_reproduced_on_the_deployed_checkout_when_available` | passed |
| `test_source_tamper_and_planted_initializer_are_refused` | passed |
| `test_bootstrap_refuses_a_tampered_module_before_it_can_execute[scripts/operators/shopify_release_gate_contract.py]` | passed |
| `test_bootstrap_refuses_a_tampered_module_before_it_can_execute[scripts/operators/commerce_runtime_pilot_migration.py]` | passed |
| `test_bootstrap_refuses_a_tampered_module_before_it_can_execute[database/migrations/versions/0121_shopify_connection_foundation.py]` | passed |
| `test_bootstrap_refuses_a_tampered_module_before_it_can_execute[scripts/operators/__init__.py]` | passed |
| `test_bootstrap_refuses_a_tampered_module_before_it_can_execute[scripts/operators/shopify_release_gate.py]` | passed |
| `test_bootstrap_refuses_a_tampered_module_before_it_can_execute[database/migrations/env.py]` | passed |
| `test_bootstrap_refuses_a_planted_initializer_and_an_added_migration` | passed |
| `test_bootstrap_refuses_wrong_release_identity_libpq_and_payload_before_any_import` | passed |
| `test_bootstrap_hands_over_to_the_gate_only_when_every_pin_holds` | passed |
| `test_render_round_trips_and_pins_every_literal` | passed |
| `test_render_refuses_malformed_inputs[kwargs0-phase_not_deployable]` | passed |
| `test_render_refuses_malformed_inputs[kwargs1-phase_not_deployable]` | passed |
| `test_render_refuses_malformed_inputs[kwargs2-unknown_phase]` | passed |
| `test_render_refuses_malformed_inputs[kwargs3-unknown_phase]` | passed |
| `test_render_refuses_malformed_inputs[kwargs4-release_sha_malformed]` | passed |
| `test_render_refuses_malformed_inputs[kwargs5-approval_sha_malformed]` | passed |
| `test_render_refuses_malformed_inputs[kwargs6-db_target_malformed]` | passed |
| `test_render_refuses_malformed_inputs[kwargs7-identity_malformed]` | passed |
| `test_render_refuses_malformed_inputs[kwargs8-identity_malformed]` | passed |
| `test_render_refuses_malformed_inputs[kwargs9-token_not_renderable]` | passed |
| `test_render_cli_refuses_an_unapproved_or_unbound_payload_with_closed_tokens` | passed |
| `test_renderer_never_prints_a_third_party_exception_body` | passed |
| `test_rows_exact_multiset_fail_closed_and_unknown_rows_are_never_echoed` | passed |
| `test_footprint_pins_the_exact_shopify_sequence_set_with_ownership` | passed |
| `test_footprint_is_exact_by_canonical_definition_and_reports_only_expected_names` | passed |
| `test_retained_definitions_are_compared_by_definition_and_reported_by_name_only` | passed |
| `test_relation_identity_must_hold_across_the_run_and_names_only_from_the_allow_list` | passed |
| `test_expectations_file_is_bound_to_these_migrations_and_covers_retained_and_s3_relations` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[1-NAHLA_SHOPIFY_CONNECTION_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[1-NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[1-NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[1-NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[1-NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[1-VITE_SHOPIFY_CONNECTION_UI]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[1-VITE_SHOPIFY_CATALOG_IMPORT_UI]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[true-NAHLA_SHOPIFY_CONNECTION_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[true-NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[true-NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[true-NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[true-NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[true-VITE_SHOPIFY_CONNECTION_UI]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[true-VITE_SHOPIFY_CATALOG_IMPORT_UI]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[YES-NAHLA_SHOPIFY_CONNECTION_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[YES-NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[YES-NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[YES-NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[YES-NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[YES-VITE_SHOPIFY_CONNECTION_UI]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[YES-VITE_SHOPIFY_CATALOG_IMPORT_UI]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[ on -NAHLA_SHOPIFY_CONNECTION_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[ on -NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[ on -NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[ on -NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[ on -NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[ on -VITE_SHOPIFY_CONNECTION_UI]` | passed |
| `test_every_shopify_flag_including_the_dashboard_ones_must_be_off[ on -VITE_SHOPIFY_CATALOG_IMPORT_UI]` | passed |
| `test_falsey_or_unset_flags_pass` | passed |
| `test_identity_requires_every_platform_id_and_never_prints_actual_values` | passed |
| `test_database_target_goes_through_the_pilot_job_validators` | passed |
| `test_parse_helpers_fail_closed_on_malformed_input` | passed |
| `test_shipped_template_is_unfilled_and_is_not_part_of_the_inventory` | passed |
| `test_valid_payload_passes_for_its_phase_only` | passed |
| `test_payload_refusals[changes0-approval_record_version]` | passed |
| `test_payload_refusals[changes1-approval_release_mismatch]` | passed |
| `test_payload_refusals[changes2-approval_not_bound_to_this_source]` | passed |
| `test_payload_refusals[changes3-approval_not_bound_to_this_source]` | passed |
| `test_payload_refusals[changes4-approval_target_mismatch]` | passed |
| `test_payload_refusals[changes5-approval_target_mismatch]` | passed |
| `test_payload_refusals[changes6-approval_target_mismatch]` | passed |
| `test_payload_refusals[changes7-approval_phase_mismatch]` | passed |
| `test_payload_refusals[changes8-approval_missing]` | passed |
| `test_payload_refusals[changes9-approval_missing]` | passed |
| `test_payload_refusals[changes10-approval_missing]` | passed |
| `test_payload_refusals[changes11-approval_timestamp_invalid]` | passed |
| `test_payload_refusals[changes12-approval_timestamp_order]` | passed |
| `test_payload_refusals[changes13-deployment_rollback_snapshot_missing]` | passed |
| `test_payload_refusals[changes14-deployment_rollback_snapshot_missing]` | passed |
| `test_payload_refusals[changes15-deployment_rollback_snapshot_missing]` | passed |
| `test_payload_refusals[changes16-deployment_rollback_snapshot_missing]` | passed |
| `test_payload_refusals[changes17-approval_timestamp_invalid]` | passed |
| `test_payload_refusals[changes18-approval_timestamp_order]` | passed |
| `test_payload_refusals[changes19-approval_timestamp_order]` | passed |
| `test_payload_refusals[changes20-backup_evidence_missing]` | passed |
| `test_payload_refusals[changes21-backup_evidence_missing]` | passed |
| `test_payload_refusals[changes22-backup_identity_mismatch]` | passed |
| `test_payload_refusals[changes23-backup_identity_mismatch]` | passed |
| `test_payload_refusals[changes24-backup_size_invalid]` | passed |
| `test_payload_refusals[changes25-backup_size_invalid]` | passed |
| `test_payload_refusals[changes26-backup_size_invalid]` | passed |
| `test_payload_refusals[changes27-backup_size_invalid]` | passed |
| `test_payload_refusals[changes28-backup_size_invalid]` | passed |
| `test_payload_refusals[changes29-backup_not_completed]` | passed |
| `test_payload_refusals[changes30-backup_not_completed]` | passed |
| `test_payload_refusals[changes31-backup_not_restorable]` | passed |
| `test_payload_refusals[changes32-approval_timestamp_invalid]` | passed |
| `test_payload_refusals[changes33-approval_timestamp_invalid]` | passed |
| `test_payload_refusals[changes34-approval_timestamp_order]` | passed |
| `test_payload_refusals[changes35-approval_timestamp_order]` | passed |
| `test_payload_refusals[changes36-approval_timestamp_order]` | passed |
| `test_payload_refusals[changes37-backup_too_old]` | passed |
| `test_payload_refusals[changes38-backup_too_old]` | passed |
| `test_payload_time_windows_and_freshness_at_run_time` | passed |
| `test_schema_changing_phase_requires_a_verified_restore_between_backup_and_approval` | passed |
| `test_approval_payload_reading_is_pinned_and_closed` | passed |
| `test_migration_preflight_passes_on_the_repository_and_refuses_duplicates_dependencies_and_malformed_files` | passed |
| `test_graph_edges_heads_and_branch_deltas_hold_for_every_phase` | passed |
| `test_graph_mismatch_is_refused_before_the_database` | passed |
| `test_main_stops_at_the_first_refusal_and_never_loads_migrations_after_an_inventory_failure` | passed |
| `test_lock_denied_on_the_legacy_key_refuses_without_reading_anything_and_without_unlocking` | passed |
| `test_unlock_failure_fails_the_run_even_when_every_check_passed` | passed |
| `test_search_path_mismatch_and_relation_replacement_are_refused` | passed |
| `test_timeouts_and_errors_are_reported_by_closed_token_and_type_only` | passed |
| `test_connect_failure_is_a_refusal_by_type_only` | passed |
| `test_gate_module_has_no_mutation_path` | passed |
| `test_cli_fails_closed_and_never_echoes_arguments_or_the_dsn` | passed |

## 5. Retained development runs (not evidence; pre-freeze working tree)

* PG16 development run 1 (first slice, pre-review working tree): 3 failed, 5 passed in 193.23s (0:03:13)
* PG16 development run 2 (first slice, three corrected cases): 3 passed, 5 deselected in 63.67s (0:01:03)
* PG16 development run 3 (corrected design, uncommitted working tree): 2 failed, 8 passed in 252.17s (0:04:12)
* PG16 development run 4 (corrected design, uncommitted working tree): 1 failed, 9 passed in 283.76s (0:04:43)
* superseded frozen runs on da54e6bf (first slice; stopped after the independent review): dbfree: 85 passed in 83.63s (0:01:23); lint: no-silent-except: OK (624 pre-existing, all baselined); gov002: FINDINGS=none rc=0 ; SUPERSEDED: frozen runs on da54e6bf stopped after the coordinator's independent review of 3dd61a60 reported 7 blockers; logs retained as diagnostic notes only
* superseded frozen runs on 9f1cf726 (corrected design; stopped after the owned-sequence and rollback-field findings): SUPERSEDED: stopped after the coordinator's source review at 27903a59/9f1cf726 found the owned-sequence footprint hole and rollback-snapshot field tightening; diagnostic only
* frozen runs on 2c64ff9b (sequence pinning; PG16 and PG18 one test-side failure each: the wrong-shaped-table case had dropped the serial key): dbfree: 153 passed in 86.67s (0:01:26); lint: no-silent-except: OK (624 pre-existing, all baselined); gov002: FINDINGS=none rc=0 ; constitution: 98 passed in 168.51s (0:02:48); gate pg16: 1 failed, 9 passed in 278.42s (0:04:38); gate pg18: 1 failed, 9 passed in 275.96s (0:04:35)
* frozen runs on c3a09d9e (PG16 and PG18 one test-side failure each: the partial-footprint assertion expected the table alone, the gate also names its sequence): dbfree: carried from 2c64ff9b (see proof); lint: no-silent-except: OK (624 pre-existing, all baselined); gov002: FINDINGS=none rc=0 ; constitution: carried from 2c64ff9b (see proof); gate pg16: 1 failed, 9 passed in 269.22s (0:04:29); gate pg18: 1 failed, 9 passed in 277.98s (0:04:37)
* frozen runs on c34864bb (PG16 and PG18 one test-side failure each: the re-ownership case named a column that table does not have): dbfree: carried from 2c64ff9b (see proof); lint: no-silent-except: OK (624 pre-existing, all baselined); gov002: FINDINGS=none rc=0 ; constitution: carried from 2c64ff9b (see proof); gate pg16: 1 failed, 9 passed in 289.11s (0:04:49); gate pg18: 1 failed, 9 passed in 284.79s (0:04:44)
* PG16 single-case development run 5 (the corrected footprint case only): 1 passed, 9 deselected in 43.61s

Diagnostic notes (kept separate from the terminal evidence above): the first slice's development run 1 failed three cases for test-side reasons (a primary key on `alembic_version`, dependent objects on the conversation scope key, tuple-vs-list snapshot shape); its frozen runs on `da54e6bf` were stopped when the coordinator's independent review of `3dd61a60` reported seven blockers (trust order, S1/S2 deployability, legacy lock key and search path, approval and backup rules, PG18 constraint rows and index validity, S3 footprint by name only, secret-safe failures) plus two additions (dashboard VITE flags, explicit duplicate/dependency refusal). On the corrected design, development run 3 failed two cases for test-side reasons (an enum stray counts together with its implicit array type; a `database checks skipped` assertion predating the fail-fast message fix) and run 4 failed one (the connection key cannot be dropped in place because composite foreign keys depend on it; the case now uses a dependency-free key); the frozen runs on `9f1cf726` were stopped when the review found the owned-sequence footprint hole, the rollback-snapshot field tightening and the renderer's third-party exception body path. After those corrections, three successive frozen heads (`2c64ff9b`, `c3a09d9e`, `c34864bb`) each failed exactly one PostgreSQL case on both servers for a test-side reason inside the S3 footprint case (listed above); the gate reported correct refusals each time (a vanished serial key is a missing pinned sequence, which outranks a definition drift; a dropped table's sequence is listed as missing with it; `shopify_shop_leases` has no `id` column). Every other case passed on every one of those heads. The DB-free and constitution runs on `2c64ff9b` are carried to the final head under the one-file diff proof in §2a. The frozen runs in §2 on the final head are the only evidence claimed for it; development runs are listed above with their own outcomes.

## 6. What the runs do not cover

* GitHub checks on the stacked PR run only the GOV-002 trusted-base scanner and the merge-freeze gate; the root CI job does not run for this base, so these tests were executed here only.
* The legacy inline guard's own code is not in the repository; its digest rule (132 files, exact digest) and its advisory key were reproduced from the owner's description, and its refusal of Shopify states was reproduced through the 462 checkout's bootstrap command, not by executing the inline guard.
* No run touched the existing platform database, Railway settings, the Shopify shell or any secret; the observed backup card is not entered anywhere.

## 7. Owner audit figures reproduced on a disposable S0 build

Six retained tables (`commerce_runtime_handover_barrier`, `commerce_runtime_handover_workers`, `commerce_runtime_deferred_inbound`, `commerce_runtime_navigation_snapshots`, `catalog_channel_retirements`, `meta_catalog_authorizations`): 90 columns, 33 constraints, 18 indexes; five serial sequence ownerships; `tenants.id`/`users.id` integer; `commerce_runtime_conversations` `UNIQUE (id, tenant_id, namespace)`; `whatsapp_oauth_nonces` with `UNIQUE (nonce_hash)`, tenant FK `ON DELETE CASCADE`, indexes on `expires_at` and `tenant_id`; no Shopify-named relation, type, function or constraint; 122 public tables at S0. These, the fifteen S3 Shopify tables by canonical definition and their fourteen serial sequences with owning table and column (`shopify_shop_leases` carries no serial key), are pinned in `shopify_release_schema_expectations.json` (DB-free case `test_expectations_file_is_bound_to_these_migrations_and_covers_retained_and_s3_relations`) and re-derived on both servers (`test_expectations_file_equals_fresh_snapshots_on_this_server`), with PostgreSQL 18 NOT NULL constraint rows excluded as redundant with column nullability.

