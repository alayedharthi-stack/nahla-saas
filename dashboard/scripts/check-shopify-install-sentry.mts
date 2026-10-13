import assert from 'node:assert/strict'
import { SENTRY_SCRUB_HOOKS as hooks } from '../src/lib/sentryHooks'

const marker = 'SYNTHETIC_INSTALL_HANDOFF_MUST_NOT_LEAVE_BROWSER'
for (const url of [
  `https://app.example.test/app/shopify#install_handle=${marker}`,
  `https://app.example.test/APP/%73HOPIFY?code=${marker}`,
  `https://app.example.test/elsewhere#install%255fhandle=${marker}`,
  `install_handle=${marker}`,
  `pilot_code=${marker}`,
  `pilot_code=${marker} next=https://example.test/?q=1`,
  `pilot%255fcode=${marker} next=https://example.test/#fragment`,
  `https://app.example.test/elsewhere?pilot%255fcode=${marker}`,
]) {
  const objects = [
    hooks.beforeSendSpan({ description: url, data: { pilot_code: marker, browser: marker, totp: marker, handle: marker } } as any),
    hooks.beforeBreadcrumb({ message: url, data: { nested: { install_handle: marker } } } as any, {}),
    hooks.beforeSendTransaction({ transaction: url, spans: [{ description: url }] } as any, {}),
    hooks.beforeSend({ request: { url, data: { pilot_code: marker, browser: marker, handle: marker, totp: marker } }, extra: { install_handle: marker } } as any, {}),
  ]
  for (const object of objects) assert.equal(JSON.stringify(object).includes(marker), false)
}
console.log('PASS actual Sentry error/transaction/span/breadcrumb hooks redact install return and body secrets')
