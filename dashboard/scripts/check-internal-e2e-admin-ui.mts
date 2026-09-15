import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { internalE2EStopReasons } from '../src/lib/internalE2ESafety.ts'

const root = fileURLToPath(new URL('..', import.meta.url))
const page = readFileSync(`${root}/src/pages/AdminInternalE2E.tsx`, 'utf8')
const api = readFileSync(`${root}/src/api/internalE2E.ts`, 'utf8')
const app = readFileSync(`${root}/src/App.tsx`, 'utf8')
const guard = readFileSync(`${root}/src/components/ProtectedRoute.tsx`, 'utf8')
const client = readFileSync(`${root}/src/api/client.ts`, 'utf8')
const auth = readFileSync(`${root}/src/auth.ts`, 'utf8')
const conversations = readFileSync(`${root}/src/pages/Conversations.tsx`, 'utf8')
const featureApi = readFileSync(`${root}/src/api/featureReality.ts`, 'utf8')
const orders = readFileSync(`${root}/src/components/conversations/CustomerOrdersDrawer.tsx`, 'utf8')

assert.match(api, /INTERNAL_E2E_ALIASES[^=]*= \['A', 'B', 'C'\]/)
assert.match(app, /path="admin\/internal-e2e" element=\{<AdminInternalE2E \/>\}/)
assert.match(guard, /wantsAdmin && !isOwner/)
assert.match(guard, /<Navigate to="\/overview" replace \/>/)
assert.match(guard, /location\.pathname === '\/admin\/internal-e2e' && isImpersonatingSupport\(\)/)
assert.match(api, /authScope: 'platform-admin'/)
assert.match(client, /getPlatformAdminSessionToken\(\)/)
assert.match(auth, /if \(!isImpersonatingSupport\(\)\) return null/)
assert.match(auth, /return isPlatformStaffRole\(role\) \? saved : null/)
assert.match(page, /data-internal-e2e-disabled/)
assert.match(page, /controlsLocked = !enabled \|\| stopReasons\.length > 0/)
assert.match(page, /data-auto-turn-form/)
assert.match(page, /value="auto" disabled/)
assert.match(page, /data-safety-stop/)
assert.match(page, /data-result-lookup/)
assert.match(api, /service_tier: 'auto'/)
assert.match(api, /\/admin\/internal-e2e\/fixtures\/provision/)
assert.match(api, /\/admin\/internal-e2e\/turns/)
assert.match(api, /\/admin\/internal-e2e\/results/)
assert.doesNotMatch(`${page}\n${api}`, /\/admin\/internal-e2e\/batches/)
assert.doesNotMatch(page, /Run 180|180-turn|batch/i)
assert.doesNotMatch(page, /getToken|localStorage|Authorization|Bearer|JWT/)
assert.doesNotMatch(api, /getToken|localStorage|Authorization|Bearer|JWT/)
assert.doesNotMatch(page, /endpoint/i)
assert.doesNotMatch(page, /<(?:input|select|textarea)[^>]*(?:name=["']tenant|tenantId|tenant_id)/)
assert.match(page, /data-synthetic-conversation-links/)
assert.match(page, /synthetic_conversation_id=\$\{conversationId\}/)
assert.match(page, /isImpersonatingSupport\(\)/)
assert.match(featureApi, /\/conversations\/internal-e2e\/\$\{encodeURIComponent\(String\(conversationId\)\)\}/)
assert.match(conversations, /requestedSyntheticConversationId/)
assert.match(conversations, /Synthetic alias/)
assert.match(conversations, /selected\.syntheticIdentifier/)
assert.match(conversations, /!selected\.synthetic/)
assert.match(orders, /internalE2EConversationOrders/)
assert.doesNotMatch(`${page}\n${conversations}\n${featureApi}`, /getToken\(|Authorization|Bearer/)

const provenZero = { value: 0, proven: true }
const safe = {
  external_egress_count: 0,
  cross_tenant_leakage: 0,
  cross_customer_leakage: 0,
  write_mutations: 0,
  salla_mutations: 0,
  unsupported_commercial_claims: 0,
  duplicate_replies: 0,
  silent_v1_fallback: 0,
  guardrail_passed: true,
  status: 'completed',
  safety_proofs: {
    cross_tenant_leakage: provenZero,
    cross_customer_leakage: provenZero,
    write_mutations: provenZero,
    salla_mutations: provenZero,
    unsupported_commercial_claims: provenZero,
    duplicate_replies: provenZero,
    silent_v1_fallback: provenZero,
  },
}
assert.deepEqual(internalE2EStopReasons(safe), [])
assert.deepEqual(internalE2EStopReasons({ ...safe, external_egress_count: 1 }), ['external_egress_count'])
assert(internalE2EStopReasons({ ...safe, unsupported_commercial_claims: 1 }).includes('unsupported_commercial_claims'))
assert(internalE2EStopReasons({ ...safe, duplicate_replies: 1 }).includes('duplicate_replies'))
assert(internalE2EStopReasons({ ...safe, guardrail_passed: false }).includes('guardrail_failed'))
assert(internalE2EStopReasons({ ...safe, status: 'test_contract_failed' }).includes('turn_not_completed'))
assert(internalE2EStopReasons({
  ...safe,
  safety_proofs: { ...safe.safety_proofs, cross_customer_leakage: { value: 0, proven: false } },
}).includes('cross_customer_leakage_unproven'))

console.log('INTERNAL_E2E admin operator UI contract: ok')
