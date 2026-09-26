# Unsubscribe confirmation blocked by store AI test mode

## Evidence and first divergence

The incident was verified against the affected conversation’s production logs.
Customer, conversation, tenant, phone and infrastructure identifiers are
intentionally omitted from this repository report.

The live inbound `الغاء` reached the correct webhook and tenant. The existing
keyword recognizer persisted PENDING_UNSUBSCRIBE. The first divergence was
`_post_wa` rejecting both the existing interactive confirmation and its plain
text fallback with `store_ai_test_mode_not_allowed`. Logs then incorrectly
said `(sent confirmation)` even though both calls returned false.

Source of truth: production inbound/transition/send-gate logs, not the visible
WhatsApp screenshot alone. The screenshot corroborates absent confirmation.
This is an execution/routing defect, not model behavior or keyword detection.
The customer is observed pending, not finally unsubscribed. Existing pending
expiry remains unchanged; no claim of indefinite suppression is made.

## Repair

The four existing system consent payloads receive an explicit, closed wire
classification. Full payload equality is validated before the webhook send
and again at the provider boundary. Arbitrary prose, changed buttons and extra
fields cannot use it. It does not call the AI or use the manual-send bypass.

Consent notices use the existing non-AI campaign recipient safety check:
all sibling manual pauses, blocked numbers and safety lookup failures still
block. AI store off/test settings continue to block ordinary AI sends, but no
longer swallow these operational notices. Existing quota, sanitizer, rate
limits, outbound dedup, capture/egress isolation and provider routing remain.
No settings, allowlists, campaign queues or customer records were changed.

Pending logs report the actual confirmation send result. Prompt timestamps and
outbound messages are recorded only when the prompt or fallback succeeds.

## Text provenance / constitution review

Decision: existing unsubscribe state machine -> existing system consent payload
builders -> exact payload validation -> recipient safety and quota -> existing
sanitizer/dedup -> provider validation -> wire. Wording and keyword recognition
are unchanged. The source is the existing system consent notice, covered by
AGENTS.md exact consent-notice exception #6, not an LLM answer. No normal AI
candidate is generated or replaced. The text fallback is the existing alternate
consent transport, not a natural-compose emergency fallback. This change adds
no prose templates and no governance exception/waiver.

## Verification scope

`tests/test_unsubscribe_notice_delivery.py` exercises the real webhook send and
provider layers with only external transport/token/persistence dependencies
mocked. It covers off/test modes, four notice payloads, ordinary AI suppression,
explicit pause, blocklist, quota, safety read failure, malicious payload changes,
provider rejection, and the actual nested webhook prompt helper's fallback and
persistence behavior. Generic tenant 77 / generic merchant and synthetic phone
are used. No evaluation model or customer messages are sent by tests.

Existing unsubscribe, AI kill-switch, automation guard, campaign send policy,
interactive dedup and constitution tests are retained, not weakened.

Post-fix production delivery is not proven by local tests or CI. Deployment and an
actual authorized confirmation must be verified separately. This patch does
not replay the historical inbound or send a retroactive customer message.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Local result: 125 passed across the new consent-wire regression, existing
unsubscribe flow, AI kill-switch and constitution suite. The supplementary
legacy `backend/tests/test_automation_send_guard.py` run reaches a pre-existing
failure in `TestProviderSendAutomationGuard.test_ai_paused_customer_does_not_post`:
its mocked connection enters the unsupported-provider path before recipient
checks. Reproduced unchanged at clean main `0a737077` (1 failed in isolation).
No test was weakened or suppressed. New tests use an explicit supported Meta
connection and prove paused/blocked recipients cannot reach provider transport.
