import { ChevronDown } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { useLanguage } from '../../i18n/context'

export function AdvancedSubSection(props: {
  title: string
  children: ReactNode
  lazyMount?: boolean
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(props.defaultOpen ?? false)
  const [mounted, setMounted] = useState(props.defaultOpen ?? false)

  const toggle = () => {
    setOpen(v => {
      const next = !v
      if (next && props.lazyMount) setMounted(true)
      return next
    })
  }

  return (
    <div className="border border-slate-200 rounded-xl overflow-hidden bg-white">
      <button
        type="button"
        onClick={toggle}
        className="w-full flex items-center justify-between gap-2 px-4 py-3 text-sm font-bold text-slate-800 hover:bg-slate-50 transition text-start"
        aria-expanded={open}
      >
        <span>{props.title}</span>
        <ChevronDown className={`w-4 h-4 text-slate-500 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {(props.lazyMount ? mounted : open) && (
        <div className={`px-4 pb-4 border-t border-slate-100 ${open ? '' : 'hidden'}`}>
          {props.children}
        </div>
      )}
    </div>
  )
}

export default function CatalogAdvancedSection(props: {
  children: ReactNode
  defaultOpen?: boolean
  open?: boolean
  onOpenChange?: (open: boolean) => void
}) {
  const { tStatic } = useLanguage()
  const [internalOpen, setInternalOpen] = useState(props.defaultOpen ?? false)
  const open = props.open ?? internalOpen
  const setOpen = (next: boolean) => {
    props.onOpenChange?.(next)
    if (props.open === undefined) setInternalOpen(next)
  }

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
      <button
        type="button"
        id="catalog-advanced-section"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between gap-3 px-5 py-4 text-start hover:bg-slate-50 transition"
        aria-expanded={open}
      >
        <span className="text-base font-bold text-slate-800">
          {tStatic(tr => tr.catalogMgmt.advanced.title)}
        </span>
        <ChevronDown className={`w-5 h-5 text-slate-500 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="px-5 pb-5 space-y-3 border-t border-slate-100 pt-4">
          {props.children}
        </div>
      )}
    </div>
  )
}
