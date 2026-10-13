# S0 full-image import dependency pin correction

Draft PR: [#1221](https://github.com/alayedharthi-stack/nahla-saas/pull/1221).
Tested code source: `a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec`.
Dedicated release ref: `release/shopify-dormant-s0-a8fb88e3` at exactly that SHA.
The working branch may receive documentation commits afterward; it is not the
future deployment source. Independent review of this correction is pending.

## First divergence and source of truth

The exact-source Railway attempt built the reviewed `94da9e2b` source and
passed root, release, inventory, libraries and migration preflight checks. It
then refused `repository_import_unverified` before graph or database checks.
The failed API deployment was `83c84a64-e6a6-4096-b8e9-d204ce834642`.
Its image manifest digest was
`sha256:1ccbb46617e497c1860b0df43bb307534e12d18b6c1cd0a5d05874d2e83fe340`.
Both services were subsequently recovered to the prior `462ede1` source.

Before changing the inventory, [run 38240282982](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38240282982)
built the complete frozen baseline with the unchanged production Dockerfile.
The unmodified rendered command reproduced the same refusal with network
disabled and no database access. A second diagnostic run used a standard-library
profiling observer to identify `backend/__init__.py`, requested by `find_spec`.
The observer did not alter the command's refusal conditions or stock guard.

Alembic graph discovery imports revision files even when they are not applied.
The already inventoried `0115_merchant_payments_foundation.py` imports
`PAYMENT_TABLES` and `PaymentBase` from `backend.payments.models` at module load.
Python package traversal therefore needs exactly three existing files.
The preceding partial inventory export omitted them, so its earlier proofs
could not establish full production-image graph compatibility.

## Reviewed closure and bounded correction

| Existing file | Bytes | SHA-256 |
| --- | ---: | --- |
| `backend/__init__.py` | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `backend/payments/__init__.py` | 217 | `e9405f349df7e7078d53d10552a647912b8d395777df0606b6a7bff600aecbf4` |
| `backend/payments/models.py` | 10882 | `af5efddc8ba52f7f27ac8c5255a3add10bc7ea6e82b3321d83bcd2854a798b88` |

The first initializer is empty and the second contains only documentation.
The models file imports `__future__`, `datetime`, `sqlalchemy` and
`sqlalchemy.orm`; it uses separate `PaymentBase` metadata rather than startup
`database.models.Base`. Its declarations do not create financial tables.
All three files are byte-identical to the accepted baseline.

Only the three explicit paths are added to `RUNTIME_EXTRA_FILES` in
`shopify_release_gate_contract.py`. After removing these entries, the entire
contract AST equals the baseline AST. The bootstrap, finder, source-only loader,
gate, revision graph, model files, schema expectations, Dockerfile, dependencies
and application runtime are unchanged. No backend wildcard or arbitrary import
allowance is introduced. No payment behavior, credential or provider call is
part of this change.

The corrected Git-byte inventory contains **146 files**, digest
`31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174`.
The unchanged gate digest is
`206eb16195a8afe3307f835c047553db6c3f917d9df8f5172a90cab3dee33157`;
the unchanged database Python digest is
`b3fcf5d151a106a9068985118d6489c381844c1ba5553fbffc9d94ffe272aa50`.

## Executed proof and provenance

[Final run 38243591231](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38243591231)
and [job 114791905740](https://github.com/alayedharthi-stack/nahla-saas/actions/runs/38243591231/job/114791905740)
completed successfully from the exact tested SHA at 11:36:57 UTC on
2026-10-12. The workflow built baseline and candidate images from full frozen
checkouts with the real unchanged production Dockerfile and identical dependency
layers. Both used Python 3.11.17 and passed `pip check`; fixture PostgreSQL was
18.6 on an internal disposable Docker network with no published port.

The proof driver was mounted read-only outside `/app`. The positive gate command
used the unmodified strict renderer and stock bootstrap. It passed all **19** S0
checks: root, release SHA, inventory, libraries, migration preflight, graph,
flags, platform identity, approval, DB target, expectations, connection, server,
lock, version rows, retained schema, Shopify footprint, relation identity and
unlock. Before/after canonical schema, version rows and relation OIDs matched.
Financial and Shopify table counts remained zero. No application startup,
scheduler or provider calls ran.

Each new pinned file was independently mutated with valid Python that would
write a sentinel if executed. All were refused with `inventory_digest_mismatch`,
with the sentinel absent and no database check. An arbitrary unreviewed backend
module remained outside the inventory and was refused by the unchanged stock
finder with `repository_import_unverified` before planted code or database
access. That diagnostic case adds an import request to a disposable bootstrap
after stock verification, without changing the guard.

Four focused inventory tests passed: exact reviewed closure/no wildcard;
sorted path-NUL-bytes-NUL digest; legacy inventory plus gate files; and source
tamper/planted initializer refusal. The adopted nonce fixture retained its OID
and no server default, with heads `{0111,0113,0120}`. It did not apply 0115 or
Shopify migrations. Prior accepted application/ASGI/Salla proofs are carried;
those suites were not rerun for this fixed-path correction.

Complete reports, distinct rebuilt image IDs, decoded log byte lengths/hashes,
exact log canonicalization, and three superseded proof-driver/setup failures
are recorded in
[proof-results.json](shopify-graph-dependency-review-bundle/proof-results.json).
The failed Railway registry artifact was not retrieved or reexecuted. Local
Docker was unavailable; no registry credentials were requested. This proof
establishes behavior in newly built real production images, not byte identity
with the earlier Railway artifact.

## Review artifacts and execution boundary

The [new S0 review bundle](shopify-graph-dependency-review-bundle/README.md)
contains the canonical inventory, exact closure, reports, frozen source binding,
unfilled external approval, bootstrap/command previews and API/dashboard patch
previews. Every artifact is pinned by its
[manifest](shopify-graph-dependency-review-bundle/manifest.json).
Payload/command/patch bytes agree and all seven proposed Shopify flags are `0`.
Stock strict rendering refuses the unfilled record with `approval_missing`
and exit 2. The owner's earlier authorization is retained; independent review
and a fresh exact action-time binding remain outstanding.

No live retry, migration, restore, activation or new environment was performed
by this correction. The latest carried recovery evidence shows API deployment
`45d4e81d-3047-41a9-b2ac-44ab66268c28` and dashboard deployment
`52cdbff2-8282-42f4-bf5b-82804d9d2066` healthy on `462ede1`, legacy settings
restored, flags OFF, unchanged S0 heads and zero Shopify objects. It is historical
10:41-10:48 UTC evidence, not a fresh execution attestation.

After independent acceptance, the parent must preserve and recheck the frozen
release ref, reobserve the eligible completed/restorable backup and rollback
targets, complete a new external record, and rerender/review source, payload,
command and service patch together before any retry. Do not reuse the expired
94da record or the current fail-closed previews. S0 needs no clone, migration
or S3 prerequisite; S3 and activation remain separate work.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
```
