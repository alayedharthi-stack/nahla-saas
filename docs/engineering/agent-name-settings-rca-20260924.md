# Merchant assistant name: settings-to-runtime RCA

Status: Draft; no merge, deployment, production configuration write or customer
test message is authorized by this change.

## Finding and first divergence

The currently saved name for **tenant 1** is **وردة**. The commerce-runtime
WhatsApp seam did not load `TenantSettings.ai_settings.assistant_name` into its
model context. `_context_preamble` supplied only channel, verified customer name
and conversation language. This is a **structured context omission**, before
inference, not evidence that the model ignored a correctly supplied name.

The repair reads the saved column for the verified tenant on every turn and
adds `assistant_name` to the existing `conversation_context` data block. It
changes no instructions and adds no conversational prose or response rewrite.

## Evidence and provenance

- Repository base: `5a49e1a731f6f336773fcb402a85e8ec63da84f0` (`main`).
- Railway production service `nahla-saas`, deployment
  `bf5c9199-a384-4ac4-a907-ea65b8fb6a89`, reported SUCCESS at
  `2026-09-24T17:05:50Z`, commit
  `40c4e047ed151750c402d106a084a706eb85c52e`, branch
  `ops/tenant1-haiku-20260921-f9021b59`. Production is not assumed to follow main.
- The settings save/read code, runtime seam, runtime entry, provider adapter
  and trusted tool-context builder were identical between those two commits.
- Authenticated live dashboard observation: Settings / System showed
  `Tenant ID = 1`; Intelligence, loaded afresh without editing or saving,
  showed `assistant_name = وردة`. The page reads `GET /settings`, whose
  implementation reads the tenant row and merges defaults without replacing
  a nonempty name. This is current application readback, **not a direct
  production SQL query or a historical audit of the original save**.
