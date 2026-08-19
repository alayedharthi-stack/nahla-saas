/**
 * Route hierarchy + Up navigation contract.
 *
 * Run from dashboard/: npm run check:route-hierarchy
 */
import { readFileSync } from 'node:fs'
import {
  listDeepChildPaths,
  listExactChildPaths,
  listTopLevelPaths,
  resolveRouteHierarchy,
} from '../src/navigation/routeHierarchy.ts'
import { parentHref, recordLocationContext, shouldUseHistoryBack } from '../src/navigation/upNavigation.ts'

function assert(cond: boolean, message: string) {
  if (!cond) {
    console.error(message)
    process.exit(1)
  }
}

const child = resolveRouteHierarchy('/intelligence')
assert(child.showBack === true, 'intelligence must show Back')
assert(child.parentPath === '/settings-hub', `intelligence parent got ${child.parentPath}`)
assert(child.kind === 'child', 'intelligence must be child')

const ltrParent = resolveRouteHierarchy('/whatsapp-connect')
assert(ltrParent.parentPath === '/channels', 'whatsapp-connect parent is channels hub')

const top = resolveRouteHierarchy('/settings-hub')
assert(top.showBack === false, 'settings-hub is top-level and must not show Back')
assert(top.kind === 'top_level', 'settings-hub kind')

const order = resolveRouteHierarchy('/orders/42')
assert(order.kind === 'deep_child', 'order detail is deep_child')
assert(order.parentPath === '/orders', 'order detail parent is /orders not home')

const branch = resolveRouteHierarchy('/sales-channels/branches/7')
assert(branch.parentPath === '/sales-channels/branches', 'branch detail must not skip to home')
assert(resolveRouteHierarchy('/sales-channels/branches').parentPath === '/sales-channels', 'branches list parent')

assert(resolveRouteHierarchy('/overview').showBack === false, 'overview has no Back')
assert(resolveRouteHierarchy('/conversations').showBack === false, 'conversations has no Back')

assert(
  resolveRouteHierarchy('/settings/security').parentPath === '/settings-hub',
  'security settings parent',
)

assert(
  resolveRouteHierarchy('/customers/import').kind === 'deep_child',
  'customer import is deep_child',
)

assert(
  shouldUseHistoryBack({
    historyIdx: 0,
    referrer: 'https://app.nahlah.ai/settings-hub',
    currentOrigin: 'https://app.nahlah.ai',
    parentPath: '/settings-hub',
  }) === false,
  'direct URL / idx 0 must not use history.back',
)

assert(
  shouldUseHistoryBack({
    historyIdx: 2,
    referrer: 'https://mail.example/message',
    currentOrigin: 'https://app.nahlah.ai',
    parentPath: '/settings-hub',
  }) === false,
  'external referrer must not use history.back',
)

assert(
  shouldUseHistoryBack({
    historyIdx: 3,
    referrer: 'https://app.nahlah.ai/settings-hub',
    currentOrigin: 'https://app.nahlah.ai',
    parentPath: '/settings-hub',
  }) === true,
  'in-app parent referrer may use history.back',
)

const mem = globalThis as typeof globalThis & { sessionStorage?: Storage }
const store = new Map<string, string>()
mem.sessionStorage = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => { store.set(k, v) },
  removeItem: (k: string) => { store.delete(k) },
  clear: () => store.clear(),
  key: () => null,
  length: 0,
} as Storage

recordLocationContext('/orders', '?status=paid&page=3')
assert(parentHref('/orders') === '/orders?status=paid&page=3', 'restore parent search/filters')

const header = readFileSync(new URL('../src/components/layout/Header.tsx', import.meta.url), 'utf8')
assert(header.includes('BackNavigation'), 'Header must own the shared Up control')
assert(!header.includes('history.back('), 'Header must not call history.back directly')

const backNav = readFileSync(
  new URL('../src/components/ui/BackNavigation.tsx', import.meta.url),
  'utf8',
)
assert(backNav.includes('rtl:-scale-x-100'), 'arrow must flip with dir=rtl')
assert(backNav.includes('data-testid="platform-up-nav"'), 'up nav must be testable')
assert(backNav.includes('hidden lg:flex'), 'breadcrumb is desktop-only')
assert(!backNav.includes('history.back'), 'Up control must not call history.back()')

const modalHint = readFileSync(
  new URL('../src/navigation/routeHierarchy.ts', import.meta.url),
  'utf8',
)
assert(modalHint.includes('Modals/sheets are not routes'), 'modals stay Close, not Back')

const parameterizedDeep = [
  '/orders/42',
  '/sales-channels/branches/7',
  '/operations-center/branches/3',
  '/admin/salla/diagnose/9',
]
assert(parameterizedDeep.every(p => resolveRouteHierarchy(p).kind === 'deep_child'), 'parameterized deep children')

const childCount = listExactChildPaths().filter(p => resolveRouteHierarchy(p).kind === 'child').length
const deepExact = listExactChildPaths().filter(p => resolveRouteHierarchy(p).kind === 'deep_child').length

console.log(
  JSON.stringify({
    top_level_count: listTopLevelPaths().length,
    child_count: childCount,
    deep_child_count: deepExact + parameterizedDeep.length,
  }),
)
console.log('route-hierarchy: OK')
