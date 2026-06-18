/**
 * Guard: dashboard must not use native alert/confirm/prompt.
 *
 * Run: npm run check:no-native-dialogs   (from dashboard/)
 *
 * Legacy pages may remain in ALLOWLIST until migrated to ConfirmModal.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const ROOT = join(import.meta.dirname, '..', 'src')

const ALLOWLIST = new Set([
  'pages/Merchants.tsx',
  'pages/Intelligence.tsx',
  'pages/AdminWebhookHealth.tsx',
  'pages/StoreIntegration.tsx',
  'pages/Settings.tsx',
  'pages/SallaEntryScreen.tsx',
  'pages/AdminMerchants.tsx',
  'pages/AdminTenants.tsx',
  'pages/Customers.tsx',
  'pages/Conversations.tsx',
  'pages/IntelligenceLibraries.tsx',
  'pages/Campaigns.tsx',
  'pages/AdminTenantIntegrity.tsx',
  'pages/Promotions.tsx',
  'pages/ProductStudio.tsx',
  'pages/AdminTools.tsx',
  'pages/Coupons.tsx',
  'components/ui/AppStoreBadges.tsx',
  'components/customers/CampaignExcludeControl.tsx',
])

const PATTERNS: Array<{ name: string; re: RegExp; skip?: RegExp }> = [
  { name: 'window.confirm', re: /\bwindow\.confirm\s*\(/ },
  { name: 'window.alert', re: /\bwindow\.alert\s*\(/ },
  { name: 'window.prompt', re: /\bwindow\.prompt\s*\(/ },
  {
    name: 'bare confirm(',
    re: /^\s*confirm\s*\(/,
    skip: /^\s*void\s+confirm\s*\(/,
  },
  { name: 'bare alert(', re: /^\s*alert\s*\(/ },
  { name: 'bare prompt(', re: /^\s*prompt\s*\(/ },
]

function isCommentLine(line: string): boolean {
  const t = line.trimStart()
  return (
    t.startsWith('//')
    || t.startsWith('*')
    || t.startsWith('/**')
    || t.includes('{/*')
    || /\*\/\s*$/.test(t)
  )
}

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name === 'node_modules') continue
      walk(path, out)
      continue
    }
    if (/\.(tsx?|jsx?)$/.test(name)) out.push(path)
  }
  return out
}

let failed = 0

for (const file of walk(ROOT)) {
  const rel = relative(ROOT, file).replace(/\\/g, '/')
  const text = readFileSync(file, 'utf8')
  const lines = text.split(/\r?\n/)

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (isCommentLine(line)) continue

    for (const { name, re, skip } of PATTERNS) {
      if (!re.test(line)) continue
      if (skip?.test(line)) continue
      if (ALLOWLIST.has(rel)) continue
      failed++
      console.error(`FAIL ${rel}:${i + 1} — native dialog ${name}`)
      console.error(`     ${line.trim()}`)
    }
  }
}

if (failed > 0) {
  console.error(`\n${failed} native dialog usage(s) outside allowlist`)
  process.exit(1)
}

console.log('OK   no native alert/confirm/prompt outside allowlist')
