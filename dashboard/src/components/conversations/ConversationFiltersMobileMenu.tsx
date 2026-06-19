import { useEffect, useRef } from 'react'
import { ChevronDown, X } from 'lucide-react'

import {
  CONVERSATION_FILTER_KEYS,
  conversationFilterActiveClass,
  conversationFilterIcon,
  conversationFilterInactiveClass,
  resolveConversationFilterCount,
  type ConversationFilter,
  type ConversationFilterHelpers,
} from './conversationFilterConfig'
import type { DashboardConversation } from '../../api/featureReality'

export interface ConversationFiltersMobileMenuProps {
  dir: string
  open: boolean
  onOpenChange: (open: boolean) => void
  activeFilter: ConversationFilter
  filterLabels: Record<ConversationFilter, string>
  filterCounts?: Partial<Record<ConversationFilter, number>> | null
  conversations: DashboardConversation[]
  helpers: ConversationFilterHelpers
  menuButtonLabel: string
  sheetTitle: string
  onSelect: (filter: ConversationFilter) => void
}

export default function ConversationFiltersMobileMenu({
  dir,
  open,
  onOpenChange,
  activeFilter,
  filterLabels,
  filterCounts,
  conversations,
  helpers,
  menuButtonLabel,
  sheetTitle,
  onSelect,
}: ConversationFiltersMobileMenuProps) {
  const sheetRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onOpenChange(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onOpenChange])

  const triggerClass =
    activeFilter === 'campaign_excluded'
      ? 'border-violet-300 bg-violet-50 text-violet-800'
      : 'border-slate-200 bg-white text-slate-700'

  return (
    <>
      <button
        type="button"
        onClick={() => onOpenChange(true)}
        className={`md:hidden w-full flex items-center justify-between gap-2 px-3 py-2.5 rounded-xl border text-sm font-medium transition-colors ${triggerClass}`}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <span className="flex flex-col items-start min-w-0 text-start">
          <span className="truncate w-full">{sheetTitle}</span>
          {activeFilter !== 'all' && (
            <span className={`text-xs truncate w-full ${
              activeFilter === 'campaign_excluded' ? 'text-violet-600' : 'text-slate-500'
            }`}>
              {filterLabels[activeFilter]}
            </span>
          )}
        </span>
        <ChevronDown className={`w-4 h-4 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="md:hidden fixed inset-0 z-50 flex flex-col justify-end">
          <button
            type="button"
            className="absolute inset-0 bg-slate-900/40"
            aria-label={menuButtonLabel}
            onClick={() => onOpenChange(false)}
          />
          <div
            ref={sheetRef}
            role="listbox"
            aria-label={sheetTitle}
            dir={dir}
            className="relative bg-white rounded-t-2xl shadow-xl border-t border-slate-200 max-h-[min(70dvh,520px)] flex flex-col"
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-slate-100 shrink-0">
              <h3 className="text-sm font-semibold text-slate-900">{sheetTitle}</h3>
              <button
                type="button"
                onClick={() => onOpenChange(false)}
                className="p-2 rounded-full hover:bg-slate-100 text-slate-500"
                aria-label="Close"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="overflow-y-auto overscroll-contain py-2 px-2">
              {CONVERSATION_FILTER_KEYS.map((f) => {
                const count = resolveConversationFilterCount(
                  f, filterCounts, conversations, helpers,
                )
                const isActive = activeFilter === f
                const rowAccent =
                  f === 'campaign_excluded'
                    ? (isActive
                      ? 'bg-violet-600 text-white'
                      : 'text-violet-800 hover:bg-violet-50')
                    : (isActive
                      ? conversationFilterActiveClass(f)
                      : conversationFilterInactiveClass(f, count ?? 0))

                return (
                  <button
                    key={f}
                    type="button"
                    role="option"
                    aria-selected={isActive}
                    onClick={() => {
                      onSelect(f)
                      onOpenChange(false)
                    }}
                    className={`w-full flex items-center justify-between gap-3 px-3 py-3 rounded-xl text-sm font-medium text-start transition-colors mb-1 ${rowAccent}`}
                  >
                    <span className="flex items-center min-w-0">
                      {conversationFilterIcon(f)}
                      <span className="truncate">{filterLabels[f]}</span>
                    </span>
                    {f !== 'all' && count != null && count > 0 && (
                      <span className={`shrink-0 text-xs font-semibold px-2 py-0.5 rounded-full ${
                        isActive
                          ? 'bg-white/20 text-inherit'
                          : f === 'campaign_excluded'
                            ? 'bg-violet-100 text-violet-700'
                            : 'bg-slate-100 text-slate-600'
                      }`}>
                        {count}
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
