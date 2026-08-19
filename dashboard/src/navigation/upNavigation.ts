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
