#!/usr/bin/env npx tsx
/**
 * CI guard: flag suspicious uses of t() / tStatic() that may pass runtime data.
 *
 * Allowed:
 *   t(tr => tr.ordersPage.title)
 *   t(metaSelector)           — pre-defined selector function reference
 *   t(label)                  — NavItem.label: (tr) => string
 *
 * Forbidden:
 *   t(apiValue)
 *   t(`prefix.${x}`)
 *   t(tr => tr.section[runtimeVar])
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const ROOT = join(import.meta.dirname, '..', 'src')
const EXT = new Set(['.ts', '.tsx'])
const SKIP_FILES = new Set(['i18n/ar.ts', 'i18n/en.ts', 'i18n/tStatic.ts', 'i18n/uiOnly.ts', 'i18n/runtimeLabels.ts'])

const violations: { file: string; line: number; text: string; reason: string }[] = []

function walk(dir: string) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) {
      if (name === 'node_modules') continue
      walk(p)
      continue
    }
    if (!EXT.has(name.slice(name.lastIndexOf('.')))) continue
    const rel = relative(join(import.meta.dirname, '..'), p).replace(/\\/g, '/')
    if (SKIP_FILES.has(rel.replace(/^src\//, ''))) continue
    scanFile(rel, p)
  }
}

function stripComments(line: string): string {
  const noBlock = line.replace(/\/\*.*?\*\//g, '')
  return noBlock.replace(/\/\/.*$/, '').trim()
}

/** t(fnRef) where fnRef is a stored selector — not a runtime string. */
const FN_REF_CALL = /\bt(?:Static)?\s*\(\s*[a-zA-Z_$][\w$.]*\s*\)/

function scanFile(rel: string, path: string) {
  const lines = readFileSync(path, 'utf8').split('\n')
  lines.forEach((raw, i) => {
    if (raw.includes('i18n-static: allow')) return
    const trimmed = raw.trim()
    if (trimmed.startsWith('//') || trimmed.startsWith('*')) return
    const line = stripComments(raw)
    if (!line) return
    const n = i + 1

    const hasT = /\bt(?:Static)?\s*\(/.test(line)
    if (!hasT) return

    const inlineSelector = /\bt(?:Static)?\s*\(\s*tr\s*=>/.test(line)
    const fnRef = FN_REF_CALL.test(line)

    if (!inlineSelector && !fnRef) {
      if (/setTimeout|clearTimeout|performance\.now|split\s*\(/.test(line)) return
      if (/const tr = t\(/.test(line)) return
      violations.push({ file: rel, line: n, text: raw.trim(), reason: 't() must use tr => selector or a stored selector function' })
    }

    if (/\bt(?:Static)?\s*\(\s*`/.test(line)) {
      violations.push({ file: rel, line: n, text: raw.trim(), reason: 'template literal passed to t()' })
    }

    if (/\bt(?:Static)?\s*\(\s*tr\s*=>\s*tr\.[^)]*\[[^'"]/.test(line)) {
      violations.push({ file: rel, line: n, text: raw.trim(), reason: 'dynamic key inside t() selector — map enum key via runtimeLabels helper' })
    }
  })
}

walk(ROOT)

if (violations.length) {
  console.error(`\n[i18n-static] ${violations.length} suspicious t() usage(s):\n`)
  for (const v of violations) {
    console.error(`  ${v.file}:${v.line} — ${v.reason}`)
    console.error(`    ${v.text}\n`)
  }
  process.exit(1)
}

console.log('[i18n-static] OK — no suspicious dynamic t() patterns found.')
