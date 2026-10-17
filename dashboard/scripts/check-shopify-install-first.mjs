import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
const inline = html.match(/<script>([\s\S]*?)<\/script>/)[1]
function run(path, fails = false) {
  let url = new URL(path, 'https://app.example.test')
  const loc = {
    get pathname() { return url.pathname }, get origin() { return url.origin },
    get search() { return url.search }, get hash() { return url.hash },
    replace: () => {},
  }
  const w = { location: loc, history: { replaceState: (_s, _t, next) => {
    if (fails) throw new Error('blocked')
    url = new URL(next, url)
  } } }
  const ctx = { window: w, URLSearchParams, document: { createElement: () => ({}), head: { appendChild: () => {} } } }
  runInNewContext(inline, ctx)
  return { w, url }
}
const handle = 'h'.repeat(43)
for (const path of ['/app/shopify', '/APP/SHOPIFY/', '/app//shopify', '/app/%73hopify']) {
  const { w, url } = run(path + '#install_handle=' + handle)
  assert.equal(url.pathname, '/app/shopify'); assert.equal(url.hash, '')
  assert.equal(w.__nahlaShopifyInstallReturn().handle, handle)
  assert.equal(w.__nahlaShopifyInstallReturn, undefined)
}
const start = run('/app/shopify?shop=demo.myshopify.com&hmac=opaque')
assert.equal(start.url.search, '')
assert.equal(start.w.__nahlaShopifyInstallReturn().shop, 'demo.myshopify.com')
assert.equal(run('/app/shopify?shop=evil.test').w.__nahlaShopifyInstallReturn().shop, '')
assert.equal(run('/app/shopify?shop=a.myshopify.com&shop=b.myshopify.com').w.__nahlaShopifyInstallReturn().shop, '')
assert.equal(run('/app/shopify#install_handle=' + handle, true).w.__nahlaShopifyInstallBlocked, true)
assert.equal(run('/overview#install_handle=' + handle).url.hash, '')
const salla = run('/app/salla/launch?token=preserved#untouched')
assert.equal(salla.url.search, '?token=preserved'); assert.equal(salla.url.hash, '#untouched')
console.log('Shopify install return capture: 18 assertions passed; Salla URL preserved')
