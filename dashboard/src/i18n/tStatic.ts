/**
 * Static UI translation accessor.
 *
 * ONLY pass compile-time-known label selectors through `tStatic`.
 * NEVER pass merchant/customer/API runtime values (names, messages,
 * product titles, order notes, backend error labelAr, etc.).
 *
 * @example
 * // GOOD — fixed key path
 * tStatic(tr => tr.ordersPage.table.order)
 *
 * @example
 * // BAD — never do this
 * tStatic(tr => tr.ordersPage[dynamicKey])
 * t(order.product_name)
 */
import type { Translations } from './types'

export type StaticLabelSelector<T = string> = (tr: Translations) => T

/** Dev-only marker type — runtime data must not flow here. */
export type StaticUiLabel<T = string> = T & { readonly __staticUi?: unique symbol }

export function createTStatic(getTr: () => Translations) {
  return function tStatic<T>(selector: StaticLabelSelector<T>): T {
    if (import.meta.env.DEV) {
      const src = selector.toString()
      if (/\$\{/.test(src)) {
        console.warn(
          '[i18n] tStatic selector contains template interpolation — only static UI keys are allowed.',
          src.slice(0, 120),
        )
      }
    }
    return selector(getTr())
  }
}
