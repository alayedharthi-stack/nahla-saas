# Commerce Agent V2 real WhatsApp acceptance harness

This harness prepares and scores the Phase 2.6 real-channel batch. It does not
activate Commerce V2, change Railway configuration, link a WhatsApp account, or
send a message without a separately approved live step.

## Safety contract

- Tenant 1 is the only permitted live tenant.
- Accounts A, B, and C must be three distinct, test-owned WhatsApp accounts.
- The corpus exposes only the seven Commerce V2 read tools.
- `schedule` fails unless outbound ownership is empty in preparation mode, or
  exactly Tenant 1 in a separately authorized `--live` mode.
- Observation uses exact inbound WAMID, conversation, and SDK trace correlation.
- Production observation starts a read-only transaction and always rolls back.
- Safety and mutation fields fail closed until evidence explicitly proves zero.
- A material safety failure requires immediate Tenant 1 rollback and batch stop.

## Workflow after live approval

1. Validate the corpus with `validate-corpus`.
2. Confirm the three test-owned accounts and controlled Salla order fixture.
3. Run `schedule` to materialize a seeded 180-turn plan. Numbers are validated
   locally but are never written to the plan; only A/B/C aliases are recorded.
4. Work sends the plan through the three linked WhatsApp test devices. Turns are
   interleaved in 60 concurrency waves and split 90/90 across AUTO and FAST.
5. Capture the controlled order/shipment fingerprint with `snapshot`, then
   collect each exact persisted turn with `observe`.
6. Capture the final fingerprint and verify it with `compare-state`. Enrich
   grounding and isolation evidence from the structured output and test fixtures.
7. Run `score`. Missing proof fails closed. Stop and rollback on a material gate.

The corpus uses multiple paraphrases and seeded randomized sequence blocks. The
seed makes an execution reproducible without turning the harness into a fixed
phrase router.
