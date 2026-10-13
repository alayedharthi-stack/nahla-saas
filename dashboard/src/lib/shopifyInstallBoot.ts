// Evaluated before auth, analytics and router. index.html performs the first scrub.
type InstallReturn = { shop: string; handle: string; error: boolean }
let captured: InstallReturn = { shop: '', handle: '', error: false }
const w = window as unknown as Record<string, unknown>
if (w.__nahlaShopifyInstallBlocked) throw new Error('shopify_install_scrub_failed')
const early = w.__nahlaShopifyInstallReturn
if (typeof early === 'function') captured = early() as InstallReturn
// Also verify cleanup when an inline script was blocked or did not run.
let path = window.location.pathname
try { path = decodeURIComponent(path) } catch { /* compare raw */ }
const onPath = path.toLowerCase().replace(/\/{2,}/g, '/').replace(/\/+$/, '') === '/app/shopify'
if (onPath || /install(?:_|%5f|%255f)(?:handle|error)/i.test(window.location.search + window.location.hash)) {
  const target = onPath ? '/app/shopify' : window.location.origin + window.location.pathname
  try { window.history.replaceState(null, '', target) } catch { /* verified below */ }
  if (window.location.search || window.location.hash || (onPath && window.location.pathname !== '/app/shopify')) {
    try { window.location.replace(target) } catch { /* boot still stops */ }
    throw new Error('shopify_install_scrub_failed')
  }
}
export const installReturn = captured
