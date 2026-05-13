/** iOS Safari fires ``focus`` aggressively; avoids billing / banner request storms. */
export function throttleFocusRefetch(
  minMs: number,
  getLast: () => number,
  setLast: (t: number) => void,
  run: () => void,
): void {
  const now = Date.now()
  if (now - getLast() < minMs) return
  setLast(now)
  run()
}
