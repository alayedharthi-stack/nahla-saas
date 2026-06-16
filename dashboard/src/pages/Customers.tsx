import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Search,
  UserPlus,
  RefreshCw,
  Users,
  Crown,
  AlertTriangle,
  ShoppingCart,
  X,
  Phone,
  Mail,
  User,
  Upload,
  Info,
  Trash2,
  CheckSquare,
  Square,
  BellOff,
  Megaphone,
  Tag,
  Beaker,
  Plus,
  Minus,
  RotateCcw,
  Filter,
  Sparkles,
  ShieldCheck,
  Loader2,
  Save,
  SkipForward,
  Check,
  Pencil,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import ToggleSwitch from '../components/ui/ToggleSwitch'
import CampaignExcludeControl from '../components/customers/CampaignExcludeControl'
import { useLanguage } from '../i18n/context'
import { UI_ONLY_GUARD } from '../i18n/uiOnly'
import { paginationChevrons } from '../lib/paginationIcons'
import type { Lang } from '../i18n/types'
import type { CustomersPageLabels } from '../i18n/customersPageLabels'
import {
  customersApi,
  type CustomerRecord,
  type CustomerSegmentMeta,
  type NameCleanupCategory,
  type NameCleanupPreviewItem,
} from '../api/customers'

// ── Name-cleanup modal state shapes ──────────────────────────────────
//
// One ``RowState`` per customer the modal is currently showing. The
// chip state lives here in-memory; autosave dribbles diffs out to the
// backend so closing the modal mid-review never loses work.
type CleanupRowStatus = 'edited' | 'skipped'

interface CleanupRowState {
  /** Token indices (into ``current_name`` split on whitespace) that
   *  the merchant has flipped OFF. ``null`` means "use the cleaner's
   *  default removal set" — equivalent to "merchant hasn't touched
   *  this row's words yet". */
  removed: number[] | null
  /** Force-clear flag (the "مسح الاسم بالكامل" button). Wins over
   *  individual word toggles. */
  cleared: boolean
  /** Skipped rows are filtered out of the default view so the
   *  merchant doesn't see them again on the next session. */
  status: CleanupRowStatus
  /** True when the row has unsaved local edits — the autosave loop
   *  uses this to decide what to ship next. Cleared on save success. */
  dirty: boolean
  /** When the backend last confirmed the row. ``null`` means the
   *  row state lives only in the browser so far. */
  savedAt: string | null
}

type CleanupFilter =
  | 'all'
  | 'pending'
  | 'edited'
  | 'high'
  | 'low'
  /** Show ONLY customers the merchant flagged as
   *  "exclude from marketing" inline during this review. Useful for
   *  reviewing the merchant's own exclusion list before applying. */
  | 'opted_out'
type CleanupSaveState = 'idle' | 'saving' | 'saved' | 'error'

const NAME_CLEANUP_AUTOSAVE_DEBOUNCE_MS = 1200

function segmentVariant(
  seg: string,
): 'green' | 'amber' | 'red' | 'blue' | 'slate' {
  if (seg === 'lead') return 'blue'
  if (seg === 'active') return 'green'
  if (seg === 'vip') return 'amber'
  if (seg === 'at_risk') return 'red'
  if (seg === 'inactive') return 'slate'
  return 'blue'
}

function rfmVariant(score: number): 'green' | 'amber' | 'red' | 'blue' | 'slate' {
  if (score >= 12) return 'green'
  if (score >= 8) return 'amber'
  if (score >= 4) return 'blue'
  return 'slate'
}

function customerLocale(lang: Lang): string {
  return lang === 'ar' ? 'ar-SA' : 'en-US'
}

function segmentDisplayLabel(seg: CustomerSegmentMeta, lang: Lang): string {
  return lang === 'en' ? (seg.label_en || seg.label_ar) : seg.label_ar
}

function formatCustomerDate(dateStr: string | null, locale: string): string {
  if (!dateStr) return '—'
  try {
    return new Date(dateStr).toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' })
  } catch { return '—' }
}

