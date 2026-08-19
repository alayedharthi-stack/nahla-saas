const RESTORE_PREFIX = 'nahla.nav.restore:'

export function recordLocationContext(pathname: string, search: string): void {
  try {
    sessionStorage.setItem(`${RESTORE_PREFIX}${pathname}`, search || '')
  } catch {
    /* private mode / quota */
  }
}

export function parentHref(parentPath: string): string {
  try {
    const saved = sessionStorage.getItem(`${RESTORE_PREFIX}${parentPath}`) || ''
    return saved ? `${parentPath}${saved}` : parentPath
  } catch {
    return parentPath
  }
}

/**
 * History back is allowed only when it would stay inside Nahla and land on
 * the canonical parent (or a deeper URL under that parent). Direct URL,
 * email, bookmark, refresh, and new-tab entry must use parentHref instead.
 */
export function shouldUseHistoryBack(opts: {
  historyIdx: unknown
  referrer: string
  currentOrigin: string
  parentPath: string
}): boolean {
  if (typeof opts.historyIdx !== 'number' || opts.historyIdx < 1) return false
  if (!opts.referrer) return false
  try {
    const ref = new URL(opts.referrer)
    if (ref.origin !== opts.currentOrigin) return false
    const refPath = ref.pathname.replace(/\/$/, '') || '/'
    const parent = opts.parentPath.replace(/\/$/, '') || '/'
    return refPath === parent || refPath.startsWith(`${parent}/`)
  } catch {
    return false
  }
}
