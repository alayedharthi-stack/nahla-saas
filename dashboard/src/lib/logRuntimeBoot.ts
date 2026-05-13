/**
 * One-line boot marker so QA can confirm JS/CSS/SW all match the expected deploy.
 * Printed once on app startup (see ``main.tsx``).
 */
export function logNahlaRuntimeBoot(): void {
  if (typeof window === 'undefined') return
  try {
    const stamp =
      typeof __NAHLA_BUILD_STAMP__ === 'string' && __NAHLA_BUILD_STAMP__.length > 0
        ? __NAHLA_BUILD_STAMP__
        : import.meta.env.VITE_BUILD_STAMP || import.meta.env.MODE || 'unknown'
    // eslint-disable-next-line no-console
    console.info(
      '%c[nahla] runtime',
      'color:#d97706;font-weight:bold;',
      JSON.stringify({
        build_stamp: stamp,
        vite_mode: import.meta.env.MODE,
        prod: !!import.meta.env.PROD,
        href: window.location.href,
        sw_supported: 'serviceWorker' in navigator,
      }),
    )
  } catch {
    /* ignore */
  }
}