export default function Customers() {
  void UI_ONLY_GUARD
  const { tStatic, dir, lang, isRTL } = useLanguage()
  const cu = tStatic(tr => tr.customersPage)
  const locale = customerLocale(lang)
  const { Prev: PrevIcon, Next: NextIcon } = paginationChevrons(isRTL)
  const navigate = useNavigate()

  const [customers, setCustomers] = useState<CustomerRecord[]>([])
  const [metrics, setMetrics] = useState<{
    totalCustomers: number
    activeCustomers: number
    vipCustomers: number
    newCustomers: number
    atRiskCustomers: number
    inactiveCustomers: number
    leads: number
  } | null>(null)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [search, setSearch] = useState('')
  const [segmentKey, setSegmentKey] = useState<string>('all')
  // Manual segment filter — separate axis from `segmentKey` so a
  // merchant can combine "auto VIP" with "manually tagged unsubscribed"
  // (or "no manual tag at all" via the special 'none' value).
  const [manualSegmentKey, setManualSegmentKey] = useState<string>('')
  const [marketingOptOutFilter, setMarketingOptOutFilter] = useState<'all' | 'out'>('all')
  const [segments, setSegments] = useState<CustomerSegmentMeta[]>([])
  const [segmentsLoading, setSegmentsLoading] = useState(true)
  const [loading, setLoading] = useState(true)
  const [showAdd, setShowAdd] = useState(false)
  const [addName, setAddName] = useState('')
  const [addPhone, setAddPhone] = useState('')
  const [addEmail, setAddEmail] = useState('')
  const [addError, setAddError] = useState('')
  const [addLoading, setAddLoading] = useState(false)
  const [selectedCustomer, setSelectedCustomer] = useState<CustomerRecord | null>(null)

  // Selection & bulk delete
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [deleteModal, setDeleteModal] = useState<'selected' | 'all' | null>(null)
  const [deleteConfirmText, setDeleteConfirmText] = useState('')
  const [deleteLoading, setDeleteLoading] = useState(false)

  // Name-cleanup tool (the "تنظيف أسماء العملاء" modal).
  //
  // Lives in this component because the preview is opt-in and we don't
  // want to add cost to the main customers list load. State shape:
  //   * ``nameCleanupOpen``        — is the modal mounted
  //   * ``nameCleanupLoading``     — fetching the preview
  //   * ``nameCleanupApplying``    — POSTing the apply call
  //   * ``nameCleanupItems``       — the candidate list (only changed rows)
  //   * ``nameCleanupSelected``    — per-row toggle (default = ON for high
  //                                  confidence, OFF for low)
  //   * ``nameCleanupSummary``     — counts from the preview response
  //   * ``nameCleanupResult``      — success banner after apply
  const [nameCleanupOpen, setNameCleanupOpen] = useState(false)
  const [nameCleanupLoading, setNameCleanupLoading] = useState(false)
  const [nameCleanupApplying, setNameCleanupApplying] = useState(false)
  const [nameCleanupItems, setNameCleanupItems] = useState<NameCleanupPreviewItem[]>([])
  const [nameCleanupSelected, setNameCleanupSelected] = useState<Set<number>>(new Set())

  // ── Inline-edit state for the name cell on the customers table ────
  // The merchant clicks the pencil → ``inlineEditId`` is set → the
  // matching row swaps the name span for an input + Save/Cancel
  // buttons. We keep all editing state in a single object so the
  // table re-renders only the row being edited, not the whole list.
  // ``inlineNameToast`` drives the success / failure banner that
  // surfaces after each save.
  const [inlineEditId, setInlineEditId] = useState<number | null>(null)
  const [inlineEditValue, setInlineEditValue] = useState('')
  const [inlineEditSaving, setInlineEditSaving] = useState(false)
  const [inlineEditError, setInlineEditError] = useState('')
  const [inlineNameToast, setInlineNameToast] = useState<{
    ok: boolean
    text: string
  } | null>(null)
  // Auto-dismiss the toast after a few seconds so it doesn't pin
  // forever after a stream of quick edits.
  useEffect(() => {
    if (!inlineNameToast) return
    const timer = setTimeout(() => setInlineNameToast(null), 3000)
    return () => clearTimeout(timer)
  }, [inlineNameToast])

  // Hard-coded to mirror the backend ``CUSTOMER_NAME_MAX_LEN`` so the
  // UI can show a counter + block over-long submissions before the
  // network round-trip. Keep in sync with backend/routers/customers.py.
  const INLINE_NAME_MAX_LEN = 80

  const startInlineEdit = useCallback((cust: CustomerRecord) => {
    setInlineEditId(cust.id)
    setInlineEditValue(cust.display_name || cust.name || '')
    setInlineEditError('')
  }, [])

  const cancelInlineEdit = useCallback(() => {
    setInlineEditId(null)
    setInlineEditValue('')
    setInlineEditError('')
    setInlineEditSaving(false)
  }, [])

  const saveInlineEdit = useCallback(async (cust: CustomerRecord) => {
    // Client-side validation — same rules the backend enforces.
    //
    // EMPTY NAMES ARE NOW ALLOWED (May 2026 policy). The merchant
    // can wipe a garbage import name, and the dashboard renders the
    // empty cell as a grey "بدون اسم" placeholder while campaigns
    // and templates fall back to "عميلنا الغالي". A high-confidence
    // AI-detected name from a later conversation may refill it
    // automatically. See ``backend/routers/customers.update_customer``
    // for the contract: empty body.name → Customer.name=None +
    // manual_name_override=true + manual_name_cleared=true.
    const trimmed = (inlineEditValue || '')
      .trim()
      .replace(/\s+/g, ' ')
    if (trimmed.length > INLINE_NAME_MAX_LEN) {
      setInlineEditError(cu.inlineEdit.nameTooLong.replace('{max}', String(INLINE_NAME_MAX_LEN)))
      return
    }
    // No-op shortcut: if the merchant didn't actually change the
    // value, just close the editor. We still trigger the API call
    // when they typed the SAME value back — that's the merchant
    // explicitly approving the spelling, which flips the
    // ``manual_name_override`` flag so the bulk cleaner stops
    // proposing changes for this row.
    setInlineEditSaving(true)
    setInlineEditError('')
    try {
      const res = await customersApi.update(cust.id, { name: trimmed })
      const _wasCleared = !trimmed
      // Update the row IN-PLACE so the merchant stays inside the
      // current search / filter / page view. Critical for the
      // "search for 'تيك توك' → edit row → keep going" workflow.
      //
      // ``res.name`` comes back as ``""`` when the merchant cleared
      // it; we store it as ``""`` so the renderer below shows the
      // grey "بدون اسم" placeholder.
      setCustomers(prev =>
        prev.map(c =>
          c.id === cust.id
            ? {
                ...c,
                name: res.name ?? trimmed,
                manual_name_override: res.manual_name_override ?? true,
                manual_name_cleared: (res as any).manual_name_cleared ?? _wasCleared,
                manual_name_edited_at: new Date().toISOString(),
              }
            : c,
        ),
      )
      // Also patch the drawer-mounted customer if it happens to be
      // the same row, so reopening it doesn't flash the stale name.
      setSelectedCustomer(prev =>
        prev && prev.id === cust.id
          ? {
              ...prev,
              name: res.name ?? trimmed,
              manual_name_override: res.manual_name_override ?? true,
              manual_name_cleared: (res as any).manual_name_cleared ?? _wasCleared,
              manual_name_edited_at: new Date().toISOString(),
            }
          : prev,
      )
      setInlineNameToast({
        ok: true,
        text: _wasCleared
          ? cu.inlineEdit.nameCleared
          : cu.inlineEdit.nameUpdated,
      })
      cancelInlineEdit()
    } catch (err: any) {
      const msg =
        err?.detail
        || err?.message
        || cu.inlineEdit.updateFailed
      setInlineEditError(
        typeof msg === 'string' ? msg : JSON.stringify(msg),
      )
    } finally {
      setInlineEditSaving(false)
    }
  }, [inlineEditValue, cancelInlineEdit, cu])
  // Per-row edit state — keyed by customer_id. Initialised from the
  // preview response (which merges in any saved draft) and mutated
  // in-place as the merchant toggles chips. Autosave watches the
  // ``dirty`` flag and dribbles changes to the backend.
  const [nameCleanupRowState, setNameCleanupRowState] = useState<
    Record<number, CleanupRowState>
  >({})
  const [nameCleanupFilter, setNameCleanupFilter] = useState<CleanupFilter>('all')
  // Per-reason chip filter (May 2026) — orthogonal to the existing
  // confidence/skip chips. ``'any'`` shows all categories. Filter is
  // applied client-side using each item's ``category`` field so we
  // don't need extra round-trips on chip toggle.
  const [nameCleanupCategory, setNameCleanupCategory] = useState<
    'any' | NameCleanupCategory
  >('any')
  // Histogram surfaced by the preview endpoint — used to render
  // chip badges with the FULL match population. ``null`` until the
  // first preview fetch resolves.
  const [nameCleanupCategoryCounts, setNameCleanupCategoryCounts] = useState<
    Record<NameCleanupCategory, number> | null
  >(null)

  // ── Session-stable view (May 2026) ───────────────────────────────
  // ``nameCleanupPinnedIds`` is the set of customer_ids that are
  // visible under the CURRENT filter+category chip. It's a SNAPSHOT —
  // it never changes when the merchant edits a chip / clears a row /
  // skips a row. The rationale: if a row was visible the moment the
  // merchant opened a chip, it has to STAY visible until they
  // explicitly:
  //   * change the filter / category chip, OR
  //   * hit the "تحديث المعاينة" rescan link, OR
  //   * close & re-open the modal, OR
  //   * apply a batch (which reloads the preview).
  //
  // Without this, every chip toggle would yank the row out of the
  // current chip (because predicates like ``!cleanupRowIsEdited``
  // flip the moment the merchant touches a chip), making it
  // physically impossible to remove a SECOND noisy token from the
  // same name. We tracked it down to the #1 frustration in the
  // preview-session feedback ("الصف يختفي قبل ما أكمل").
  //
  // ``null`` means "no snapshot yet" — visibleCleanupItems falls
  // back to an empty array until the first useEffect synchronisation
  // fires (which happens immediately after items load).
  const [nameCleanupPinnedIds, setNameCleanupPinnedIds] = useState<Set<number> | null>(null)
  // Bump this counter to force a snapshot rebuild WITHOUT changing
  // any filter / category — used by the inline "تحديث المعاينة"
  // button. It's just a monotonically increasing dep for the
  // pinned-snapshot useEffect.
  const [nameCleanupViewEpoch, setNameCleanupViewEpoch] = useState(0)
  // Ref shadow of ``nameCleanupRowState`` so the pinned-snapshot
  // effect can READ the latest row state without LISTING it as a
  // dep (which would defeat the entire pinning mechanism).
  const nameCleanupRowStateRef = useRef(nameCleanupRowState)
  useEffect(() => {
    nameCleanupRowStateRef.current = nameCleanupRowState
  }, [nameCleanupRowState])
  const [nameCleanupSummary, setNameCleanupSummary] = useState<{
    totalCustomers: number
    totalScanned:   number
    matchCount:     number
    highConfidence: number
    lowConfidence:  number
    truncated:      boolean
    maxItems:       number
    draftEdited:    number
    draftSkipped:   number
  } | null>(null)
  const [nameCleanupResult, setNameCleanupResult] = useState<{
    applied: number
    skipped: number
    draftsCleared: number
  } | null>(null)
  const [nameCleanupError, setNameCleanupError] = useState('')
  const [nameCleanupSaveState, setNameCleanupSaveState] = useState<CleanupSaveState>('idle')
  const [nameCleanupLastSavedAt, setNameCleanupLastSavedAt] = useState<string | null>(null)

  // Autosave plumbing: a ref-held debounce timer so consecutive chip
  // toggles coalesce into one POST, plus a ref-held "in-flight" guard
  // so we don't fire overlapping save requests when the merchant is
  // clicking fast. We keep them in refs (not state) so toggling them
  // doesn't re-render the modal.
  const nameCleanupSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const nameCleanupSaveInFlight = useRef(false)
  const nameCleanupPendingFlush = useRef(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [res, metricsRes] = await Promise.all([
        customersApi.list({
          search, page, perPage: 50, segment: segmentKey,
          manualSegment: manualSegmentKey || undefined,
          marketingOptOut: marketingOptOutFilter === 'out' ? true : undefined,
        }),
        customersApi.metrics(),
      ])
      setCustomers(res.customers)
      setTotal(res.total)
      setPages(res.pages)
      setMetrics(metricsRes)
    } catch {
      setCustomers([])
      setMetrics(null)
    } finally {
      setLoading(false)
    }
  }, [search, page, segmentKey, manualSegmentKey, marketingOptOutFilter])

  const loadSegments = useCallback(async () => {
    setSegmentsLoading(true)
    try {
      const res = await customersApi.segments()
      setSegments(res.segments)
    } catch {
      setSegments([])
    } finally {
      setSegmentsLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    loadSegments()
  }, [loadSegments])

  useEffect(() => {
    setPage(1)
  }, [search, segmentKey, manualSegmentKey, marketingOptOutFilter])

  const handleAdd = async () => {
    if (!addName.trim() || !addPhone.trim()) {
      setAddError(cu.addModal.nameRequired)
      return
    }
    setAddLoading(true)
    setAddError('')
    try {
      await customersApi.create({
        name: addName.trim(),
        phone: addPhone.trim(),
        email: addEmail.trim() || undefined,
      })
      setShowAdd(false)
      setAddName('')
      setAddPhone('')
      setAddEmail('')
      load()
    } catch (err: any) {
      const msg =
        err?.detail || err?.message || cu.addModal.addFailed
      setAddError(typeof msg === 'string' ? msg : JSON.stringify(msg))
    } finally {
      setAddLoading(false)
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm(cu.deleteModal.singleConfirm)) return
    try {
      await customersApi.delete(id)
      setSelectedCustomer(null)
      setSelectedIds(prev => { const s = new Set(prev); s.delete(id); return s })
      load()
    } catch {
      alert(cu.deleteModal.deleteFailed)
    }
  }

  const toggleSelect = (id: number) => {
    setSelectedIds(prev => {
      const s = new Set(prev)
      s.has(id) ? s.delete(id) : s.add(id)
      return s
    })
  }

  const toggleSelectAll = () => {
    if (selectedIds.size === customers.length) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(customers.map(c => c.id)))
    }
  }

  const handleBulkDelete = async () => {
    setDeleteLoading(true)
    try {
      if (deleteModal === 'all') {
        await customersApi.deleteAll()
      } else {
        await customersApi.bulkDelete(Array.from(selectedIds))
      }
      setSelectedIds(new Set())
      setDeleteModal(null)
      setDeleteConfirmText('')
      setSelectedCustomer(null)
      load()
    } catch {
      alert(cu.deleteModal.deleteFailed)
    } finally {
      setDeleteLoading(false)
    }
  }

  // ── Name-cleanup handlers ─────────────────────────────────────────
  const splitCleanupTokens = (raw: string): string[] =>
    (raw || '').trim().split(/\s+/).filter(Boolean)

  // For a row that the merchant hasn't manually edited, infer what
  // the cleaner would have removed by diffing current_name against
  // suggested_name. This is the starting chip state per row.
  const inferCleanerRemoved = (it: NameCleanupPreviewItem): number[] => {
    const tokens = splitCleanupTokens(it.current_name)
    if (!it.suggested_name) {
      return tokens.map((_, i) => i)
    }
    const remaining = new Map<string, number>()
    splitCleanupTokens(it.suggested_name).forEach(t =>
      remaining.set(t, (remaining.get(t) || 0) + 1),
    )
    const removed: number[] = []
    tokens.forEach((tok, i) => {
      const left = remaining.get(tok) || 0
      if (left > 0) {
        remaining.set(tok, left - 1)
      } else {
        removed.push(i)
      }
    })
    return removed
  }

  // Build the initial RowState map for a preview response. Draft state
  // (from a previous session) wins; otherwise we use the cleaner's
  // default removal set. Either way the row starts ``dirty=false``.
  const buildInitialRowState = (
    items: NameCleanupPreviewItem[],
  ): Record<number, CleanupRowState> => {
    const map: Record<number, CleanupRowState> = {}
    for (const it of items) {
      const draft = it.draft
      map[it.customer_id] = {
        removed: draft?.removed_word_indices
          ? [...draft.removed_word_indices]
          : null,
        cleared: !!draft?.cleared,
        status:  (draft?.status as CleanupRowStatus) || 'edited',
        dirty:   false,
        savedAt: draft?.updated_at ?? null,
      }
    }
    return map
  }

  const openNameCleanup = async () => {
    setNameCleanupOpen(true)
    setNameCleanupResult(null)
    setNameCleanupError('')
    setNameCleanupLoading(true)
    try {
      const res = await customersApi.nameCleanupPreview()
      setNameCleanupItems(res.items)
      setNameCleanupRowState(buildInitialRowState(res.items))
      setNameCleanupCategoryCounts(res.category_counts ?? null)
      setNameCleanupSummary({
        totalCustomers: res.total_customers,
        totalScanned:   res.total_scanned,
        matchCount:     res.match_count,
        highConfidence: res.high_confidence,
        lowConfidence:  res.low_confidence,
        truncated:      res.truncated,
        maxItems:       res.max_items,
        draftEdited:    res.draft_edited,
        draftSkipped:   res.draft_skipped,
      })
      // ── Default: NO rows pre-selected (May 2026 policy) ──────
      // Earlier behaviour ticked every high-confidence row by
      // default, which made it dangerously easy to bulk-clear
      // 1,800+ names with a single click before the merchant
      // could review them. We now open the modal with an empty
      // selection — the merchant must explicitly tick each row
      // (or use the "تحديد الكل في الصفحة الحالية" header chip)
      // before the "تطبيق المحدد" button activates.
      setNameCleanupSelected(new Set())
      setNameCleanupSaveState('saved')
      setNameCleanupLastSavedAt(new Date().toISOString())
    } catch (err: any) {
      const msg = err?.detail || err?.message || cu.nameCleanup.loadPreviewFailed
      setNameCleanupError(typeof msg === 'string' ? msg : JSON.stringify(msg))
      setNameCleanupItems([])
      setNameCleanupSummary(null)
    } finally {
      setNameCleanupLoading(false)
    }
  }

  const closeNameCleanup = () => {
    if (nameCleanupApplying) return
    // Flush any pending autosave synchronously-ish so the merchant
    // doesn't lose the last few chip toggles by closing the modal
    // half a second too early. ``triggerCleanupAutosave`` resolves
    // when the in-flight POST finishes; we fire-and-forget so the
    // modal closes immediately but the network call still runs.
    if (nameCleanupSaveTimer.current) {
      clearTimeout(nameCleanupSaveTimer.current)
      nameCleanupSaveTimer.current = null
    }
    void flushCleanupAutosave()
    setNameCleanupOpen(false)
    setNameCleanupItems([])
    setNameCleanupSelected(new Set())
    setNameCleanupRowState({})
    setNameCleanupSummary(null)
    setNameCleanupResult(null)
    setNameCleanupError('')
    setNameCleanupFilter('all')
    setNameCleanupCategory('any')
    setNameCleanupCategoryCounts(null)
    setNameCleanupSaveState('idle')
    setNameCleanupPinnedIds(null)
    setNameCleanupViewEpoch(0)
  }

  // ── Autosave loop ────────────────────────────────────────────────
  // Collect every "dirty" row and POST it as a single batch. We re-run
  // ourselves whenever:
  //   * the debounce timer fires from a chip toggle, or
  //   * a save completes and there are still dirty rows queued.
  const flushCleanupAutosave = useCallback(async (): Promise<void> => {
    if (nameCleanupSaveInFlight.current) {
      // A save is already running. Mark "another flush needed" so we
      // re-trigger as soon as the current one resolves.
      nameCleanupPendingFlush.current = true
      return
    }
    // Snapshot current dirty rows. We mutate state at the end, so
    // capture by reference from the latest setState callback.
    let dirtyPayload: Array<{
      customer_id: number
      removed_word_indices: number[] | null
      cleared: boolean
      status: 'edited' | 'skipped'
    }> = []
    setNameCleanupRowState(prev => {
      dirtyPayload = Object.entries(prev)
        .filter(([, st]) => st.dirty)
        .map(([cid, st]) => ({
          customer_id:          Number(cid),
          removed_word_indices: st.removed,
          cleared:              st.cleared,
          status:               st.status,
        }))
      return prev
    })
    if (dirtyPayload.length === 0) {
      setNameCleanupSaveState(prevState => (
        prevState === 'saving' ? 'saved' : prevState
      ))
      return
    }

    nameCleanupSaveInFlight.current = true
    setNameCleanupSaveState('saving')
    try {
      const res = await customersApi.nameCleanupDraftSave(dirtyPayload)
      const savedIds = new Set(dirtyPayload.map(p => p.customer_id))
      setNameCleanupRowState(prev => {
        const next: Record<number, CleanupRowState> = { ...prev }
        for (const id of savedIds) {
          if (next[id]) {
            next[id] = { ...next[id], dirty: false, savedAt: res.saved_at }
          }
        }
        return next
      })
      setNameCleanupLastSavedAt(res.saved_at)
      setNameCleanupSaveState('saved')
    } catch (err: any) {
      // Surface but keep the dirty flag so the next debounce retries.
      const msg = err?.detail || err?.message || cu.nameCleanup.saveDraftFailed
      setNameCleanupError(typeof msg === 'string' ? msg : JSON.stringify(msg))
      setNameCleanupSaveState('error')
    } finally {
      nameCleanupSaveInFlight.current = false
      if (nameCleanupPendingFlush.current) {
        nameCleanupPendingFlush.current = false
        // Another flush was requested while we were running — go
        // again so the merchant's latest edits don't sit forever.
        void flushCleanupAutosave()
      }
    }
  }, [cu])

  // Mark a single row dirty AND schedule a debounced autosave run.
  const queueCleanupAutosave = useCallback(() => {
    if (nameCleanupSaveTimer.current) {
      clearTimeout(nameCleanupSaveTimer.current)
    }
    setNameCleanupSaveState('saving')
    nameCleanupSaveTimer.current = setTimeout(() => {
      nameCleanupSaveTimer.current = null
      void flushCleanupAutosave()
    }, NAME_CLEANUP_AUTOSAVE_DEBOUNCE_MS)
  }, [flushCleanupAutosave])

  // ── Row helpers (rebuild around the new RowState shape) ──────────
  const cleanupResolvedRemoved = useCallback(
    (it: NameCleanupPreviewItem): Set<number> => {
      const st = nameCleanupRowState[it.customer_id]
      if (st?.cleared) {
        return new Set(splitCleanupTokens(it.current_name).map((_, i) => i))
      }
      if (st?.removed !== undefined && st.removed !== null) {
        return new Set(st.removed)
      }
      return new Set(inferCleanerRemoved(it))
    },
    [nameCleanupRowState],
  )

  const cleanupRowIsCleared = (id: number): boolean =>
    !!nameCleanupRowState[id]?.cleared

  const cleanupRowIsSkipped = (id: number): boolean =>
    nameCleanupRowState[id]?.status === 'skipped'

  const cleanupRowIsEdited = (id: number): boolean => {
    const st = nameCleanupRowState[id]
    if (!st) return false
    return st.cleared || st.removed !== null || st.status === 'skipped'
  }

  const cleanupRowResolvedValue = (it: NameCleanupPreviewItem): string | null => {
    if (cleanupRowIsCleared(it.customer_id)) return null
    const removed = cleanupResolvedRemoved(it)
    const tokens = splitCleanupTokens(it.current_name)
    const kept = tokens.filter((_, i) => !removed.has(i))
    const joined = kept.join(' ').trim()
    return joined.length > 0 ? joined : null
  }

  // Mutator: toggle one word in the row's removed set. Implicitly
  // marks the row dirty and selects it for apply.
  const toggleCleanupWord = (it: NameCleanupPreviewItem, idx: number) => {
    setNameCleanupRowState(prev => {
      const existing = prev[it.customer_id]
      const baseRemoved = existing?.removed
        ?? inferCleanerRemoved(it)
      const set = new Set(baseRemoved)
      if (set.has(idx)) set.delete(idx)
      else set.add(idx)
      return {
        ...prev,
        [it.customer_id]: {
          removed: Array.from(set),
          // Touching a chip implicitly takes the row out of "cleared".
          cleared: false,
          status:  existing?.status === 'skipped' ? 'edited' : (existing?.status ?? 'edited'),
          dirty:   true,
          savedAt: existing?.savedAt ?? null,
        },
      }
    })
    setNameCleanupSelected(prev => {
      if (prev.has(it.customer_id)) return prev
      const next = new Set(prev)
      next.add(it.customer_id)
      return next
    })
    queueCleanupAutosave()
  }

  const clearCleanupRow = (id: number) => {
    setNameCleanupRowState(prev => {
      const existing = prev[id]
      return {
        ...prev,
        [id]: {
          removed: null,
          cleared: true,
          status:  'edited',
          dirty:   true,
          savedAt: existing?.savedAt ?? null,
        },
      }
    })
    setNameCleanupSelected(prev => {
      if (prev.has(id)) return prev
      const next = new Set(prev)
      next.add(id)
      return next
    })
    queueCleanupAutosave()
  }

  const resetCleanupRow = (id: number) => {
    // Drop the row back to "no edits". When we autosave with
    // ``removed=null && cleared=false && status='edited'`` the
    // backend deletes the draft row server-side.
    setNameCleanupRowState(prev => {
      const existing = prev[id]
      if (!existing) return prev
      return {
        ...prev,
        [id]: {
          removed: null,
          cleared: false,
          status:  'edited',
          dirty:   true,
          savedAt: existing.savedAt,
        },
      }
    })
    queueCleanupAutosave()
  }

  const skipCleanupRow = (id: number) => {
    setNameCleanupRowState(prev => {
      const existing = prev[id]
      return {
        ...prev,
        [id]: {
          removed: existing?.removed ?? null,
          cleared: !!existing?.cleared,
          status:  'skipped',
          dirty:   true,
          savedAt: existing?.savedAt ?? null,
        },
      }
    })
    // Skipped rows are NOT going to be applied — remove from selection.
    setNameCleanupSelected(prev => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
    queueCleanupAutosave()
  }

  // Explicit "save now" — used by the "حفظ ومتابعة لاحقاً" button.
  const saveCleanupDraftNow = async () => {
    if (nameCleanupSaveTimer.current) {
      clearTimeout(nameCleanupSaveTimer.current)
      nameCleanupSaveTimer.current = null
    }
    await flushCleanupAutosave()
  }

  // Discard every draft row server-side; reload the modal to see
  // pristine cleaner defaults again.
  const discardCleanupDraft = async () => {
    if (!confirm(cu.draftDiscardConfirm)) {
      return
    }
    try {
      await customersApi.nameCleanupDraftDiscard()
      await openNameCleanup()
    } catch (err: any) {
      const msg = err?.detail || err?.message || cu.nameCleanup.discardFailed
      setNameCleanupError(typeof msg === 'string' ? msg : JSON.stringify(msg))
    }
  }

  // ── Predicate-based "live" filter (NOT used directly for render) ──
  // This is the rule that says "would this row be visible under the
  // current filter+category if we re-evaluated everything right now".
  // It's used in two places:
  //   1. To compute the pinned snapshot the moment the merchant
  //      changes a chip (next useEffect below).
  //   2. To compute the live chip counters in the toolbar so the
  //      merchant can SEE how many rows have moved buckets since the
  //      snapshot was taken (e.g. "تم تعديله يدوياً (3)").
  //
  // We deliberately do NOT memoize this on ``nameCleanupRowState``
  // for the render path — that would re-evaluate predicates on every
  // chip toggle and re-shuffle the visible set, which is the bug we
  // just removed.
  const cleanupRowMatchesActiveFilter = useCallback(
    (
      it: NameCleanupPreviewItem,
      rowState: Record<number, CleanupRowState>,
    ): boolean => {
      const isSkippedHere = rowState[it.customer_id]?.status === 'skipped'
      const isEditedHere = (() => {
        const st = rowState[it.customer_id]
        if (!st) return false
        return st.cleared || st.removed !== null || st.status === 'skipped'
      })()
      const matchesCategory =
        nameCleanupCategory === 'any'
          ? true
          : (it.category || 'other') === nameCleanupCategory
      if (!matchesCategory) return false
      switch (nameCleanupFilter) {
        case 'opted_out':
          return !!it.marketing_opt_out_manual && !isSkippedHere
        case 'all':
          return !isSkippedHere
        case 'pending':
          return !isEditedHere
        case 'edited':
          return isEditedHere && !isSkippedHere
        case 'high':
          return it.confidence === 'high' && !isSkippedHere
        case 'low':
          return it.confidence === 'low' && !isSkippedHere
        default:
          return !isSkippedHere
      }
    },
    [nameCleanupFilter, nameCleanupCategory],
  )

  // ── Snapshot synchronisation ─────────────────────────────────────
  // Rebuild the pinned-id snapshot WHENEVER the merchant changes
  // the filter chip, the category chip, hits "تحديث المعاينة" (epoch
  // bump), or the items list itself changes (e.g. preview reload
  // after apply). Notably absent from the deps: ``nameCleanupRowState``.
  // That's the whole point — chip toggles inside a card MUST NOT
  // trigger a rebuild.
  useEffect(() => {
    if (!nameCleanupOpen) return
    const rowState = nameCleanupRowStateRef.current
    const ids = new Set<number>()
    for (const it of nameCleanupItems) {
      if (cleanupRowMatchesActiveFilter(it, rowState)) {
        ids.add(it.customer_id)
      }
    }
    setNameCleanupPinnedIds(ids)
  }, [
    nameCleanupOpen,
    nameCleanupItems,
    nameCleanupFilter,
    nameCleanupCategory,
    nameCleanupViewEpoch,
    cleanupRowMatchesActiveFilter,
  ])

  // The render-time visible list is the items array intersected with
  // the pinned snapshot, preserving the original sort order. Once
  // pinned, a row stays put until one of the rebuild triggers above
  // fires — even if its state has drifted (now edited / now skipped).
  const visibleCleanupItems = useMemo(() => {
    if (!nameCleanupPinnedIds) return []
    return nameCleanupItems.filter(it =>
      nameCleanupPinnedIds.has(it.customer_id),
    )
  }, [nameCleanupItems, nameCleanupPinnedIds])

  // ── Drift counter ────────────────────────────────────────────────
  // How many currently-pinned rows have drifted out of the active
  // filter since the snapshot was taken? Surfaced as a "تحديث
  // المعاينة (N)" link so the merchant sees there's pending work to
  // re-bucket. ``0`` means the snapshot is in sync with the live
  // predicate — link stays grey/disabled.
  const cleanupDriftedCount = useMemo(() => {
    if (!nameCleanupPinnedIds) return 0
    let count = 0
    for (const it of nameCleanupItems) {
      if (!nameCleanupPinnedIds.has(it.customer_id)) continue
      if (!cleanupRowMatchesActiveFilter(it, nameCleanupRowState)) {
        count += 1
      }
    }
    return count
  }, [
    nameCleanupPinnedIds,
    nameCleanupItems,
    nameCleanupRowState,
    cleanupRowMatchesActiveFilter,
  ])

  const refreshCleanupView = useCallback(() => {
    setNameCleanupViewEpoch(n => n + 1)
  }, [])

  // Flush the autosave on unmount so the merchant doesn't lose work
  // by navigating away from the customers page mid-review.
  useEffect(() => {
    return () => {
      if (nameCleanupSaveTimer.current) {
        clearTimeout(nameCleanupSaveTimer.current)
      }
      // Fire-and-forget — the page is unloading, we can't await.
      void flushCleanupAutosave()
    }
  }, [flushCleanupAutosave])

  const toggleCleanupRow = (id: number) => {
    setNameCleanupSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleCleanupSelectAll = () => {
    // Operate on the currently visible (post-filter) rows so the
    // merchant can e.g. "select all low-confidence + apply".
    const visibleIds = visibleCleanupItems.map(it => it.customer_id)
    const allVisibleSelected = visibleIds.every(id => nameCleanupSelected.has(id))
    if (allVisibleSelected) {
      setNameCleanupSelected(prev => {
        const next = new Set(prev)
        visibleIds.forEach(id => next.delete(id))
        return next
      })
    } else {
      setNameCleanupSelected(prev => {
        const next = new Set(prev)
        visibleIds.forEach(id => next.add(id))
        return next
      })
    }
  }

  const applyCleanupSelected = async () => {
    if (nameCleanupSelected.size === 0) return
    setNameCleanupApplying(true)
    setNameCleanupError('')
    try {
      // Make sure any in-flight chip edits hit the server before we
      // apply, so the audit row picks up the merchant's latest state.
      await saveCleanupDraftNow()

      const items = nameCleanupItems
        .filter(it => nameCleanupSelected.has(it.customer_id))
        .filter(it => !cleanupRowIsSkipped(it.customer_id))
        .map(it => {
          const resolved = cleanupRowResolvedValue(it)
          const wasEdited = cleanupRowIsEdited(it.customer_id)
          return {
            customer_id: it.customer_id,
            new_name:    resolved,
            reason:      wasEdited ? cu.nameCleanup.manualEditReason.replace('{reason}', it.reason) : it.reason,
            confidence:  it.confidence,
          }
        })
      const res = await customersApi.nameCleanupApply({ items })
      setNameCleanupResult({
        applied:       res.applied_count,
        skipped:       res.skipped_count,
        draftsCleared: res.drafts_cleared,
      })
      const appliedIds = new Set(res.applied.map(a => a.customer_id))
      setNameCleanupItems(prev => prev.filter(it => !appliedIds.has(it.customer_id)))
      setNameCleanupSelected(new Set())
      setNameCleanupRowState(prev => {
        const next = { ...prev }
        appliedIds.forEach(id => delete next[id])
        return next
      })
      // Drop applied counts off the summary so the merchant sees
      // realistic remaining numbers without a round-trip.
      setNameCleanupSummary(prev => {
        if (!prev) return prev
        return {
          ...prev,
          matchCount:  Math.max(0, prev.matchCount - res.applied_count),
          draftEdited: Math.max(0, prev.draftEdited - res.drafts_cleared),
        }
      })
      load()
    } catch (err: any) {
      const msg = err?.detail || err?.message || cu.nameCleanup.applyFailed
      setNameCleanupError(typeof msg === 'string' ? msg : JSON.stringify(msg))
    } finally {
      setNameCleanupApplying(false)
    }
  }

  const applyCleanupHighConfidence = async () => {
    setNameCleanupApplying(true)
    setNameCleanupError('')
    try {
      await saveCleanupDraftNow()
      const res = await customersApi.nameCleanupApply({
        highConfidenceOnly: true,
      })
      setNameCleanupResult({
        applied:       res.applied_count,
        skipped:       res.skipped_count,
        draftsCleared: res.drafts_cleared,
      })
      const appliedIds = new Set(res.applied.map(a => a.customer_id))
      setNameCleanupItems(prev => prev.filter(it => !appliedIds.has(it.customer_id)))
      setNameCleanupSelected(prev => {
        const next = new Set(prev)
        appliedIds.forEach(id => next.delete(id))
        return next
      })
      setNameCleanupRowState(prev => {
        const next = { ...prev }
        appliedIds.forEach(id => delete next[id])
        return next
      })
      setNameCleanupSummary(prev => {
        if (!prev) return prev
        return {
          ...prev,
          matchCount:     Math.max(0, prev.matchCount - res.applied_count),
          highConfidence: Math.max(0, prev.highConfidence - res.applied_count),
          draftEdited:    Math.max(0, prev.draftEdited - res.drafts_cleared),
        }
      })
      load()
    } catch (err: any) {
      const msg = err?.detail || err?.message || cu.nameCleanup.applyFailed
      setNameCleanupError(typeof msg === 'string' ? msg : JSON.stringify(msg))
    } finally {
      setNameCleanupApplying(false)
    }
  }

  const allCurrentSelected = customers.length > 0 && selectedIds.size === customers.length
  const someSelected = selectedIds.size > 0 && !allCurrentSelected

  return (
    <div dir={dir} className="space-y-5">
      {/* ── Inline name-edit toast ────────────────────────────────────
          Floating notification rendered at the bottom of the viewport
          so it never covers the row the merchant just edited. Auto-
          dismisses after 3s (see the useEffect near the state
          declaration). Click anywhere to dismiss earlier. */}
      {inlineNameToast && (
        <div
          onClick={() => setInlineNameToast(null)}
          role="status"
          aria-live="polite"
          className={`fixed bottom-6 inset-x-0 mx-auto z-50 max-w-xs px-4 py-2.5 rounded-lg border text-xs text-center shadow-lg cursor-pointer ${
            inlineNameToast.ok
              ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
              : 'bg-red-50 border-red-200 text-red-800'
          }`}
        >
          {inlineNameToast.ok ? '✓ ' : '⚠️ '}{inlineNameToast.text}
        </div>
      )}

      <PageHeader
        title={cu.title}
        subtitle={cu.subtitle}
        action={
          <div className="flex items-center gap-2">
            <button
              onClick={openNameCleanup}
              className="btn-secondary text-sm flex items-center gap-2"
              title={cu.actions.cleanNamesTitle}
            >
              <Sparkles className="w-4 h-4" />
              {cu.actions.cleanNames}
            </button>
            <button
              onClick={() => navigate('/customers/import')}
              className="btn-secondary text-sm flex items-center gap-2"
            >
              <Upload className="w-4 h-4" />
              {cu.actions.importCustomers}
            </button>
            <button
              onClick={() => setShowAdd(true)}
              className="btn-primary text-sm flex items-center gap-2"
            >
              <UserPlus className="w-4 h-4" />
              {cu.actions.addCustomer}
            </button>
          </div>
        }
      />

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label={cu.cards.total}
          value={String(metrics?.totalCustomers ?? total)}
          change={0}
          icon={Users}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
        <StatCard
          label={cu.cards.vip}
          value={String(metrics?.vipCustomers ?? 0)}
          change={0}
          icon={Crown}
          iconColor="text-amber-600"
          iconBg="bg-amber-50"
        />
        <StatCard
          label={cu.cards.atRisk}
          value={String((metrics?.atRiskCustomers ?? 0) + (metrics?.inactiveCustomers ?? 0))}
          change={0}
          icon={AlertTriangle}
          iconColor="text-red-600"
          iconBg="bg-red-50"
        />
        <StatCard
          label={cu.cards.active}
          value={String(metrics?.activeCustomers ?? 0)}
          change={0}
          icon={ShoppingCart}
          iconColor="text-emerald-600"
          iconBg="bg-emerald-50"
        />
      </div>

      {/* Unified segment chips — each chip shows EVERY customer in that
          cohort, whether the auto classifier put them there OR the
          merchant added them manually. The drawer surfaces the source
          per-customer ("VIP تلقائي" vs "VIP يدوي" vs "VIP يدوي + تلقائي").
          We label the strip explicitly so merchants don't read "RFM"
          into it — to them it's just "the segments". */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 px-1">
          <Tag className="w-3.5 h-3.5 text-slate-400" />
          <span className="text-[11px] font-semibold text-slate-600">
            {cu.segments.title}
          </span>
          <span className="text-[10px] text-slate-400">
            {cu.segments.subtitle}
          </span>
        </div>
        <SegmentChips
          cu={cu}
          lang={lang}
          locale={locale}
          segments={segments}
          loading={segmentsLoading}
          active={segmentKey}
          onSelect={setSegmentKey}
          campaignExcludedActive={marketingOptOutFilter === 'out'}
          onToggleCampaignExcluded={() => {
            setMarketingOptOutFilter((prev) => (prev === 'out' ? 'all' : 'out'))
            setPage(1)
          }}
          campaignExcludedLabel={cu.filters.marketingOut}
        />
      </div>

      {/* Search + Actions */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="absolute start-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={cu.searchPlaceholder}
            dir={dir}
            className="w-full ps-9 pe-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
          />
        </div>

        {/* Manual-only filter — narrow axis for merchants who want to
            see ONLY their manually-tagged customers (e.g. for audit).
            The chip strip above already unions auto+manual so this
            dropdown is opt-in tooling, not the default path. */}
        <div className="relative">
          <Tag className="absolute start-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400 pointer-events-none" />
          <select
            value={manualSegmentKey}
            onChange={(e) => setManualSegmentKey(e.target.value)}
            dir={dir}
            className="ps-9 pe-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none bg-white"
            title={cu.filters.manualSegmentTitle}
          >
            <option value="">{cu.filters.showAll}</option>
            <option value="none">{cu.filters.noManualTag}</option>
            {segments.filter(s => s.key !== 'all').map(s => (
              <option key={s.key} value={s.key}>
                {cu.filters.manualOnly.replace('{label}', segmentDisplayLabel(s, lang))}
              </option>
            ))}
          </select>
        </div>

        <button
          onClick={load}
          disabled={loading}
          className="btn-secondary text-sm flex items-center gap-2"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          {cu.actions.refresh}
        </button>

        {/* Bulk actions — only visible when items are selected */}
        {selectedIds.size > 0 && (
          <button
            onClick={() => { setDeleteModal('selected'); setDeleteConfirmText('') }}
            className="flex items-center gap-2 text-sm text-red-600 bg-red-50 border border-red-200 hover:bg-red-100 px-3 py-2 rounded-lg transition-colors"
          >
            <Trash2 className="w-4 h-4" />
            {cu.actions.deleteSelected} ({selectedIds.size})
          </button>
        )}

        {/* Delete all — always visible as secondary danger action */}
        <button
          onClick={() => { setDeleteModal('all'); setDeleteConfirmText('') }}
          className="flex items-center gap-2 text-sm text-red-500 hover:text-red-700 hover:bg-red-50 border border-transparent hover:border-red-200 px-3 py-2 rounded-lg transition-colors"
        >
          <Trash2 className="w-4 h-4" />
          {cu.actions.deleteAll}
        </button>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        {loading ? (
          <div className="flex flex-col items-center justify-center py-16 gap-3">
            <div className="w-8 h-8 border-4 border-brand-200 border-t-brand-500 rounded-full animate-spin" />
            <span className="text-sm text-slate-400">{cu.loading}</span>
          </div>
        ) : customers.length === 0 ? (
          <div className="text-center py-16 text-sm text-slate-400">
            {cu.table.empty}
          </div>
        ) : (
          <div className="overflow-x-auto overflow-y-auto max-h-[60vh]" dir={dir}>
            <table className="w-full text-xs" dir={dir}>
              <thead className="sticky top-0 z-10">
                <tr className="border-b border-slate-100 bg-slate-50">
                  {/* Select-all checkbox */}
                  <th className="px-3 py-3 w-10">
                    <button
                      onClick={toggleSelectAll}
                      className="text-slate-400 hover:text-brand-500 transition-colors"
                      title={allCurrentSelected ? cu.table.deselectAll : cu.table.selectAll}
                    >
                      {allCurrentSelected ? (
                        <CheckSquare className="w-4 h-4 text-brand-500" />
                      ) : someSelected ? (
                        <CheckSquare className="w-4 h-4 text-brand-300" />
                      ) : (
                        <Square className="w-4 h-4" />
                      )}
                    </button>
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">{cu.table.name}</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">{cu.table.phone}</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">{cu.table.email}</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">{cu.table.status}</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">RFM</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">{cu.table.smartSegment}</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">{cu.table.orders}</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">{cu.table.spend}</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">{cu.table.lastOrder}</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">{cu.table.source}</th>
                  <th className="px-3 py-3 w-10" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {customers.map((c) => {
                  const isChecked = selectedIds.has(c.id)
                  return (
                    <tr
                      key={c.id}
                      className={`hover:bg-slate-50 transition-colors ${isChecked ? 'bg-brand-50/60' : ''}`}
                    >
                      {/* Checkbox */}
                      <td className="px-3 py-3">
                        <button
                          onClick={(e) => { e.stopPropagation(); toggleSelect(c.id) }}
                          className="text-slate-300 hover:text-brand-500 transition-colors"
                        >
                          {isChecked
                            ? <CheckSquare className="w-4 h-4 text-brand-500" />
                            : <Square className="w-4 h-4" />
                          }
                        </button>
                      </td>
                      {/* ── Name cell with inline-edit pencil ─────────────
                          Click the pencil → the name span swaps for an
                          input + Save/Cancel. While editing we stop
                          propagation so opening the drawer doesn't
                          steal focus from the input. The PATCH call
                          updates the row IN-PLACE so the merchant
                          stays inside their current search/filter
                          context (critical for the "search تيك توك →
                          fix names one by one" workflow). */}
                      <td className="px-3 py-3">
                        {inlineEditId === c.id ? (
                          <div
                            className="flex flex-col gap-1 min-w-[200px]"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <div className="flex items-center gap-1.5">
                              <input
                                type="text"
                                autoFocus
                                value={inlineEditValue}
                                onChange={(e) => setInlineEditValue(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') {
                                    e.preventDefault()
                                    saveInlineEdit(c)
                                  } else if (e.key === 'Escape') {
                                    e.preventDefault()
                                    cancelInlineEdit()
                                  }
                                }}
                                disabled={inlineEditSaving}
                                maxLength={INLINE_NAME_MAX_LEN}
                                placeholder={cu.table.namePlaceholder}
                                className="px-2 py-1 text-sm border border-brand-300 rounded-md focus:outline-none focus:ring-2 focus:ring-brand-200 disabled:bg-slate-50 disabled:text-slate-400 w-full"
                                dir="auto"
                              />
                              <button
                                onClick={(e) => { e.stopPropagation(); saveInlineEdit(c) }}
                                disabled={inlineEditSaving}
                                title={cu.table.saveTitle}
                                className="p-1 rounded-md bg-brand-500 hover:bg-brand-600 text-white disabled:opacity-50 disabled:cursor-not-allowed"
                              >
                                {inlineEditSaving
                                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                  : <Check className="w-3.5 h-3.5" />}
                              </button>
                              <button
                                onClick={(e) => { e.stopPropagation(); cancelInlineEdit() }}
                                disabled={inlineEditSaving}
                                title={cu.table.cancelTitle}
                                className="p-1 rounded-md bg-slate-100 hover:bg-slate-200 text-slate-600 disabled:opacity-50 disabled:cursor-not-allowed"
                              >
                                <X className="w-3.5 h-3.5" />
                              </button>
                            </div>
                            {inlineEditError && (
                              <span className="text-[11px] text-red-600 leading-tight">
                                {inlineEditError}
                              </span>
                            )}
                            <span className="text-[10px] text-slate-400 leading-tight">
                              {inlineEditValue.length}/{INLINE_NAME_MAX_LEN}
                            </span>
                          </div>
                        ) : (
                          <div className="flex items-center gap-1.5 flex-wrap">
                            {/* The name itself opens the drawer (legacy
                                behaviour). The pencil — rendered as an
                                ALWAYS-VISIBLE inline-edit affordance —
                                stops propagation so it never opens the
                                drawer. Per merchant feedback, the
                                pencil must be discoverable at a
                                glance, not only on hover, so it ships
                                with a subtle base opacity that lifts
                                to full on hover/focus. The big trash
                                icon already lives in its own dedicated
                                column at the far end of the row, so
                                the pencil is unambiguously the
                                "edit name" affordance. */}
                            {/* Empty / NULL names render as a soft
                                "بدون اسم" placeholder (May 2026 policy).
                                The placeholder is NOT a real value — the
                                merchant deliberately wiped a garbage
                                name, templates fall back to
                                "عميلنا الغالي", and the AI may refill
                                from a future "اسمي ..." turn. The
                                placeholder is rendered in lighter
                                slate-400 italic so it reads as
                                "missing data" not "real value". */}
                            {(c.display_name || c.name) ? (
                              <span
                                className="font-medium text-slate-900 cursor-pointer"
                                onClick={() => setSelectedCustomer(c)}
                              >
                                {c.display_name || c.name}
                              </span>
                            ) : (
                              <span
                                className="font-normal italic text-slate-400 cursor-pointer"
                                onClick={() => setSelectedCustomer(c)}
                                title={cu.table.noNameTitle}
                              >
                                {cu.table.noName}
                              </span>
                            )}
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation()
                                startInlineEdit(c)
                              }}
                              className="inline-flex items-center justify-center w-6 h-6 rounded-md text-slate-400 hover:text-brand-600 hover:bg-brand-50 focus:text-brand-600 focus:bg-brand-50 focus:outline-none focus:ring-2 focus:ring-brand-200 transition-colors shrink-0"
                              title={cu.table.quickEditName}
                              aria-label={cu.table.editNameAria}
                            >
                              <Pencil className="w-3.5 h-3.5" />
                            </button>
                            {c.manual_name_override && (
                              <span
                                className="inline-flex items-center gap-0.5 bg-emerald-50 text-emerald-700 text-[10px] font-medium px-1.5 py-0.5 rounded-full border border-emerald-200"
                                title={cu.table.manualEditHint}
                              >
                                <ShieldCheck className="w-2.5 h-2.5" />
                                {cu.table.editedBadge}
                              </span>
                            )}
                            {c.is_unsubscribed && (
                              <span className="inline-flex items-center gap-0.5 bg-red-100 text-red-700 text-[10px] font-semibold px-1.5 py-0.5 rounded-full border border-red-200">
                                <BellOff className="w-2.5 h-2.5" />
                                {cu.table.unsubscribed}
                              </span>
                            )}
                            {!c.is_unsubscribed && c.pending_unsubscribe && (
                              <span className="inline-flex items-center gap-0.5 bg-amber-100 text-amber-700 text-[10px] font-semibold px-1.5 py-0.5 rounded-full border border-amber-200">
                                <BellOff className="w-2.5 h-2.5" />
                                {cu.table.pendingUnsub}
                              </span>
                            )}
                          </div>
                        )}
                      </td>
                      <td dir="ltr" className="px-3 py-3 text-slate-600 font-mono cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.phone || '—'}
                      </td>
                      <td className="px-3 py-3 text-slate-500 cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.email || '—'}
                      </td>
                      <td className="px-3 py-3 cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        <Badge label={c.status_label} variant={segmentVariant(c.status)} />
                      </td>
                      <td className="px-3 py-3 cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        <Badge
                          label={String(c.rfm_scores?.total ?? c.rfm_total_score ?? 0)}
                          variant={rfmVariant(c.rfm_scores?.total ?? c.rfm_total_score ?? 0)}
                        />
                      </td>
                      <td className="px-3 py-3 text-slate-600 whitespace-nowrap cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.rfm_segment_label || '—'}
                      </td>
                      <td className="px-3 py-3 text-slate-700 font-semibold cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.orders_count ?? c.total_orders}
                      </td>
                      <td className="px-3 py-3 text-slate-700 whitespace-nowrap cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {(c.total_spent ?? c.total_spend).toLocaleString(locale)} {cu.currency}
                      </td>
                      <td className="px-3 py-3 text-slate-500 whitespace-nowrap cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {formatCustomerDate(c.last_order_date ?? c.last_order_at, locale)}
                      </td>
                      <td className="px-3 py-3 cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.source === 'manual' ? (
                          <span className="text-xs text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">{c.source_label}</span>
                        ) : (
                          <span className="text-xs text-slate-500">{c.source_label}</span>
                        )}
                      </td>
                      {/* Quick delete */}
                      <td className="px-3 py-3">
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDelete(c.id) }}
                          className="text-slate-300 hover:text-red-500 transition-colors"
                          title={cu.table.deleteCustomer}
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination */}
        {pages > 1 && (
          <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100">
            <span className="text-xs text-slate-500">
              {cu.table.pageOf
                .replace('{page}', String(page))
                .replace('{pages}', String(pages))
                .replace('{total}', String(total))}
            </span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="btn-secondary text-xs py-1 px-3 disabled:opacity-40 inline-flex items-center gap-1"
              >
                <PrevIcon className="w-3.5 h-3.5" />
                {cu.table.prev}
              </button>
              <button
                disabled={page >= pages}
                onClick={() => setPage((p) => p + 1)}
                className="btn-secondary text-xs py-1 px-3 disabled:opacity-40 inline-flex items-center gap-1"
              >
                {cu.table.next}
                <NextIcon className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Name-cleanup Modal — "تنظيف أسماء العملاء"
          Shows the preview of customer names that the cleaner thinks
          need work, with per-row checkboxes and two apply buttons
          ("Apply selected" / "Apply high-confidence only"). Strictly
          scoped to the current tenant on the backend; this UI just
          shows whatever the preview endpoint returned. */}
      {nameCleanupOpen && (
        <div dir={dir} className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div dir={dir} className="bg-white rounded-xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col">
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
              <div className="flex items-center gap-2">
                <div className="w-9 h-9 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center">
                  <Sparkles className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="text-base font-semibold text-slate-900">
                    {cu.nameCleanup.title}
                  </h3>
                  <p className="text-[11px] text-slate-500">
                    {cu.nameCleanup.subtitle}
                  </p>
                </div>
              </div>
              <button
                onClick={closeNameCleanup}
                disabled={nameCleanupApplying}
                className="text-slate-400 hover:text-slate-600 disabled:opacity-40"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Body */}
            <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
              {nameCleanupLoading && (
                <div className="flex flex-col items-center justify-center py-12 text-slate-500 text-sm gap-3">
                  <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
                  {cu.nameCleanup.scanning}
                </div>
              )}

              {!nameCleanupLoading && nameCleanupError && (
                <div className="rounded-lg border border-red-200 bg-red-50 text-red-700 text-xs p-3">
                  {nameCleanupError}
                </div>
              )}

              {!nameCleanupLoading && nameCleanupResult && (
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 text-emerald-800 text-sm p-3 flex items-center gap-2">
                  <ShieldCheck className="w-5 h-5" />
                  <span>
                    {cu.nameCleanup.appliedSummary.replace('{applied}', String(nameCleanupResult.applied))}
                    {nameCleanupResult.skipped > 0
                      ? cu.nameCleanup.skippedSuffix.replace('{skipped}', String(nameCleanupResult.skipped))
                      : ''}
                  </span>
                </div>
              )}

              {!nameCleanupLoading && nameCleanupSummary && (
                <>
                  <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
                    <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                      <div className="text-slate-500">{cu.nameCleanup.totalCustomers}</div>
                      <div className="text-lg font-semibold text-slate-800 mt-1">
                        {nameCleanupSummary.totalCustomers.toLocaleString(locale)}
                      </div>
                      <div className="text-[10px] text-slate-400 mt-0.5">
                        {cu.nameCleanup.scanned.replace('{count}', nameCleanupSummary.totalScanned.toLocaleString(locale))}
                      </div>
                    </div>
                    <div className="rounded-lg border border-blue-200 bg-blue-50/60 p-3">
                      <div className="text-blue-700">{cu.nameCleanup.needsCleanup}</div>
                      <div className="text-lg font-semibold text-blue-800 mt-1">
                        {nameCleanupSummary.matchCount.toLocaleString(locale)}
                      </div>
                      <div className="text-[10px] text-blue-500 mt-0.5">
                        {cu.nameCleanup.ofTotal.replace('{total}', nameCleanupSummary.totalCustomers.toLocaleString(locale))}
                      </div>
                    </div>
                    <div className="rounded-lg border border-emerald-200 bg-emerald-50/60 p-3">
                      <div className="text-emerald-700">{cu.nameCleanup.highConfidence}</div>
                      <div className="text-lg font-semibold text-emerald-800 mt-1">
                        {nameCleanupSummary.highConfidence.toLocaleString(locale)}
                      </div>
                    </div>
                    <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-3">
                      <div className="text-amber-700">{cu.nameCleanup.needsReview}</div>
                      <div className="text-lg font-semibold text-amber-800 mt-1">
                        {nameCleanupSummary.lowConfidence.toLocaleString(locale)}
                      </div>
                    </div>
                  </div>

                  {nameCleanupSummary.truncated && (
                    <div className="rounded-lg border border-amber-300 bg-amber-50 text-amber-900 text-xs p-3 flex items-start gap-2">
                      <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
                      <div>
                        {cu.nameCleanup.tooManyResults
                          .replace('{max}', nameCleanupSummary.maxItems.toLocaleString(locale))
                          .replace('{match}', nameCleanupSummary.matchCount.toLocaleString(locale))}
                      </div>
                    </div>
                  )}
                </>
              )}

              {!nameCleanupLoading && nameCleanupSummary && (
                <div className="flex items-center justify-between gap-2 flex-wrap text-[11px]">
                  {/* Save-state indicator (left side, where data lives). */}
                  <div className="flex items-center gap-3 text-slate-500">
                    {nameCleanupSaveState === 'saving' && (
                      <span className="inline-flex items-center gap-1 text-blue-600">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        {cu.nameCleanup.savingDraft}
                      </span>
                    )}
                    {nameCleanupSaveState === 'saved' && nameCleanupLastSavedAt && (
                      <span className="inline-flex items-center gap-1 text-emerald-600">
                        <Check className="w-3.5 h-3.5" />
                        {cu.nameCleanup.saved}
                      </span>
                    )}
                    {nameCleanupSaveState === 'error' && (
                      <span className="inline-flex items-center gap-1 text-rose-600">
                        <AlertTriangle className="w-3.5 h-3.5" />
                        {cu.nameCleanup.saveFailed}
                      </span>
                    )}
                    {nameCleanupSummary.draftEdited > 0 && (
                      <span className="text-slate-500">
                        {cu.nameCleanup.draftSaved.replace('{edited}', nameCleanupSummary.draftEdited.toLocaleString(locale))}
                        {nameCleanupSummary.draftSkipped > 0 && (
                          <span className="text-slate-400">
                            {' '}{cu.nameCleanup.draftSkipped.replace('{skipped}', nameCleanupSummary.draftSkipped.toLocaleString(locale))}
                          </span>
                        )}
                      </span>
                    )}
                  </div>

                  {/* Session actions (right side). */}
                  <div className="flex items-center gap-2 flex-wrap">
                    <button
                      type="button"
                      onClick={openNameCleanup}
                      disabled={nameCleanupLoading || nameCleanupApplying}
                      className="btn-secondary text-xs px-2.5 py-1 flex items-center gap-1.5 disabled:opacity-40"
                      title={cu.nameCleanup.rescanTitle}
                    >
                      <RefreshCw className="w-3 h-3" />
                      {cu.nameCleanup.rescan}
                    </button>
                    <button
                      type="button"
                      onClick={saveCleanupDraftNow}
                      disabled={
                        nameCleanupApplying || nameCleanupSaveState === 'saving'
                      }
                      className="btn-secondary text-xs px-2.5 py-1 flex items-center gap-1.5 disabled:opacity-40"
                    >
                      <Save className="w-3 h-3" />
                      {cu.nameCleanup.saveContinue}
                    </button>
                    {nameCleanupSummary.draftEdited + nameCleanupSummary.draftSkipped > 0 && (
                      <button
                        type="button"
                        onClick={discardCleanupDraft}
                        disabled={nameCleanupApplying}
                        className="text-xs px-2.5 py-1 flex items-center gap-1.5 text-slate-500 hover:text-rose-600 disabled:opacity-40"
                        title={cu.nameCleanup.discardDraftTitle}
                      >
                        <Trash2 className="w-3 h-3" />
                        {cu.nameCleanup.discardDraft}
                      </button>
                    )}
                  </div>
                </div>
              )}

              {/* Per-reason category chips (May 2026) — sit ABOVE
                  the existing confidence chip row so the merchant
                  can drill down by reason first (مصدر / مدينة /
                  بدون اسم / كلمة زائدة) and then narrow further by
                  confidence. Counts come from the FULL match
                  population so badges don't collapse to zero on the
                  first selection. */}
              {!nameCleanupLoading && nameCleanupItems.length > 0 && nameCleanupCategoryCounts && (
                <div className="flex items-center gap-1.5 flex-wrap pb-1 border-b border-slate-100">
                  <Filter className="w-3 h-3 text-slate-400" />
                  <span className="text-[11px] text-slate-500">{cu.nameCleanup.byReason}</span>
                  {([
                    'any',
                    'source_label_name',
                    'location_label_name',
                    'placeholder_name',
                    'suspicious_suffix',
                    'generic_bad_name',
                    'other',
                  ] as const).map(key => {
                    const label = key === 'any'
                      ? cu.nameCleanup.categories.all
                      : cu.nameCleanup.categories[key]
                    const count = key === 'any'
                      ? Object.values(nameCleanupCategoryCounts).reduce((a, b) => a + b, 0)
                      : (nameCleanupCategoryCounts[key as NameCleanupCategory] ?? 0)
                    // Don't render zero-count category chips except
                    // ``any`` (always available) — keeps the chip row
                    // short on clean tenants.
                    if (count === 0 && key !== 'any') return null
                    const active = nameCleanupCategory === key
                    return (
                      <button
                        key={key}
                        type="button"
                        onClick={() => setNameCleanupCategory(key)}
                        className={
                          'text-[11px] px-2 py-0.5 rounded-full border transition ' +
                          (active
                            ? 'bg-rose-600 text-white border-rose-600'
                            : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50')
                        }
                      >
                        {label}
                        <span className={
                          'ms-1 text-[10px] ' +
                          (active ? 'text-rose-50' : 'text-slate-400')
                        }>
                          ({count.toLocaleString(locale)})
                        </span>
                      </button>
                    )
                  })}
                </div>
              )}

              {!nameCleanupLoading && nameCleanupSummary && nameCleanupItems.length > 0 && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  <Filter className="w-3 h-3 text-slate-400" />
                  {([
                    'all',
                    'pending',
                    'edited',
                    'high',
                    'low',
                    'opted_out',
                  ] as const).map(key => ({
                    key,
                    label: cu.nameCleanup.filters[key],
                    count:
                      key === 'all'
                        ? nameCleanupItems.filter(it => !cleanupRowIsSkipped(it.customer_id)).length
                        : key === 'pending'
                          ? nameCleanupItems.filter(it => !cleanupRowIsEdited(it.customer_id)).length
                          : key === 'edited'
                            ? nameCleanupItems.filter(it => cleanupRowIsEdited(it.customer_id) && !cleanupRowIsSkipped(it.customer_id)).length
                            : key === 'high'
                              ? nameCleanupItems.filter(it => it.confidence === 'high' && !cleanupRowIsSkipped(it.customer_id)).length
                              : key === 'low'
                                ? nameCleanupItems.filter(it => it.confidence === 'low' && !cleanupRowIsSkipped(it.customer_id)).length
                                : nameCleanupItems.filter(it => it.marketing_opt_out_manual && !cleanupRowIsSkipped(it.customer_id)).length,
                  })).map(f => (
                    <button
                      key={f.key}
                      type="button"
                      onClick={() => setNameCleanupFilter(f.key)}
                      className={
                        'text-[11px] px-2 py-0.5 rounded-full border transition ' +
                        (nameCleanupFilter === f.key
                          ? 'bg-brand-600 text-white border-brand-600'
                          : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50')
                      }
                    >
                      {f.label}
                      <span className={
                        'ms-1 text-[10px] ' +
                        (nameCleanupFilter === f.key ? 'text-brand-50' : 'text-slate-400')
                      }>
                        ({f.count.toLocaleString(locale)})
                      </span>
                    </button>
                  ))}
                </div>
              )}

              {!nameCleanupLoading && nameCleanupItems.length === 0 && !nameCleanupError && (
                <div className="rounded-lg border border-slate-200 bg-slate-50 text-slate-600 text-sm p-6 text-center">
                  {nameCleanupResult
                    ? cu.nameCleanup.emptyAfterApply
                    : cu.nameCleanup.emptyClean}
                </div>
              )}

              {!nameCleanupLoading && nameCleanupItems.length > 0 && (
                <div className="rounded-lg border border-slate-200 overflow-hidden">
                  <div className="flex items-center justify-between px-3 py-2 bg-slate-50 border-b border-slate-200 text-xs">
                    {(() => {
                      // The select-all button operates ONLY on the
                      // rows currently visible (post-filter). We
                      // deliberately do NOT offer a "select all
                      // matches across categories" shortcut — with
                      // 1,800+ candidates a single click could
                      // mass-clear the entire customer table. The
                      // merchant must navigate by category chip,
                      // tick the visible rows, apply, then move on.
                      const allVisibleSelected = visibleCleanupItems.length > 0
                        && visibleCleanupItems.every(it => nameCleanupSelected.has(it.customer_id))
                      return (
                        <button
                          onClick={toggleCleanupSelectAll}
                          disabled={visibleCleanupItems.length === 0}
                          className="flex items-center gap-2 text-slate-700 hover:text-brand-600 disabled:opacity-40"
                        >
                          {allVisibleSelected ? (
                            <CheckSquare className="w-4 h-4 text-brand-600" />
                          ) : (
                            <Square className="w-4 h-4 text-slate-400" />
                          )}
                          <span>
                            {allVisibleSelected
                              ? cu.nameCleanup.deselectVisible
                              : cu.nameCleanup.selectAllVisible.replace(
                                  '{count}',
                                  visibleCleanupItems.length
                                    ? ` (${visibleCleanupItems.length.toLocaleString(locale)})`
                                    : '',
                                )}
                          </span>
                        </button>
                      )
                    })()}
                    <span className="text-slate-500">
                      {cu.nameCleanup.selectedOf
                        .replace('{selected}', nameCleanupSelected.size.toLocaleString(locale))
                        .replace('{visible}', visibleCleanupItems.length.toLocaleString(locale))}
                      {visibleCleanupItems.length !== nameCleanupItems.length && (
                        <span className="text-slate-400 ms-1">
                          {cu.nameCleanup.ofTotalItems.replace('{total}', nameCleanupItems.length.toLocaleString(locale))}
                        </span>
                      )}
                    </span>
                    <span className="text-slate-400 text-[10px] hidden sm:inline">
                      {cu.nameCleanup.clickRowHint}
                    </span>
                    {cleanupDriftedCount > 0 && (
                      <button
                        type="button"
                        onClick={refreshCleanupView}
                        className="ms-auto inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100 text-[11px]"
                        title={cu.nameCleanup.refreshPreviewTitle}
                      >
                        <RotateCcw className="w-3 h-3" />
                        <span>
                          {cu.nameCleanup.refreshPreview.replace('{count}', cleanupDriftedCount.toLocaleString(locale))}
                        </span>
                      </button>
                    )}
                  </div>
                  <div className="max-h-[50vh] overflow-y-auto divide-y divide-slate-100">
                    {visibleCleanupItems.length === 0 ? (
                      <div className="py-10 text-center text-xs text-slate-500">
                        {cu.nameCleanup.noRowsInFilter}
                      </div>
                    ) : visibleCleanupItems.map((it) => {
                      const checked = nameCleanupSelected.has(it.customer_id)
                      const isHigh = it.confidence === 'high'
                      const tokens = splitCleanupTokens(it.current_name)
                      const removed = cleanupResolvedRemoved(it)
                      const isCleared = cleanupRowIsCleared(it.customer_id)
                      const resolved = cleanupRowResolvedValue(it)
                      const isEdited = cleanupRowIsEdited(it.customer_id)
                      const isSkipped = cleanupRowIsSkipped(it.customer_id)

                      /* Helper: any button/chip handler is wrapped
                       * with this so the row-level click (which
                       * toggles selection) does NOT also fire when
                       * the merchant taps an actual action. With
                       * thousands of rows the merchant lives by
                       * clicking the row; an accidental
                       * selection-toggle on every chip click would
                       * be infuriating. */
                      const stopAnd = (fn: () => void) =>
                        (e: React.MouseEvent) => {
                          e.stopPropagation()
                          fn()
                        }

                      /* Row click toggles selection — but only when
                       * the row is not "skipped" (skipped rows
                       * aren't candidates for apply). Making the
                       * whole row a hit target is the dominant
                       * win for tenants reviewing thousands of
                       * names; the original 16×16 checkbox was
                       * the #1 UX complaint. */
                      const onRowClick = isSkipped
                        ? undefined
                        : () => toggleCleanupRow(it.customer_id)
                      return (
                        <div
                          key={it.customer_id}
                          onClick={onRowClick}
                          role={onRowClick ? 'button' : undefined}
                          aria-pressed={onRowClick ? checked : undefined}
                          tabIndex={onRowClick ? 0 : undefined}
                          onKeyDown={(e) => {
                            // Keyboard parity — Space/Enter on a
                            // focused row toggles selection the
                            // same way a mouse click does.
                            if (!onRowClick) return
                            if (e.key === ' ' || e.key === 'Enter') {
                              e.preventDefault()
                              onRowClick()
                            }
                          }}
                          className={
                            'px-3 py-3 flex items-start gap-3 select-none transition-colors ' +
                            (isSkipped
                              ? 'bg-slate-50/80 opacity-70'
                              : checked
                                ? 'bg-brand-50/50 cursor-pointer ring-1 ring-inset ring-brand-200'
                                : 'hover:bg-slate-50/80 cursor-pointer')
                          }
                        >
                          <button
                            type="button"
                            onClick={stopAnd(() => toggleCleanupRow(it.customer_id))}
                            disabled={isSkipped}
                            className="mt-0.5 text-slate-400 hover:text-brand-600 disabled:opacity-30"
                            aria-label={checked ? cu.nameCleanup.deselect : cu.nameCleanup.selectCustomer}
                          >
                            {checked ? (
                              <CheckSquare className="w-4 h-4 text-brand-600" />
                            ) : (
                              <Square className="w-4 h-4" />
                            )}
                          </button>

                          <div className="flex-1 min-w-0 space-y-2">
                            {/* Header row: phone + reason + confidence + state */}
                            <div className="flex items-center justify-between gap-2 text-[11px]">
                              <div className="flex items-center gap-2 min-w-0">
                                {it.phone && (
                                  <span dir="ltr" className="text-slate-400 shrink-0">
                                    {it.phone}
                                  </span>
                                )}
                                <span className="text-slate-500 truncate">
                                  {it.reason || '—'}
                                </span>
                              </div>
                              <div className="flex items-center gap-1.5 shrink-0">
                                {/* Category badge — coarse reason
                                    bucket from the cleaner verdict.
                                    Surfaced INLINE on every row so
                                    the merchant immediately sees
                                    "this is a TikTok suffix" vs
                                    "this is a city name" without
                                    parsing the Arabic reason text. */}
                                {it.category && (() => {
                                  const badgeLabel = cu.nameCleanup.categoryBadges[it.category as keyof typeof cu.nameCleanup.categoryBadges]
                                  const variantMap: Record<string, 'amber' | 'purple' | 'blue' | 'green' | 'slate'> = {
                                    source_label_name:    'amber',
                                    location_label_name:  'blue',
                                    placeholder_name:     'slate',
                                    suspicious_suffix:    'purple',
                                    generic_bad_name:     'slate',
                                    other:                'slate',
                                  }
                                  const variant = variantMap[it.category]
                                  return badgeLabel && variant
                                    ? <Badge label={badgeLabel} variant={variant} />
                                    : null
                                })()}
                                {it.marketing_opt_out_manual && (
                                  <Badge
                                    label={cu.nameCleanup.excludedFromCampaigns}
                                    variant="purple"
                                  />
                                )}
                                {isEdited && !isSkipped && (
                                  <Badge label={cu.nameCleanup.modified} variant="blue" />
                                )}
                                {isSkipped && (
                                  <Badge label={cu.nameCleanup.skipped} variant="slate" />
                                )}
                                <Badge
                                  label={isHigh ? cu.nameCleanup.highConf : cu.nameCleanup.review}
                                  variant={isHigh ? 'green' : 'amber'}
                                />
                              </div>
                            </div>

                            {/* Word chips: click to drop / restore each word.
                                Kept words are solid; dropped words are crossed
                                out — clicking either toggles state. */}
                            <div className="flex flex-wrap items-center gap-1.5">
                              {tokens.length === 0 ? (
                                <span className="text-slate-400 text-xs">
                                  {cu.nameCleanup.noWords}
                                </span>
                              ) : (
                                tokens.map((tok, idx) => {
                                  const isDropped = removed.has(idx) || isCleared
                                  return (
                                    <button
                                      key={idx}
                                      type="button"
                                      onClick={stopAnd(() => toggleCleanupWord(it, idx))}
                                      className={
                                        'inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs border transition ' +
                                        (isDropped
                                          ? 'border-slate-200 bg-slate-50 text-slate-400 line-through hover:text-slate-600'
                                          : 'border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100')
                                      }
                                      title={isDropped ? cu.nameCleanup.restoreWord : cu.nameCleanup.removeWord}
                                    >
                                      <span>{tok}</span>
                                      {isDropped ? (
                                        <RotateCcw className="w-3 h-3" />
                                      ) : (
                                        <X className="w-3 h-3" />
                                      )}
                                    </button>
                                  )
                                })
                              )}
                            </div>

                            {/* Resolved value preview + per-row actions */}
                            <div className="flex items-center justify-between gap-2 flex-wrap">
                              <div className="text-xs">
                                <span className="text-slate-500 me-1">{cu.nameCleanup.resultLabel}</span>
                                {resolved ? (
                                  <span className="font-medium text-slate-800">
                                    {resolved}
                                  </span>
                                ) : (
                                  <span className="inline-flex items-center gap-1 text-rose-600 font-medium">
                                    <Trash2 className="w-3 h-3" />
                                    {cu.nameCleanup.willClearName}
                                  </span>
                                )}
                              </div>
                              <div className="flex items-center gap-2 flex-wrap">
                                {isEdited && (
                                  <button
                                    type="button"
                                    onClick={stopAnd(() => resetCleanupRow(it.customer_id))}
                                    className="text-[11px] text-slate-500 hover:text-slate-700 inline-flex items-center gap-1"
                                  >
                                    <RotateCcw className="w-3 h-3" />
                                    {cu.nameCleanup.resetSuggestion}
                                  </button>
                                )}
                                <button
                                  type="button"
                                  onClick={stopAnd(() => skipCleanupRow(it.customer_id))}
                                  className={
                                    'text-[11px] inline-flex items-center gap-1 px-2 py-0.5 rounded-md border transition ' +
                                    (isSkipped
                                      ? 'border-slate-400 bg-slate-100 text-slate-700'
                                      : 'border-slate-200 text-slate-500 hover:bg-slate-50')
                                  }
                                  title={cu.nameCleanup.skipRow}
                                >
                                  <SkipForward className="w-3 h-3" />
                                  {isSkipped ? cu.nameCleanup.skipped : cu.nameCleanup.skip}
                                </button>
                                <button
                                  type="button"
                                  onClick={stopAnd(() => clearCleanupRow(it.customer_id))}
                                  className={
                                    'text-[11px] inline-flex items-center gap-1 px-2 py-0.5 rounded-md border transition ' +
                                    (isCleared
                                      ? 'border-rose-300 bg-rose-50 text-rose-700'
                                      : 'border-rose-200 text-rose-600 hover:bg-rose-50')
                                  }
                                >
                                  <Trash2 className="w-3 h-3" />
                                  {cu.nameCleanup.clearEntireName}
                                </button>
                                <CampaignExcludeControl
                                  customerId={it.customer_id}
                                  optedOut={!!it.marketing_opt_out_manual}
                                  customerLabel={it.current_name || it.phone || String(it.customer_id)}
                                  variant="inline-chip"
                                  onSuccess={(nextOptedOut) => {
                                    setNameCleanupItems(prev =>
                                      prev.map(row =>
                                        row.customer_id === it.customer_id
                                          ? { ...row, marketing_opt_out_manual: nextOptedOut }
                                          : row,
                                      ),
                                    )
                                    if (nextOptedOut) {
                                      setNameCleanupSelected(prev => {
                                        if (!prev.has(it.customer_id)) return prev
                                        const next = new Set(prev)
                                        next.delete(it.customer_id)
                                        return next
                                      })
                                    }
                                  }}
                                />
                              </div>
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}

              {!nameCleanupLoading && (
                <div className="rounded-lg border border-blue-100 bg-blue-50/60 text-blue-800 text-[11px] p-3 leading-relaxed space-y-1">
                  <div>
                    <Info className="w-3.5 h-3.5 inline me-1" />
                    {cu.nameCleanup.helpPrimary}
                  </div>
                  <div className="ps-5">
                    {cu.nameCleanup.helpSecondary}
                  </div>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-between px-6 py-3 border-t border-slate-100 bg-slate-50/60">
              <button
                onClick={closeNameCleanup}
                disabled={nameCleanupApplying}
                className="btn-secondary text-xs px-3 py-1.5 disabled:opacity-40"
              >
                {cu.nameCleanup.close}
              </button>
              <div className="flex items-center gap-2">
                <button
                  onClick={applyCleanupHighConfidence}
                  disabled={
                    nameCleanupApplying
                    || nameCleanupLoading
                    || (nameCleanupSummary?.highConfidence ?? 0) === 0
                  }
                  className="btn-secondary text-xs px-3 py-1.5 flex items-center gap-1.5 disabled:opacity-40"
                  title={cu.nameCleanup.applyHighTitle}
                >
                  <ShieldCheck className="w-3.5 h-3.5" />
                  {cu.nameCleanup.applyHighOnly}
                </button>
                <button
                  onClick={applyCleanupSelected}
                  disabled={nameCleanupApplying || nameCleanupSelected.size === 0}
                  className="btn-primary text-xs px-3 py-1.5 flex items-center gap-1.5 disabled:opacity-40"
                  title={
                    nameCleanupSelected.size === 0
                      ? cu.nameCleanup.applySelectedHint
                      : cu.nameCleanup.applySelectedTitle.replace('{count}', String(nameCleanupSelected.size))
                  }
                >
                  {nameCleanupApplying ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <CheckSquare className="w-3.5 h-3.5" />
                  )}
                  {nameCleanupSelected.size === 0
                    ? cu.nameCleanup.applySelected
                    : cu.nameCleanup.applySelectedCount.replace('{count}', nameCleanupSelected.size.toLocaleString(locale))}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Add Customer Modal */}
      {showAdd && (
        <div dir={dir} className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div dir={dir} className="bg-white rounded-xl shadow-xl w-full max-w-md p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-900">
                {cu.addModal.title}
              </h3>
              <button
                onClick={() => {
                  setShowAdd(false)
                  setAddError('')
                }}
                className="text-slate-400 hover:text-slate-600"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">
                  <User className="w-3.5 h-3.5 inline me-1" />
                  {cu.addModal.nameLabel}
                </label>
                <input
                  type="text"
                  value={addName}
                  onChange={(e) => setAddName(e.target.value)}
                  placeholder={cu.table.namePlaceholder}
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">
                  <Phone className="w-3.5 h-3.5 inline me-1" />
                  {cu.addModal.phoneLabel}
                </label>
                <input
                  dir="ltr"
                  type="tel"
                  value={addPhone}
                  onChange={(e) => setAddPhone(e.target.value)}
                  placeholder="+966 5XXXXXXXX"
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">
                  <Mail className="w-3.5 h-3.5 inline me-1" />
                  {cu.addModal.emailLabel}
                </label>
                <input
                  dir="ltr"
                  type="email"
                  value={addEmail}
                  onChange={(e) => setAddEmail(e.target.value)}
                  placeholder="email@example.com"
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
                />
              </div>
            </div>

            {addError && (
              <p className="text-xs text-red-600 bg-red-50 rounded-lg px-3 py-2">
                {addError}
              </p>
            )}

            <div className="flex gap-2 justify-end pt-2">
              <button
                onClick={() => {
                  setShowAdd(false)
                  setAddError('')
                }}
                className="btn-secondary text-sm"
              >
                {cu.addModal.cancel}
              </button>
              <button
                onClick={handleAdd}
                disabled={addLoading}
                className="btn-primary text-sm flex items-center gap-2"
              >
                {addLoading && (
                  <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                )}
                {cu.addModal.submit}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Bulk / Delete-All Confirmation Modal ─────────────────────────── */}
      {deleteModal && (
        <div dir={dir} className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div dir={dir} className="bg-white rounded-2xl shadow-2xl w-full max-w-md">
            {/* Danger header */}
            <div className="bg-red-600 rounded-t-2xl px-6 py-5 flex items-start gap-3">
              <div className="w-10 h-10 bg-white/20 rounded-full flex items-center justify-center shrink-0">
                <Trash2 className="w-5 h-5 text-white" />
              </div>
              <div>
                <h3 className="text-base font-bold text-white">
                  {deleteModal === 'all'
                    ? cu.deleteModal.deleteAllTitle
                    : cu.deleteModal.deleteSelectedTitle.replace('{count}', String(selectedIds.size))}
                </h3>
                <p className="text-red-100 text-xs mt-1">
                  {cu.deleteModal.irreversible}
                </p>
              </div>
            </div>

            <div className="px-6 py-5 space-y-4">
              {/* Warning message */}
              <div className="bg-red-50 border border-red-200 rounded-xl p-4 space-y-2">
                <p className="text-sm font-semibold text-red-800">
                  {deleteModal === 'all'
                    ? cu.deleteModal.deleteAllBody.replace('{total}', total.toLocaleString(locale))
                    : cu.deleteModal.deleteSelectedBody.replace('{count}', String(selectedIds.size))}
                </p>
                <ul className="text-xs text-red-700 space-y-1 list-disc ps-5">
                  <li>{cu.deleteModal.bulletNoRestore}</li>
                  <li>{cu.deleteModal.bulletDataGone}</li>
                  <li>{cu.deleteModal.bulletCampaigns}</li>
                </ul>
              </div>

              {/* Confirmation input */}
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-2">
                  {cu.deleteModal.confirmPrompt}{' '}
                  <span className="font-mono font-bold text-red-600 bg-red-50 px-1.5 py-0.5 rounded">
                    {cu.deleteModal.confirmWord}
                  </span>
                </label>
                <input
                  type="text"
                  value={deleteConfirmText}
                  onChange={e => setDeleteConfirmText(e.target.value)}
                  placeholder={cu.deleteModal.confirmPlaceholder.replace('{word}', cu.deleteModal.confirmWord)}
                  className="w-full px-3 py-2 text-sm border-2 border-slate-200 rounded-lg focus:ring-0 focus:border-red-400 outline-none transition-colors"
                  autoFocus
                />
              </div>

              {/* Actions */}
              <div className="flex gap-3 pt-1">
                <button
                  onClick={() => { setDeleteModal(null); setDeleteConfirmText('') }}
                  className="flex-1 btn-secondary text-sm"
                  disabled={deleteLoading}
                >
                  {cu.deleteModal.cancel}
                </button>
                <button
                  onClick={handleBulkDelete}
                  disabled={deleteConfirmText !== cu.deleteModal.confirmWord || deleteLoading}
                  className="flex-1 flex items-center justify-center gap-2 text-sm bg-red-600 hover:bg-red-700 text-white rounded-lg py-2 font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {deleteLoading ? (
                    <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  ) : (
                    <Trash2 className="w-4 h-4" />
                  )}
                  {deleteLoading ? cu.deleteModal.deleting : cu.deleteModal.confirmDelete}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Customer Detail Side Panel */}
      {selectedCustomer && (
        <div dir={dir} className="fixed inset-0 bg-black/40 flex justify-end z-50">
          <div
            className="absolute inset-0"
            onClick={() => setSelectedCustomer(null)}
          />
          <div dir={dir} className="relative bg-white w-full max-w-sm h-full max-h-dvh shadow-xl overflow-y-auto">
            <div className="sticky top-0 bg-white border-b border-slate-100 px-5 py-4 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-900">
                {cu.drawer.title}
              </h3>
              <button
                onClick={() => setSelectedCustomer(null)}
                className="text-slate-400 hover:text-slate-600"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="p-5 space-y-5">
              <div className="text-center space-y-2">
                <div className={`w-16 h-16 rounded-full flex items-center justify-center mx-auto ${selectedCustomer.is_unsubscribed ? 'bg-red-50' : 'bg-brand-50'}`}>
                  {selectedCustomer.is_unsubscribed
                    ? <BellOff className="w-8 h-8 text-red-400" />
                    : <User className="w-8 h-8 text-brand-500" />
                  }
                </div>
                <h4 className="text-base font-semibold text-slate-900">
                  {selectedCustomer.display_name || selectedCustomer.name}
                </h4>
                {selectedCustomer.is_unsubscribed && (
                  <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-700 text-center space-y-1">
                    <p className="font-semibold flex items-center justify-center gap-1">
                      <BellOff className="w-3.5 h-3.5" />
                      {cu.drawer.unsubscribedTitle}
                    </p>
                    <p className="text-red-500">{cu.drawer.unsubscribedBody}</p>
                    <p className="text-slate-500 text-[10px]">{cu.drawer.unsubscribedNote}</p>
                  </div>
                )}
                {!selectedCustomer.is_unsubscribed && selectedCustomer.pending_unsubscribe && (
                  <div className="bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-xs text-amber-800 text-center space-y-1">
                    <p className="font-semibold flex items-center justify-center gap-1">
                      <BellOff className="w-3.5 h-3.5" />
                      {cu.drawer.pendingUnsubTitle}
                    </p>
                    <p className="text-amber-700">{cu.drawer.pendingUnsubBody}</p>
                    <p className="text-slate-500 text-[10px]">{cu.drawer.pendingUnsubNote}</p>
                  </div>
                )}
                <div className="flex flex-wrap items-center justify-center gap-1.5">
                  <Badge
                    label={selectedCustomer.status_label}
                    variant={segmentVariant(selectedCustomer.status)}
                  />
                  <span className="text-[10px] text-slate-400 px-1">{cu.drawer.smartClassification}</span>
                </div>
                {selectedCustomer.source === 'manual' && (
                  <span className="block text-xs text-blue-600">
                    {selectedCustomer.source_label}
                  </span>
                )}
              </div>

              {/* Manual segments — merchant-curated, drawn from the same
                  Nahla registry as the auto chip strip. We do NOT allow
                  free-form tags by design (segment_key MUST validate
                  against `services.nahla_segments`). */}
              <ManualSegmentsSection
                cu={cu}
                lang={lang}
                dir={dir}
                customer={selectedCustomer}
                segments={segments}
                onChange={async (next) => {
                  setSelectedCustomer(next)
                  // Refresh the row inside the table so the new
                  // tags / opt-out icon update without a full page
                  // reload.
                  setCustomers(prev => prev.map(c => c.id === next.id ? next : c))
                }}
                onRequireListReload={load}
              />

              <div className="space-y-3">
                <div className="flex items-center gap-3 text-sm">
                  <Phone className="w-4 h-4 text-slate-400" />
                  <span dir="ltr" className="font-mono text-slate-700">
                    {selectedCustomer.phone || '—'}
                  </span>
                </div>
                <div className="flex items-center gap-3 text-sm">
                  <Mail className="w-4 h-4 text-slate-400" />
                  <span className="text-slate-700">
                    {selectedCustomer.email || '—'}
                  </span>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {selectedCustomer.orders_count ?? selectedCustomer.total_orders}
                  </p>
                  <p className="text-xs text-slate-500">{cu.drawer.orders}</p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {(selectedCustomer.total_spent ?? selectedCustomer.total_spend).toLocaleString(locale)}
                  </p>
                  <p className="text-xs text-slate-500">{cu.drawer.spend.replace('{currency}', cu.currency)}</p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {(selectedCustomer.avg_order_value ?? selectedCustomer.average_order_value).toLocaleString(locale)}
                  </p>
                  <p className="text-xs text-slate-500">
                    {cu.drawer.avgOrder.replace('{currency}', cu.currency)}
                  </p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {Math.round(selectedCustomer.churn_risk_score * 100)}%
                  </p>
                  <p className="text-xs text-slate-500">{cu.drawer.churnRisk}</p>
                </div>
              </div>

              <div className="space-y-2 text-xs">
                <div className="flex justify-between">
                  <span className="text-slate-500">{cu.drawer.rfmSegment}</span>
                  <span className="text-slate-700">
                    {selectedCustomer.rfm_segment_label || '—'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">{cu.drawer.rfmScore}</span>
                  <span className="text-slate-700 font-mono">
                    {selectedCustomer.rfm_scores?.code || selectedCustomer.rfm_code || '000'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">{cu.drawer.firstOrder}</span>
                  <span className="text-slate-700">
                    {formatCustomerDate(selectedCustomer.first_order_date ?? selectedCustomer.first_order_at, locale)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">{cu.drawer.lastOrder}</span>
                  <span className="text-slate-700">
                    {formatCustomerDate(selectedCustomer.last_order_date ?? selectedCustomer.last_order_at, locale)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">{cu.drawer.firstSeen}</span>
                  <span className="text-slate-700">
                    {formatCustomerDate(selectedCustomer.first_seen_at, locale)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">{cu.drawer.lastRecalc}</span>
                  <span className="text-slate-700">
                    {formatCustomerDate(selectedCustomer.metrics_computed_at, locale)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">{cu.drawer.returning}</span>
                  <span className="text-slate-700">
                    {selectedCustomer.is_returning ? cu.yes : cu.no}
                  </span>
                </div>
              </div>

              <button
                onClick={() => handleDelete(selectedCustomer.id)}
                className="w-full text-xs text-red-600 hover:text-red-700 hover:bg-red-50 rounded-lg py-2 transition-colors"
              >
                {cu.drawer.deleteCustomer}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Sub-components ────────────────────────────────────────────────────────────

/**
 * Drawer block that lets the merchant pin / unpin Nahla *manual*
 * segment tags on this customer, plus toggle the two marketing
 * preference flags (opt-out + test-recipient).
 *
 * Hard rule: manual tags MUST come from the official Nahla segment
 * registry — we render a dropdown, never a free-form input. The
 * backend re-validates the key on every POST so even a stale UI
 * cannot inject arbitrary strings.
 */
interface ManualSegmentsSectionProps {
  cu: CustomersPageLabels
  lang: Lang
  dir: 'ltr' | 'rtl'
  customer: CustomerRecord
  segments: CustomerSegmentMeta[]
  onChange: (next: CustomerRecord) => void | Promise<void>
  onRequireListReload?: () => void
}

function ManualSegmentsSection({
  cu, lang, dir, customer, segments, onChange, onRequireListReload,
}: ManualSegmentsSectionProps) {
  const [adding, setAdding] = useState<string>('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string>('')

  const manualKeys = customer.manual_segments || []
  const optedOut = customer.marketing_opt_out_manual
  const isTestRecipient = customer.is_campaign_test_recipient

  // Pool of segments the merchant can still add — exclude already
  // pinned ones + the meta "all" segment which would be meaningless
  // as a per-customer tag.
  const addableSegments = segments.filter(
    s => s.key !== 'all' && !manualKeys.includes(s.key),
  )

  /**
   * Unified override handler — replaces the legacy add/remove pair.
   *
   *   force_include → add to segment regardless of auto classifier
   *   force_exclude → remove from segment regardless of auto classifier
   *   auto          → drop the override; classifier alone decides
   *
   * The backend returns the updated ``manual_sources`` map and
   * ``is_member`` for the segment, so we patch the customer record
   * locally without a full refetch.
   */
  const handleOverride = async (
    key: string,
    mode: 'force_include' | 'force_exclude' | 'auto',
  ) => {
    if (!key) return
    setBusy(key)
    setError('')
    try {
      const res = await customersApi.overrideSegment(customer.id, key, mode)
      if (!res.ok) {
        setError(res.message || cu.manualSegments.updateSegmentFailed)
        return
      }
      // Optimistic merge: rebuild the segment_sources map from the
      // response. ``manual_sources`` is ``Record<key, "include"|"exclude">``
      // while ``segment_sources`` is the richer per-segment breakdown
      // (automatic / manual_include / manual_exclude / is_member) the
      // backend computes for the list endpoint, so we merge by patching
      // each touched key against the previous snapshot.
      const manualModes = res.manual_sources || {}
      const prevSources = customer.segment_sources || {}
      const nextSources: CustomerRecord['segment_sources'] = { ...prevSources }
      // Patch every key the response mentions (these are the only ones
      // that can have changed) and also the touched key itself in case
      // the response cleared it.
      const touchedKeys = new Set<string>([key, ...Object.keys(manualModes)])
      touchedKeys.forEach(k => {
        const prev = prevSources[k] || {
          automatic: false, manual_include: false,
          manual_exclude: false, is_member: false,
        }
        const m = manualModes[k]
        const manual_include = m === 'include'
        const manual_exclude = m === 'exclude'
        const is_member =
          k === key && typeof res.is_member === 'boolean'
            ? res.is_member
            : (prev.automatic || manual_include) && !manual_exclude
        nextSources[k] = {
          automatic: prev.automatic,
          manual_include,
          manual_exclude,
          is_member,
        }
      })
      const manual_segments = Object.entries(nextSources || {})
        .filter(([, v]) => v?.manual_include)
        .map(([k]) => k)
      const manual_segments_labels = manual_segments.map(k => {
        const seg = segments.find(s => s.key === k)
        return seg ? segmentDisplayLabel(seg, lang) : k
      })
      await onChange({
        ...customer,
        segment_sources: nextSources,
        manual_segments,
        manual_segments_labels,
      })
      // The chip strip counts on the list page depend on the same
      // unified-membership formula, so trigger a reload so the
      // merchant sees the new counts immediately.
      onRequireListReload?.()
    } catch (err: any) {
      setError(err?.detail || err?.message || cu.manualSegments.updateSegmentFailed)
    } finally {
      setBusy(null)
    }
  }

  const handleToggleTest = async () => {
    setBusy('__test__')
    setError('')
    try {
      const res = await customersApi.updateMarketingPreferences(customer.id, {
        is_campaign_test_recipient: !isTestRecipient,
      })
      await onChange({
        ...customer,
        is_campaign_test_recipient: res.is_campaign_test_recipient,
      })
    } catch (err: any) {
      setError(err?.detail || err?.message || cu.manualSegments.updateTestFailed)
    } finally {
      setBusy(null)
    }
  }

  // Unified rendering data.
  //
  // The drawer shows ALL Nahla segments (except "all" which is a meta
  // bucket) so the merchant always sees the full vocabulary — and for
  // each one we show:
  //   * whether the customer is a member right now (final membership)
  //   * a SINGLE small badge if the merchant overrode the auto signal:
  //     "مضاف يدويًا" / "مستبعد يدويًا"  (never "auto" / "manual" as
  //     two separate concepts).
  //
  // Membership = automatic ∨ force_include, MINUS force_exclude.
  // Same formula the backend uses for /customers, /campaigns audience
  // and the chip counts — so what the merchant sees here is exactly
  // what the campaign wizard will target.
  const sources = customer.segment_sources || {}
  const renderableSegments = segments.filter(s => s.key !== 'all')

  return (
    <div className="space-y-3 border border-slate-100 rounded-xl p-3 bg-slate-50/40">
      <div className="flex items-center justify-between">
        <h5 className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
          <Tag className="w-3.5 h-3.5 text-slate-400" />
          {cu.manualSegments.title}
        </h5>
        <span className="text-[10px] text-slate-400">
          {cu.manualSegments.subtitle}
        </span>
      </div>

      {/* Per-segment row. Each row exposes the three actions the spec
          requires:
            ➕ "إضافة لهذا التصنيف"      → mode=force_include
            ➖ "استبعاد من هذا التصنيف"   → mode=force_exclude
            ↻ "العودة للتصنيف التلقائي" → mode=auto

          The active-state badge ("مضاف يدويًا" / "مستبعد يدويًا") is
          the ONLY place where overrides are surfaced — we never tell
          the merchant that "manual" and "auto" are two separate
          classifications. */}
      <div className="space-y-1.5">
        {renderableSegments.map(s => {
          const src = sources[s.key] || {
            automatic:       false,
            manual_include:  false,
            manual_exclude:  false,
            is_member:       false,
          } as any
          const isMember     = !!src.is_member
          const isOverridden = src.manual_include || src.manual_exclude
          const overrideKind = src.manual_exclude
            ? cu.manualSegments.manuallyExcluded
            : src.manual_include
              ? cu.manualSegments.manuallyAdded
              : ''
          const pillCls = isMember
            ? 'text-emerald-700 bg-emerald-50 border-emerald-200'
            : src.manual_exclude
              ? 'text-slate-500 bg-slate-100 border-slate-200 line-through'
              : 'text-slate-400 bg-white border-slate-200'

          return (
            <div
              key={s.key}
              className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-lg bg-white border border-slate-100"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span
                  className={`inline-flex items-center gap-1 text-[11px] font-semibold border px-2 py-0.5 rounded-full ${pillCls}`}
                  title={
                    isMember
                      ? cu.manualSegments.inSegmentTitle
                      : cu.manualSegments.outSegmentTitle
                  }
                >
                  {segmentDisplayLabel(s, lang)}
                </span>
                {isOverridden && (
                  <span
                    className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
                      src.manual_exclude
                        ? 'bg-rose-50 text-rose-700 border border-rose-100'
                        : 'bg-amber-50 text-amber-700 border border-amber-100'
                    }`}
                    title={cu.manualSegments.overrideHint}
                  >
                    {overrideKind}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-0.5">
                <button
                  type="button"
                  disabled={busy === s.key || src.manual_include}
                  onClick={() => handleOverride(s.key, 'force_include')}
                  className="p-1 rounded hover:bg-emerald-50 text-emerald-600 disabled:opacity-30 disabled:hover:bg-transparent"
                  title={cu.manualSegments.addToSegment}
                >
                  <Plus className="w-3.5 h-3.5" />
                </button>
                <button
                  type="button"
                  disabled={busy === s.key || src.manual_exclude}
                  onClick={() => handleOverride(s.key, 'force_exclude')}
                  className="p-1 rounded hover:bg-rose-50 text-rose-600 disabled:opacity-30 disabled:hover:bg-transparent"
                  title={cu.manualSegments.excludeFromSegment}
                >
                  <Minus className="w-3.5 h-3.5" />
                </button>
                <button
                  type="button"
                  disabled={busy === s.key || !isOverridden}
                  onClick={() => handleOverride(s.key, 'auto')}
                  className="p-1 rounded hover:bg-slate-100 text-slate-500 disabled:opacity-30 disabled:hover:bg-transparent"
                  title={cu.manualSegments.resetToAuto}
                >
                  <RotateCcw className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          )
        })}
      </div>

      {/* Quick add — same dropdown surface, but it now triggers the
          unified force_include path so it round-trips through the
          override endpoint, keeping a single audit log of changes.

          We hide it when every segment already has a positive
          override / auto match; the merchant uses the inline ➕ on
          each row in that case.
       */}
      {addableSegments.length > 0 && (
        <div className="flex items-center gap-2 border-t border-slate-100 pt-2">
          <select
            value={adding}
            onChange={(e) => setAdding(e.target.value)}
            dir={dir}
            className="flex-1 ps-2 pe-2 py-1.5 text-xs border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none bg-white"
            disabled={!!busy}
          >
            <option value="">{cu.manualSegments.quickAddPlaceholder}</option>
            {addableSegments.map(s => (
              <option key={s.key} value={s.key}>{segmentDisplayLabel(s, lang)}</option>
            ))}
          </select>
          <button
            type="button"
            onClick={async () => {
              if (!adding) return
              await handleOverride(adding, 'force_include')
              setAdding('')
            }}
            disabled={!adding || !!busy}
            className="text-xs font-semibold text-brand-600 bg-brand-50 hover:bg-brand-100 border border-brand-200 px-2.5 py-1.5 rounded-lg disabled:opacity-50 flex items-center gap-1"
          >
            <Plus className="w-3.5 h-3.5" />
            {cu.manualSegments.add}
          </button>
        </div>
      )}

      {error && (
        <p className="text-[11px] text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1">
          {error}
        </p>
      )}

      {/* Marketing opt-out — same control as conversations inbox */}
      <div className="border-t border-slate-100 pt-3">
        <CampaignExcludeControl
          customerId={customer.id}
          phone={customer.phone}
          optedOut={optedOut}
          customerLabel={customer.name || customer.phone || String(customer.id)}
          variant="button"
          onSuccess={async (nextOptedOut) => {
            await onChange({
              ...customer,
              marketing_opt_out_manual: nextOptedOut,
              marketing_opt_out_manual_at: nextOptedOut
                ? new Date().toISOString()
                : null,
            })
            onRequireListReload?.()
          }}
        />
      </div>

      {/* Quick "add to campaign test list" — internal flag, no
          merchant-visible tag is created. */}
      <div className="border-t border-slate-100 pt-3 space-y-2">
        <label className="flex items-start gap-2.5 cursor-pointer select-none">
          <ToggleSwitch
            checked={isTestRecipient}
            disabled={busy === '__test__'}
            onClick={handleToggleTest}
            activeClass="bg-emerald-500"
            inactiveClass="bg-slate-200"
          />
          <div className="flex-1 min-w-0">
            <p className="text-xs font-semibold text-slate-800 flex items-center gap-1">
              <Beaker className="w-3.5 h-3.5 text-slate-400" />
              {cu.manualSegments.testListTitle}
            </p>
            <p className="text-[11px] text-slate-500 leading-snug">
              {cu.manualSegments.testListBody}
            </p>
          </div>
        </label>
      </div>
    </div>
  )
}


/**
 * Horizontal scrollable row of Nahla segment chips.
 *
 * Reuses the SAME registry as the campaign wizard
 * (`/customers/segments` + `/campaigns/wizard/segments`) so the chip
 * label "VIP عملاء" + count here always equals what the wizard's
 * Step 2 shows for the same tenant.
 *
 * The icon string from the backend is intentionally NOT rendered as an
 * arbitrary lucide-react component to avoid bundling the entire icon
 * library — we use a small, fixed visual treatment per chip instead.
 */
interface SegmentChipsProps {
  cu: CustomersPageLabels
  lang: Lang
  locale: string
  segments: CustomerSegmentMeta[]
  loading: boolean
  active: string
  onSelect: (key: string) => void
  campaignExcludedActive: boolean
  onToggleCampaignExcluded: () => void
  campaignExcludedLabel: string
}

function SegmentChips({
  cu, lang, locale, segments, loading, active, onSelect,
  campaignExcludedActive, onToggleCampaignExcluded, campaignExcludedLabel,
}: SegmentChipsProps) {
  const [openInfo, setOpenInfo] = useState<string | null>(null)

  if (loading) {
    return (
      <div className="flex gap-2 overflow-hidden">
        {[0, 1, 2, 3, 4].map(i => (
          <div key={i} className="h-9 w-24 rounded-full bg-slate-100 animate-pulse" />
        ))}
      </div>
    )
  }
  if (!segments || segments.length === 0) return null

  const activeSeg = segments.find(s => s.key === active) ?? null
  const popoverSeg = openInfo ? segments.find(s => s.key === openInfo) : null
  const segmentsBeforeUnsub = segments.filter(s => s.key !== 'unsubscribed')
  const unsubscribedSeg = segments.find(s => s.key === 'unsubscribed')

  const renderSegmentChip = (seg: CustomerSegmentMeta) => {
    const isActive = active === seg.key
    const segLabel = segmentDisplayLabel(seg, lang)
    return (
      <div key={seg.key} className="relative shrink-0">
        <button
          onClick={() => onSelect(seg.key)}
          title={seg.description_ar}
          className={
            'inline-flex items-center gap-2 ps-3.5 pe-2 py-1.5 rounded-full text-xs font-medium transition-colors border ' +
            (seg.key === 'unsubscribed'
              ? (isActive
                  ? 'bg-red-500 text-white border-red-500 shadow-sm'
                  : 'bg-red-50 text-red-600 border-red-200 hover:bg-red-100 hover:border-red-300')
              : (isActive
                  ? 'bg-brand-500 text-white border-brand-500 shadow-sm'
                  : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50 hover:border-slate-300'))
          }
        >
          {seg.key === 'unsubscribed' && <BellOff className="w-3 h-3 shrink-0" />}
          <span>{segLabel}</span>
          <span
            className={
              'inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 rounded-full text-[10px] font-semibold ' +
              (seg.key === 'unsubscribed'
                ? (isActive ? 'bg-white/20 text-white' : 'bg-red-100 text-red-600')
                : (isActive ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-500'))
            }
          >
            {seg.customer_count.toLocaleString(locale)}
          </span>
          <button
            type="button"
            aria-label={cu.segments.segmentInfoAria.replace('{label}', segLabel)}
            onClick={(e) => {
              e.stopPropagation()
              setOpenInfo(openInfo === seg.key ? null : seg.key)
            }}
            className={
              'ms-0.5 inline-flex items-center justify-center w-5 h-5 rounded-full transition-colors ' +
              (isActive
                ? 'text-white/80 hover:bg-white/15'
                : (seg.key === 'unsubscribed'
                    ? 'text-red-400 hover:bg-red-100 hover:text-red-600'
                    : 'text-slate-400 hover:bg-slate-100 hover:text-slate-600'))
            }
          >
            <Info className="w-3.5 h-3.5" />
          </button>
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 overflow-x-auto pb-1 -mb-1">
        {segmentsBeforeUnsub.map(renderSegmentChip)}

        <div className="relative shrink-0">
          <button
            type="button"
            onClick={onToggleCampaignExcluded}
            title={cu.segments.campaignExcludedNoticeBody}
            className={
              'inline-flex items-center gap-2 ps-3.5 pe-3 py-1.5 rounded-full text-xs font-medium transition-colors border ' +
              (campaignExcludedActive
                ? 'bg-violet-600 text-white border-violet-600 shadow-sm'
                : 'bg-violet-50 text-violet-700 border-violet-200 hover:bg-violet-100 hover:border-violet-300')
            }
          >
            <Megaphone className="w-3 h-3 shrink-0" />
            <span>{campaignExcludedLabel}</span>
          </button>
        </div>

        {unsubscribedSeg && renderSegmentChip(unsubscribedSeg)}
      </div>

      {popoverSeg && (
        <div className="relative bg-white border border-slate-200 rounded-lg shadow-sm p-4">
          <button
            type="button"
            onClick={() => setOpenInfo(null)}
            className="absolute top-3 end-3 text-slate-400 hover:text-slate-600"
            aria-label={cu.segments.closeDefinition}
          >
            <X className="w-4 h-4" />
          </button>
          <p className="text-sm font-semibold text-slate-800 mb-1">
            {segmentDisplayLabel(popoverSeg, lang)}
          </p>
          <p className="text-xs text-slate-500 leading-relaxed mb-3">
            {popoverSeg.criteria_ar || popoverSeg.description_ar}
          </p>
          <div className="flex flex-wrap gap-3 text-[11px] text-slate-400">
            <span>{cu.segments.currentCount}: <span className="text-slate-600 font-medium">{popoverSeg.customer_count.toLocaleString(locale)}</span></span>
            {popoverSeg.crm_statuses.length > 0 && (
              <span>{cu.segments.crmStatuses}: <span className="font-mono text-slate-600">{popoverSeg.crm_statuses.join(' · ')}</span></span>
            )}
            {popoverSeg.rfm_buckets.length > 0 && (
              <span>{cu.segments.rfmBuckets}: <span className="font-mono text-slate-600">{popoverSeg.rfm_buckets.join(' · ')}</span></span>
            )}
          </div>
        </div>
      )}

      {activeSeg && active !== 'all' && active !== 'unsubscribed' && (
        <p className="text-[11px] text-slate-500 ps-1">
          {cu.segments.showingFilter
            .replace('{count}', activeSeg.customer_count.toLocaleString(locale))
            .replace('{label}', segmentDisplayLabel(activeSeg, lang))}
        </p>
      )}

      {active === 'unsubscribed' && (
        <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-lg px-3 py-2.5 text-xs text-red-700">
          <BellOff className="w-4 h-4 mt-0.5 shrink-0 text-red-400" />
          <div className="space-y-0.5">
            <p className="font-semibold">{cu.segments.unsubscribedNoticeTitle}</p>
            <p className="text-red-600">{cu.segments.unsubscribedNoticeBody}</p>
            <p className="text-slate-500 mt-1">
              {cu.segments.unsubscribedHint}
            </p>
          </div>
        </div>
      )}

      {campaignExcludedActive && (
        <div className="flex items-start gap-2 bg-violet-50 border border-violet-200 rounded-lg px-3 py-2.5 text-xs text-violet-800">
          <Megaphone className="w-4 h-4 mt-0.5 shrink-0 text-violet-500" />
          <div className="space-y-0.5">
            <p className="font-semibold">{cu.segments.campaignExcludedNoticeTitle}</p>
            <p className="text-violet-700">{cu.segments.campaignExcludedNoticeBody}</p>
          </div>
        </div>
      )}
    </div>
  )
}