- The same tenant's persisted conversation showed `من انت ؟` at 23:05 Saudi
  time on September 24, followed by a reply beginning `أنا ناهلة` (the exact
  live spelling differs from the assignment's `نهلة`).
- Matching production trace at `2026-09-24T20:05:24.938Z`: tenant 1,
  application conversation 9, runtime turn 42, route `commerce_runtime`,
  one model step, zero tool calls, 236 reply characters,
  `dispatch_status=accepted`, `customer_reach=unknown`.
- The adjacent TURN log identifies the inbound as `من انت ؟`,
  `brain_called=False`, `outbound_sent=True`. The recorded model is
  `claude-haiku-4-5-20251001`; its selection is unchanged.
- Phone numbers, connection identifiers containing phone IDs, provider message
  IDs, account credentials and unrelated customer text are deliberately omitted.
  The spoken identifier `2-1` was not interpreted as tenant 21 or 33.

## Source-of-truth trace

| Layer | Existing path and finding |
|---|---|
| Merchant UI | `dashboard/src/pages/Intelligence.tsx`: input binds `ai.assistant_name`; `handleSave` sends `{ ai }`. |
| Save request | `dashboard/src/api/settings.ts`: `PUT /settings`. |
| Persistence | `backend/routers/settings.py`: `AISettingsIn.assistant_name`, update of `settings.ai_settings`, commit and refresh. `database/models.py`: tenant-unique JSONB `TenantSettings.ai_settings`. |
| Settings readback | `GET /settings` and `core.tenant.merge_ai_defaults` preserve a nonempty custom name. The current live readback is وردة. |
| Routing | `whatsapp_webhook.py` invokes `maybe_handle_with_commerce_runtime` after existing eligibility gates and returns when that runtime owns the turn. Logs establish this owner for turn 42. |
| First divergence | `services/commerce_runtime_pilot.py::_context_preamble` omitted the assistant setting. `_own_turn` passed that incomplete mapping to the runtime. |
| Other settings read | `CommerceAgentContext.from_trusted_scope` reads settings for locale/timezone, but does not expose `assistant_name`. The legacy persona/overlay identity loaders are not the owner of this turn. |
| Cache | There is no assistant-name cache on this runtime path to invalidate. Tool caches are per run; the missing setting never entered the context. The repair queries the scalar JSON column every turn, avoiding a stale ORM identity-map object. |
| Provider boundary | `runtime_entry.run_commerce_runtime_turn` forwards the preamble; `AnthropicReasoningProvider._messages` serializes it inside `conversation_context`. Instructions remain supplied separately and unchanged. |

## Bounded change

Only `backend/services/commerce_runtime_pilot.py` changes runtime behavior:

1. Read `ai_settings` by explicit tenant ID, on the caller's session before
   starting the runtime worker. Do not create settings or run configuration
   hygiene in this new read.
2. Preserve a nonblank saved name verbatim, including its script; no translation.
3. For a missing/blank name, supply `NAHLAH` when the existing conversation
   language is English (`en` or `en-*`), otherwise `نحلة`. No text-based language
   detection or change to conversation language is added.
4. A database read failure propagates to the existing runtime error handling;
   it is not silently converted into an assertion that the setting is absent.

There are no identity-specific keyword rules, fixed greetings, regex rewrites,
forced introductions, migrations, environment changes or cache restarts.

## Isolation from ongoing work

Work is on `codex/agent-name-settings-rca-20260924`, an independent checkout.
Open PR files and branches were inspected before editing:

- #1145: `claude/eager-feynman-yoiaw7` (Pagination).
- #1143: `claude/nahlah-commerce-c0-audit-lbubgc` (navigation predecessor).
- #1085: `fix/p27b-v3-runtime-fallback-integrity` (V2 context/runner).
- #1144 and #1146: campaign guard and campaign incident RCA.

None of their changed files is changed here. In particular, the runtime entry,
provider, loop, tool context, store knowledge and presentation files are untouched.
Campaign 35, campaign fixes and Railway patch `762f0a9a` are untouched.

## Validation and limits

`tests/test_commerce_runtime_assistant_identity.py` uses the real settings save
handler, SQL persistence, `_own_turn`, and provider serialization. It replaces
the ledger/transport entry with a recording boundary and substitutes inference;
it neither calls a model nor sends a WhatsApp message.

- Before repair: the saved-name case failed with `KeyError: 'assistant_name'`
  after successful save and database readback of وردة.
- After repair: custom-name delivery; second generic clothing merchant named
  Atlas; Arabic custom name preserved in an English conversation; absent,
  missing-key, empty and whitespace names in Arabic/English; rename visible
  on the next turn while an old ORM settings object remains cached; failed
  settings read is not mistaken for a missing name.
- The test pins the separately supplied system instructions and model value.
- Existing seam test fixture gains only support for the new scalar settings
  read; no existing assertion or guard is removed.
- Local results: **134 passed** (new identity tests, seam and provider),
  **279 passed** (routing, guard, acceptance, transport, instructions, recovery
  and constitution); **413 total**. `lint_no_silent_except.py` and
  `git diff --check` passed.
- CI results belong to the Draft PR's exact head. A local pass is not a claim
  that GitHub CI or production acceptance passed.

**Input delivery is proved; generated compliance is not.** The current
production UI/log correlation identifies the runtime path, but no raw model
request/response snapshot was retrieved for turn 42. The original save time
and incident-time settings revision were not reconstructed.

Final-text path reviewed: model `submit_reply.text` -> adapter candidate ->
loop verification -> reserved delivery intent -> dispatcher -> existing
WhatsApp sanitizer/guards/dedup -> wire observation -> persisted transcript.
The UI is the persisted outbound transcript and logs identify the LLM runtime;
raw candidate versus wire equality for this historical event remains
unverified. No claim is made that a sanitizer, guard or dedup could not have
transformed it. No part of that downstream path is modified here.

This Draft is not merge/deploy approval. If later authorized synthetic inference
receives the correct `assistant_name` but still uses another identity, stop at
that evidence and review the model-facing change separately; do not add a
prompt patch or text replacement to this repair.

```text
INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
TOOLS_CHANGED=NO
PRESENTATION_POLICY_CHANGED=NO
```
