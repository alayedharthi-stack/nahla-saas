import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const apiSource = readFileSync(resolve(root, 'src/api/supportAccess.ts'), 'utf8')
const panelSource = readFileSync(resolve(root, 'src/components/admin/SupportAccessTargetsPanel.tsx'), 'utf8')
const pageSource = readFileSync(resolve(root, 'src/pages/AdminMerchants.tsx'), 'utf8')
const backendSource = readFileSync(resolve(root, '../backend/routers/support_access.py'), 'utf8')

function requireContract(condition: boolean, message: string): void {
  if (!condition) throw new Error(`[support-access-tenant-path] ${message}`)
}

requireContract(apiSource.includes("'/admin/support-access/targets'"), 'tenant-aware discovery endpoint is required')
requireContract(apiSource.includes("'/admin/support-access/requests'"), 'tenant-targeted request endpoint is required')
requireContract(apiSource.includes('apiCall<'), 'dashboard authenticated apiCall must be reused')
requireContract(!/getToken|localStorage|Authorization|Bearer|jwt/i.test(apiSource), 'API wrapper must not handle or expose tokens')
requireContract(!/getToken|localStorage|Authorization|Bearer|jwt/i.test(panelSource), 'operator panel must not handle or expose tokens')
requireContract(panelSource.includes('tenant_id: selected.tenant_id'), 'canonical tenant identity must be submitted')
requireContract(panelSource.includes('duration_hours: durationHours'), 'requested duration must be explicit')
requireContract(panelSource.includes('PENDING'), 'pending approval state must be visible')
requireContract(panelSource.includes('APPROVED'), 'approved state must be visible')
requireContract(panelSource.includes('REVOKED'), 'revoked state must be visible')
requireContract(panelSource.includes('EXPIRED'), 'expired state must be visible')
requireContract(pageSource.includes('<SupportAccessTargetsPanel />'), 'support console must render tenant targets')
requireContract(backendSource.includes('Depends(require_admin)'), 'backend admin authorization must remain required')
requireContract(backendSource.includes('"status": "pending"'), 'request creation must remain pending-only')
requireContract(!panelSource.includes('/merchant/access-requests/'), 'platform UI must not expose merchant approval')

console.log('support-access tenant request path contract: PASS')
