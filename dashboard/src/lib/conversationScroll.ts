export interface ScrollMetrics {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}

export const DEFAULT_NEAR_BOTTOM_PX = 80

export function isConversationNearBottom(
  metrics: ScrollMetrics,
  threshold = DEFAULT_NEAR_BOTTOM_PX,
): boolean {
  return metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight <= threshold
}

export function preservedHistoryScrollTop({
  previousScrollTop,
  previousScrollHeight,
  nextScrollHeight,
}: {
  previousScrollTop: number
  previousScrollHeight: number
  nextScrollHeight: number
}): number {
  return Math.max(0, previousScrollTop + (nextScrollHeight - previousScrollHeight))
}

export function shouldAutoScrollForNewMessage({
  wasNearBottom,
  operatorPausedAutoScroll,
}: {
  wasNearBottom: boolean
  operatorPausedAutoScroll: boolean
}): boolean {
  return wasNearBottom && !operatorPausedAutoScroll
}

export function shouldShowJumpToLatest({
  nearBottom,
  operatorPausedAutoScroll,
}: {
  nearBottom: boolean
  operatorPausedAutoScroll: boolean
}): boolean {
  return !nearBottom && operatorPausedAutoScroll
}

/**
 * Keep an initial open pinned to the latest message while fonts/images settle.
 * The observer watches the content element (not the fixed-height scroller), so
 * lazy media dimensions trigger another exact bottom positioning pass.
 */
export function pinConversationToLatestAfterLayout(
  scroller: HTMLElement,
  content: HTMLElement | null,
  opts: { settleMs?: number } = {},
): () => void {
  let cancelled = false
  let frameOne = 0
  let frameTwo = 0
  const scroll = () => {
    if (!cancelled) scroller.scrollTop = scroller.scrollHeight
  }
  frameOne = requestAnimationFrame(() => {
    scroll()
    frameTwo = requestAnimationFrame(scroll)
  })
  const observer = typeof ResizeObserver !== 'undefined' && content
    ? new ResizeObserver(scroll)
    : null
  if (observer && content) observer.observe(content)
  const timer = window.setTimeout(() => observer?.disconnect(), opts.settleMs ?? 2_000)
  return () => {
    cancelled = true
    cancelAnimationFrame(frameOne)
    cancelAnimationFrame(frameTwo)
    window.clearTimeout(timer)
    observer?.disconnect()
  }
}
