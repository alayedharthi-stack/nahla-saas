import { ChevronLeft, ChevronRight, type LucideIcon } from 'lucide-react'

/** Chevron direction follows reading order for prev/next pagination. */
export function paginationChevrons(isRTL: boolean): { Prev: LucideIcon; Next: LucideIcon } {
  return isRTL
    ? { Prev: ChevronRight, Next: ChevronLeft }
    : { Prev: ChevronLeft, Next: ChevronRight }
}
