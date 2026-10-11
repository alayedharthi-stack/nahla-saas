/**
 * Browser signals after which the session may have changed: another tab
 * logged in / out or switched tenant (``storage``), or this tab came back to
 * the foreground after a refresh loop tick (``focus`` / ``visibilitychange``).
 * Controllers compare the session binding key themselves; this only wakes them.
 */
export function watchSessionSignals(onSignal: () => void): () => void {
  const onVisibility = () => {
    if (document.visibilityState === 'visible') onSignal()
  }
  window.addEventListener('storage', onSignal)
  window.addEventListener('focus', onSignal)
  document.addEventListener('visibilitychange', onVisibility)
  return () => {
    window.removeEventListener('storage', onSignal)
    window.removeEventListener('focus', onSignal)
    document.removeEventListener('visibilitychange', onVisibility)
  }
}
