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
  Tag,
  Beaker,
  Plus,
  Minus,
  RotateCcw,
  ShieldOff,
  Filter,
  Sparkles,
  ShieldCheck,
  Loader2,
  Save,
  SkipForward,
  UserMinus,
  Check,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  customersApi,
  type CustomerRecord,
  type CustomerSegmentMeta,
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

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—'
  try {
    return new Date(dateStr).toLocaleDateString('ar-SA', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
  } catch {
    return '—'
  }
}

export default function Customers() {
  useLanguage()
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
  const [marketingOptOutFilter, setMarketingOptOutFilter] = useState<'all' | 'in' | 'out'>('all')
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
  // Per-row edit state — keyed by customer_id. Initialised from the
  // preview response (which merges in any saved draft) and mutated
  // in-place as the merchant toggles chips. Autosave watches the
  // ``dirty`` flag and dribbles changes to the backend.
  const [nameCleanupRowState, setNameCleanupRowState] = useState<
    Record<number, CleanupRowState>
  >({})
  const [nameCleanupFilter, setNameCleanupFilter] = useState<CleanupFilter>('all')
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
          marketingOptOut: marketingOptOutFilter === 'all'
            ? undefined
            : marketingOptOutFilter === 'out',
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
      setAddError('الاسم ورقم الواتساب مطلوبان')
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
        err?.detail || err?.message || 'حدث خطأ أثناء إضافة العميل'
      setAddError(typeof msg === 'string' ? msg : JSON.stringify(msg))
    } finally {
      setAddLoading(false)
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm('هل أنت متأكد من حذف هذا العميل؟')) return
    try {
      await customersApi.delete(id)
      setSelectedCustomer(null)
      setSelectedIds(prev => { const s = new Set(prev); s.delete(id); return s })
      load()
    } catch {
      alert('حدث خطأ أثناء الحذف')
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
      alert('حدث خطأ أثناء الحذف')
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
      // Pre-tick every high-confidence row so the default "Apply
      // selected" run wipes the easy wins out in one click.
      setNameCleanupSelected(
        new Set(
          res.items
            .filter(it => it.confidence === 'high')
            .map(it => it.customer_id),
        ),
      )
      setNameCleanupSaveState('saved')
      setNameCleanupLastSavedAt(new Date().toISOString())
    } catch (err: any) {
      const msg = err?.detail || err?.message || 'تعذر تحميل المعاينة'
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
    setNameCleanupSaveState('idle')
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
      const msg = err?.detail || err?.message || 'تعذر حفظ المسودة'
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
  }, [])

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

  // Inline "exclude from marketing campaigns" toggle.
  //
  // What this does: flips ``Customer.extra_metadata.marketing_opt_out_manual``
  // for ONE row server-side, then optimistically reflects the new
  // state in the modal's local items array so the badge updates
  // without a full preview refetch.
  //
  // What this is NOT (three distinct buckets, by design):
  //   * NOT a customer-driven unsubscribe (the customer sending STOP).
  //   * NOT a Quality Engine auto-suppression (repeated quality_risk
  //     failures).
  // Each lives in a separate column / table server-side; conflating
  // them in the UI here would mislead the merchant about WHY a row
  // is excluded. The backend distinction is locked down in
  // ``/customers/name-cleanup/marketing-opt-out`` + the test suite.
  const toggleCleanupRowOptedOut = async (customerId: number, optedOut: boolean) => {
    try {
      await customersApi.nameCleanupMarketingOptOut({
        customer_ids: [customerId],
        opted_out: optedOut,
      })
      // Optimistic update of the local item — keeps the UI snappy
      // without a full preview round-trip. The next manual
      // "إعادة الفحص" will canonicalise from the server anyway.
      setNameCleanupItems(prev =>
        prev.map(it =>
          it.customer_id === customerId
            ? { ...it, marketing_opt_out_manual: optedOut }
            : it,
        ),
      )
      // If the merchant opted them OUT, also de-select the row so
      // a subsequent "Apply selected" doesn't accidentally rename
      // them — opt-out is a strong signal that the row is no
      // longer interesting for the cleanup workflow.
      if (optedOut) {
        setNameCleanupSelected(prev => {
          if (!prev.has(customerId)) return prev
          const next = new Set(prev)
          next.delete(customerId)
          return next
        })
      }
    } catch (err: any) {
      const msg = err?.detail || err?.message || 'تعذر تنفيذ الإجراء'
      setNameCleanupError(
        typeof msg === 'string' ? msg : JSON.stringify(msg),
      )
    }
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
    if (!confirm('سيتم تجاهل جميع التعديلات المحفوظة في المسودة. هل أنت متأكد؟')) {
      return
    }
    try {
      await customersApi.nameCleanupDraftDiscard()
      await openNameCleanup()
    } catch (err: any) {
      const msg = err?.detail || err?.message || 'تعذر تجاهل المسودة'
      setNameCleanupError(typeof msg === 'string' ? msg : JSON.stringify(msg))
    }
  }

  // Filtered view derived from current state + filter chip.
  const visibleCleanupItems = useMemo(() => {
    if (nameCleanupFilter === 'opted_out') {
      // Show ONLY the customers the merchant has flagged as
      // "exclude from marketing" inline. Skipped rows are still
      // hidden unless explicitly included — opt-out and skip are
      // distinct dimensions.
      return nameCleanupItems.filter(
        it => it.marketing_opt_out_manual && !cleanupRowIsSkipped(it.customer_id),
      )
    }
    if (nameCleanupFilter === 'all') {
      return nameCleanupItems.filter(it => !cleanupRowIsSkipped(it.customer_id))
    }
    if (nameCleanupFilter === 'pending') {
      return nameCleanupItems.filter(
        it => !cleanupRowIsEdited(it.customer_id),
      )
    }
    if (nameCleanupFilter === 'edited') {
      return nameCleanupItems.filter(
        it =>
          cleanupRowIsEdited(it.customer_id) &&
          !cleanupRowIsSkipped(it.customer_id),
      )
    }
    if (nameCleanupFilter === 'high') {
      return nameCleanupItems.filter(
        it => it.confidence === 'high' && !cleanupRowIsSkipped(it.customer_id),
      )
    }
    // 'low'
    return nameCleanupItems.filter(
      it => it.confidence === 'low' && !cleanupRowIsSkipped(it.customer_id),
    )
  }, [nameCleanupItems, nameCleanupRowState, nameCleanupFilter])

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
            reason:      wasEdited ? `تعديل يدوي — ${it.reason}` : it.reason,
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
      const msg = err?.detail || err?.message || 'تعذر تطبيق التنظيف'
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
      const msg = err?.detail || err?.message || 'تعذر تطبيق التنظيف'
      setNameCleanupError(typeof msg === 'string' ? msg : JSON.stringify(msg))
    } finally {
      setNameCleanupApplying(false)
    }
  }

  const allCurrentSelected = customers.length > 0 && selectedIds.size === customers.length
  const someSelected = selectedIds.size > 0 && !allCurrentSelected

  return (
    <div className="space-y-5">
      <PageHeader
        title="العملاء"
        subtitle="إدارة وتصنيف العملاء"
        action={
          <div className="flex items-center gap-2">
            <button
              onClick={openNameCleanup}
              className="btn-secondary text-sm flex items-center gap-2"
              title="إزالة الكلمات التجارية ('عميل'، 'customer'...) وأرقام الجوال من حقل الاسم — يعمل على المتجر الحالي فقط"
            >
              <Sparkles className="w-4 h-4" />
              تنظيف أسماء العملاء
            </button>
            <button
              onClick={() => navigate('/customers/import')}
              className="btn-secondary text-sm flex items-center gap-2"
            >
              <Upload className="w-4 h-4" />
              استيراد العملاء
            </button>
            <button
              onClick={() => setShowAdd(true)}
              className="btn-primary text-sm flex items-center gap-2"
            >
              <UserPlus className="w-4 h-4" />
              إضافة عميل
            </button>
          </div>
        }
      />

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="إجمالي العملاء"
          value={String(metrics?.totalCustomers ?? total)}
          change={0}
          icon={Users}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
        <StatCard
          label="عملاء VIP"
          value={String(metrics?.vipCustomers ?? 0)}
          change={0}
          icon={Crown}
          iconColor="text-amber-600"
          iconBg="bg-amber-50"
        />
        <StatCard
          label="في خطر المغادرة"
          value={String((metrics?.atRiskCustomers ?? 0) + (metrics?.inactiveCustomers ?? 0))}
          change={0}
          icon={AlertTriangle}
          iconColor="text-red-600"
          iconBg="bg-red-50"
        />
        <StatCard
          label="عملاء نشطون"
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
            شرائح العملاء
          </span>
          <span className="text-[10px] text-slate-400">
            تشمل التصنيف الذكي والتصنيف اليدوي معاً
          </span>
        </div>
        <SegmentChips
          segments={segments}
          loading={segmentsLoading}
          active={segmentKey}
          onSelect={setSegmentKey}
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
            placeholder="بحث بالاسم أو رقم الهاتف..."
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
            className="ps-9 pe-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none bg-white"
            title="فلترة حسب العملاء المُصنَّفين يدوياً فقط"
          >
            <option value="">عرض الكل</option>
            <option value="none">— بدون أي تصنيف يدوي —</option>
            {segments.filter(s => s.key !== 'all').map(s => (
              <option key={s.key} value={s.key}>يدوي فقط: {s.label_ar}</option>
            ))}
          </select>
        </div>

        {/* Marketing opt-out filter */}
        <div className="relative">
          <Filter className="absolute start-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400 pointer-events-none" />
          <select
            value={marketingOptOutFilter}
            onChange={(e) => setMarketingOptOutFilter(e.target.value as 'all' | 'in' | 'out')}
            className="ps-9 pe-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none bg-white"
            title="فلترة حسب الاستبعاد التسويقي اليدوي"
          >
            <option value="all">كل العملاء</option>
            <option value="in">المؤهلون للحملات</option>
            <option value="out">المستبعدون يدوياً</option>
          </select>
        </div>

        <button
          onClick={load}
          disabled={loading}
          className="btn-secondary text-sm flex items-center gap-2"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          تحديث
        </button>

        {/* Bulk actions — only visible when items are selected */}
        {selectedIds.size > 0 && (
          <button
            onClick={() => { setDeleteModal('selected'); setDeleteConfirmText('') }}
            className="flex items-center gap-2 text-sm text-red-600 bg-red-50 border border-red-200 hover:bg-red-100 px-3 py-2 rounded-lg transition-colors"
          >
            <Trash2 className="w-4 h-4" />
            حذف المحدد ({selectedIds.size})
          </button>
        )}

        {/* Delete all — always visible as secondary danger action */}
        <button
          onClick={() => { setDeleteModal('all'); setDeleteConfirmText('') }}
          className="flex items-center gap-2 text-sm text-red-500 hover:text-red-700 hover:bg-red-50 border border-transparent hover:border-red-200 px-3 py-2 rounded-lg transition-colors"
        >
          <Trash2 className="w-4 h-4" />
          حذف الكل
        </button>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16">
            <div className="w-8 h-8 border-4 border-brand-200 border-t-brand-500 rounded-full animate-spin" />
          </div>
        ) : customers.length === 0 ? (
          <div className="text-center py-16 text-sm text-slate-400">
            لا يوجد عملاء
          </div>
        ) : (
          <div className="overflow-x-auto overflow-y-auto max-h-[60vh]">
            <table className="w-full text-xs">
              <thead className="sticky top-0 z-10">
                <tr className="border-b border-slate-100 bg-slate-50">
                  {/* Select-all checkbox */}
                  <th className="px-3 py-3 w-10">
                    <button
                      onClick={toggleSelectAll}
                      className="text-slate-400 hover:text-brand-500 transition-colors"
                      title={allCurrentSelected ? 'إلغاء تحديد الكل' : 'تحديد الكل'}
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
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الاسم</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الهاتف</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">البريد</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الحالة</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">RFM</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">القطاع الذكي</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الطلبات</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الإنفاق</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">آخر طلب</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">المصدر</th>
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
                      <td
                        className="px-3 py-3 cursor-pointer"
                        onClick={() => setSelectedCustomer(c)}
                      >
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="font-medium text-slate-900">{c.name || '—'}</span>
                          {c.is_unsubscribed && (
                            <span className="inline-flex items-center gap-0.5 bg-red-100 text-red-700 text-[10px] font-semibold px-1.5 py-0.5 rounded-full border border-red-200">
                              <BellOff className="w-2.5 h-2.5" />
                              ألغى الاشتراك
                            </span>
                          )}
                          {!c.is_unsubscribed && c.pending_unsubscribe && (
                            <span className="inline-flex items-center gap-0.5 bg-amber-100 text-amber-700 text-[10px] font-semibold px-1.5 py-0.5 rounded-full border border-amber-200">
                              <BellOff className="w-2.5 h-2.5" />
                              بانتظار تأكيد الإلغاء
                            </span>
                          )}
                        </div>
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
                        {(c.total_spent ?? c.total_spend).toLocaleString('ar-SA')} ر.س
                      </td>
                      <td className="px-3 py-3 text-slate-500 whitespace-nowrap cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {formatDate(c.last_order_date ?? c.last_order_at)}
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
                          title="حذف العميل"
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
              صفحة {page} من {pages} ({total} عميل)
            </span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="btn-secondary text-xs py-1 px-3 disabled:opacity-40"
              >
                السابق
              </button>
              <button
                disabled={page >= pages}
                onClick={() => setPage((p) => p + 1)}
                className="btn-secondary text-xs py-1 px-3 disabled:opacity-40"
              >
                التالي
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
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col">
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
              <div className="flex items-center gap-2">
                <div className="w-9 h-9 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center">
                  <Sparkles className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="text-base font-semibold text-slate-900">
                    تنظيف أسماء العملاء
                  </h3>
                  <p className="text-[11px] text-slate-500">
                    معاينة الأسماء المُقترحة قبل التطبيق — يعمل فقط على عملاء المتجر الحالي
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
                  جاري فحص أسماء العملاء...
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
                    تم تطبيق {nameCleanupResult.applied} تغيير
                    {nameCleanupResult.skipped > 0
                      ? ` — تخطّينا ${nameCleanupResult.skipped} (تم تنظيفها مسبقاً أو غير موجودة)`
                      : ''}
                  </span>
                </div>
              )}

              {!nameCleanupLoading && nameCleanupSummary && (
                <>
                  <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
                    <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                      <div className="text-slate-500">إجمالي العملاء</div>
                      <div className="text-lg font-semibold text-slate-800 mt-1">
                        {nameCleanupSummary.totalCustomers.toLocaleString('ar-EG')}
                      </div>
                      <div className="text-[10px] text-slate-400 mt-0.5">
                        فُحص {nameCleanupSummary.totalScanned.toLocaleString('ar-EG')} عميل
                      </div>
                    </div>
                    <div className="rounded-lg border border-blue-200 bg-blue-50/60 p-3">
                      <div className="text-blue-700">أسماء تحتاج تنظيف</div>
                      <div className="text-lg font-semibold text-blue-800 mt-1">
                        {nameCleanupSummary.matchCount.toLocaleString('ar-EG')}
                      </div>
                      <div className="text-[10px] text-blue-500 mt-0.5">
                        من أصل {nameCleanupSummary.totalCustomers.toLocaleString('ar-EG')}
                      </div>
                    </div>
                    <div className="rounded-lg border border-emerald-200 bg-emerald-50/60 p-3">
                      <div className="text-emerald-700">ثقة عالية</div>
                      <div className="text-lg font-semibold text-emerald-800 mt-1">
                        {nameCleanupSummary.highConfidence.toLocaleString('ar-EG')}
                      </div>
                    </div>
                    <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-3">
                      <div className="text-amber-700">تحتاج مراجعة</div>
                      <div className="text-lg font-semibold text-amber-800 mt-1">
                        {nameCleanupSummary.lowConfidence.toLocaleString('ar-EG')}
                      </div>
                    </div>
                  </div>

                  {nameCleanupSummary.truncated && (
                    <div className="rounded-lg border border-amber-300 bg-amber-50 text-amber-900 text-xs p-3 flex items-start gap-2">
                      <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
                      <div>
                        النتائج الكثيرة جداً — نعرض أول {nameCleanupSummary.maxItems.toLocaleString('ar-EG')} اسم فقط
                        (المجموع {nameCleanupSummary.matchCount.toLocaleString('ar-EG')}).
                        طبّق هذه الدفعة ثم أعد فتح الأداة لإكمال البقية، أو استخدم
                        &quot;تطبيق ذوي الثقة العالية فقط&quot; لتنفيذ كل الأسماء عالية الثقة
                        دفعة واحدة (يعمل على جميع العملاء وليس على المعروضين فقط).
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
                        جاري حفظ المسودة...
                      </span>
                    )}
                    {nameCleanupSaveState === 'saved' && nameCleanupLastSavedAt && (
                      <span className="inline-flex items-center gap-1 text-emerald-600">
                        <Check className="w-3.5 h-3.5" />
                        تم الحفظ
                      </span>
                    )}
                    {nameCleanupSaveState === 'error' && (
                      <span className="inline-flex items-center gap-1 text-rose-600">
                        <AlertTriangle className="w-3.5 h-3.5" />
                        فشل الحفظ — سنحاول مجدداً
                      </span>
                    )}
                    {nameCleanupSummary.draftEdited > 0 && (
                      <span className="text-slate-500">
                        مسودة محفوظة: {nameCleanupSummary.draftEdited.toLocaleString('ar-EG')}
                        {nameCleanupSummary.draftSkipped > 0 && (
                          <span className="text-slate-400">
                            {' '}+ {nameCleanupSummary.draftSkipped.toLocaleString('ar-EG')} متخطى
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
                      title="إعادة فحص قاعدة العملاء كاملة من جديد"
                    >
                      <RefreshCw className="w-3 h-3" />
                      إعادة الفحص
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
                      حفظ ومتابعة لاحقاً
                    </button>
                    {nameCleanupSummary.draftEdited + nameCleanupSummary.draftSkipped > 0 && (
                      <button
                        type="button"
                        onClick={discardCleanupDraft}
                        disabled={nameCleanupApplying}
                        className="text-xs px-2.5 py-1 flex items-center gap-1.5 text-slate-500 hover:text-rose-600 disabled:opacity-40"
                        title="حذف جميع التعديلات المحفوظة في المسودة"
                      >
                        <Trash2 className="w-3 h-3" />
                        تجاهل المسودة
                      </button>
                    )}
                  </div>
                </div>
              )}

              {!nameCleanupLoading && nameCleanupSummary && nameCleanupItems.length > 0 && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  <Filter className="w-3 h-3 text-slate-400" />
                  {([
                    { key: 'all',     label: 'الكل',                 count: nameCleanupItems.filter(it => !cleanupRowIsSkipped(it.customer_id)).length },
                    { key: 'pending', label: 'غير منظف',             count: nameCleanupItems.filter(it => !cleanupRowIsEdited(it.customer_id)).length },
                    { key: 'edited',  label: 'تم تعديله يدوياً',     count: nameCleanupItems.filter(it => cleanupRowIsEdited(it.customer_id) && !cleanupRowIsSkipped(it.customer_id)).length },
                    { key: 'high',    label: 'تنظيف تلقائي (ثقة عالية)', count: nameCleanupItems.filter(it => it.confidence === 'high' && !cleanupRowIsSkipped(it.customer_id)).length },
                    { key: 'low',     label: 'يحتاج مراجعة',         count: nameCleanupItems.filter(it => it.confidence === 'low' && !cleanupRowIsSkipped(it.customer_id)).length },
                    { key: 'opted_out', label: 'مستبعد من الحملات', count: nameCleanupItems.filter(it => it.marketing_opt_out_manual && !cleanupRowIsSkipped(it.customer_id)).length },
                  ] as { key: CleanupFilter; label: string; count: number }[]).map(f => (
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
                        ({f.count.toLocaleString('ar-EG')})
                      </span>
                    </button>
                  ))}
                </div>
              )}

              {!nameCleanupLoading && nameCleanupItems.length === 0 && !nameCleanupError && (
                <div className="rounded-lg border border-slate-200 bg-slate-50 text-slate-600 text-sm p-6 text-center">
                  {nameCleanupResult
                    ? 'لا توجد أسماء أخرى تحتاج تنظيفاً. يمكنك إغلاق النافذة أو الضغط على "إعادة الفحص" لإعادة المسح.'
                    : 'جميع أسماء العملاء في هذا المتجر تبدو نظيفة — لا يوجد ما يحتاج إلى تغيير.'}
                </div>
              )}

              {!nameCleanupLoading && nameCleanupItems.length > 0 && (
                <div className="rounded-lg border border-slate-200 overflow-hidden">
                  <div className="flex items-center justify-between px-3 py-2 bg-slate-50 border-b border-slate-200 text-xs">
                    <button
                      onClick={toggleCleanupSelectAll}
                      className="flex items-center gap-2 text-slate-700 hover:text-brand-600"
                    >
                      {visibleCleanupItems.length > 0
                        && visibleCleanupItems.every(it => nameCleanupSelected.has(it.customer_id)) ? (
                        <CheckSquare className="w-4 h-4 text-brand-600" />
                      ) : (
                        <Square className="w-4 h-4 text-slate-400" />
                      )}
                      <span>
                        {visibleCleanupItems.length > 0
                          && visibleCleanupItems.every(it => nameCleanupSelected.has(it.customer_id))
                          ? 'إلغاء تحديد الكل (المعروض)'
                          : 'تحديد الكل (المعروض)'}
                      </span>
                    </button>
                    <span className="text-slate-500">
                      {nameCleanupSelected.size} / {visibleCleanupItems.length} محدد
                      {visibleCleanupItems.length !== nameCleanupItems.length && (
                        <span className="text-slate-400 ms-1">
                          (من {nameCleanupItems.length})
                        </span>
                      )}
                    </span>
                    <span className="text-slate-400 text-[10px] hidden sm:inline">
                      • انقر على الصف لتحديد العميل
                    </span>
                  </div>
                  <div className="max-h-[50vh] overflow-y-auto divide-y divide-slate-100">
                    {visibleCleanupItems.length === 0 ? (
                      <div className="py-10 text-center text-xs text-slate-500">
                        لا توجد أسماء ضمن هذا الفلتر.
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
                            aria-label={checked ? 'إلغاء التحديد' : 'تحديد العميل'}
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
                                {it.marketing_opt_out_manual && (
                                  // Distinct badge — three states never
                                  // conflate: "مستبعد من الحملات"
                                  // (merchant-driven), "ألغى الاشتراك"
                                  // (customer-driven), "تم حجبه تلقائياً"
                                  // (Quality Engine). Three buckets, three
                                  // badges, three audit trails.
                                  <Badge
                                    label="مستبعد من الحملات"
                                    variant="purple"
                                  />
                                )}
                                {isEdited && !isSkipped && (
                                  <Badge label="معدّل" variant="blue" />
                                )}
                                {isSkipped && (
                                  <Badge label="متخطى" variant="slate" />
                                )}
                                <Badge
                                  label={isHigh ? 'ثقة عالية' : 'مراجعة'}
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
                                  لا توجد كلمات
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
                                      title={isDropped ? 'إعادة هذه الكلمة' : 'حذف هذه الكلمة'}
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
                                <span className="text-slate-500 me-1">الناتج:</span>
                                {resolved ? (
                                  <span className="font-medium text-slate-800">
                                    {resolved}
                                  </span>
                                ) : (
                                  <span className="inline-flex items-center gap-1 text-rose-600 font-medium">
                                    <Trash2 className="w-3 h-3" />
                                    سيُمسح الاسم
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
                                    إعادة المقترح
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
                                  title="تخطّى هذا الصف — لن يظهر في جلسات المراجعة القادمة"
                                >
                                  <SkipForward className="w-3 h-3" />
                                  {isSkipped ? 'متخطى' : 'تخطّى'}
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
                                  مسح الاسم بالكامل
                                </button>
                                {/*
                                 * Inline marketing opt-out. The button
                                 * toggles ``marketing_opt_out_manual`` on
                                 * the customer record — distinct from
                                 * the cleanup pipeline (we never delete
                                 * the customer, the conversation, or
                                 * any inbound history). The dispatcher
                                 * honours the flag in its pre-send
                                 * filter; inbound messages still arrive
                                 * normally.
                                 */}
                                <button
                                  type="button"
                                  onClick={stopAnd(() =>
                                    toggleCleanupRowOptedOut(
                                      it.customer_id,
                                      !it.marketing_opt_out_manual,
                                    )
                                  )}
                                  className={
                                    // Slightly stronger visual weight than
                                    // the other inline actions — this is
                                    // the only one with a permanent
                                    // side-effect on the customer's
                                    // marketing eligibility, so it earns
                                    // a more saturated chip.
                                    'text-[11px] font-medium inline-flex items-center gap-1 px-2 py-0.5 rounded-md border transition ' +
                                    (it.marketing_opt_out_manual
                                      ? 'border-purple-400 bg-purple-100 text-purple-800 hover:bg-purple-200'
                                      : 'border-purple-300 bg-purple-50 text-purple-700 hover:bg-purple-100')
                                  }
                                  title={
                                    it.marketing_opt_out_manual
                                      ? 'إعادة تفعيل التسويق لهذا العميل'
                                      : 'استبعاد العميل من الحملات التسويقية (لا يتأثر استقبال رسائله)'
                                  }
                                >
                                  <UserMinus className="w-3 h-3" />
                                  {it.marketing_opt_out_manual
                                    ? 'إعادة تفعيل التسويق'
                                    : 'استبعاد من الحملات'}
                                </button>
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
                    اضغط على أي كلمة لحذفها (تظهر مشطوبة) أو لإعادتها — أو استخدم زر
                    &quot;مسح الاسم بالكامل&quot; لمسح الاسم كلياً وترك الحملات تستخدم
                    العبارة الافتراضية.
                  </div>
                  <div className="ps-5">
                    بعد التطبيق، تستخدم الحملات الاسم المحفوظ مباشرة — إذا أصبح الاسم فارغاً تُستخدم
                    العبارة &quot;عميلنا الغالي&quot;. كل التغييرات تُحفظ في سجل تدقيق
                    داخلي قابل للمراجعة.
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
                إغلاق
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
                  title="تطبيق كل المقترحات عالية الثقة دون مراجعة فردية"
                >
                  <ShieldCheck className="w-3.5 h-3.5" />
                  تطبيق ذوي الثقة العالية فقط
                </button>
                <button
                  onClick={applyCleanupSelected}
                  disabled={nameCleanupApplying || nameCleanupSelected.size === 0}
                  className="btn-primary text-xs px-3 py-1.5 flex items-center gap-1.5 disabled:opacity-40"
                >
                  {nameCleanupApplying ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <CheckSquare className="w-3.5 h-3.5" />
                  )}
                  تطبيق المحدد ({nameCleanupSelected.size})
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Add Customer Modal */}
      {showAdd && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-900">
                إضافة عميل جديد
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
                  الاسم *
                </label>
                <input
                  type="text"
                  value={addName}
                  onChange={(e) => setAddName(e.target.value)}
                  placeholder="اسم العميل"
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">
                  <Phone className="w-3.5 h-3.5 inline me-1" />
                  رقم الواتساب *
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
                  البريد الإلكتروني
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
                إلغاء
              </button>
              <button
                onClick={handleAdd}
                disabled={addLoading}
                className="btn-primary text-sm flex items-center gap-2"
              >
                {addLoading && (
                  <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                )}
                إضافة
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Bulk / Delete-All Confirmation Modal ─────────────────────────── */}
      {deleteModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md">
            {/* Danger header */}
            <div className="bg-red-600 rounded-t-2xl px-6 py-5 flex items-start gap-3">
              <div className="w-10 h-10 bg-white/20 rounded-full flex items-center justify-center shrink-0">
                <Trash2 className="w-5 h-5 text-white" />
              </div>
              <div>
                <h3 className="text-base font-bold text-white">
                  {deleteModal === 'all' ? '⚠️ حذف جميع العملاء' : `⚠️ حذف ${selectedIds.size} عميل`}
                </h3>
                <p className="text-red-100 text-xs mt-1">
                  هذا الإجراء لا يمكن التراجع عنه
                </p>
              </div>
            </div>

            <div className="px-6 py-5 space-y-4">
              {/* Warning message */}
              <div className="bg-red-50 border border-red-200 rounded-xl p-4 space-y-2">
                <p className="text-sm font-semibold text-red-800">
                  {deleteModal === 'all'
                    ? `سيتم حذف جميع العملاء (${total.toLocaleString('ar-SA')} عميل) بشكل دائم.`
                    : `سيتم حذف ${selectedIds.size} عميل محدد بشكل دائم.`
                  }
                </p>
                <ul className="text-xs text-red-700 space-y-1 list-disc list-inside">
                  <li>لن تتمكن من استعادة هذه البيانات</li>
                  <li>سيتم حذف جميع معلومات العملاء وبياناتهم</li>
                  <li>قد تتأثر الحملات والأتمتة المرتبطة بهم</li>
                </ul>
              </div>

              {/* Confirmation input */}
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-2">
                  للتأكيد، اكتب كلمة{' '}
                  <span className="font-mono font-bold text-red-600 bg-red-50 px-1.5 py-0.5 rounded">
                    احذف
                  </span>{' '}
                  في الحقل أدناه:
                </label>
                <input
                  type="text"
                  value={deleteConfirmText}
                  onChange={e => setDeleteConfirmText(e.target.value)}
                  placeholder="اكتب: احذف"
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
                  إلغاء
                </button>
                <button
                  onClick={handleBulkDelete}
                  disabled={deleteConfirmText !== 'احذف' || deleteLoading}
                  className="flex-1 flex items-center justify-center gap-2 text-sm bg-red-600 hover:bg-red-700 text-white rounded-lg py-2 font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {deleteLoading ? (
                    <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  ) : (
                    <Trash2 className="w-4 h-4" />
                  )}
                  {deleteLoading ? 'جارٍ الحذف...' : 'حذف نهائي'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Customer Detail Side Panel */}
      {selectedCustomer && (
        <div className="fixed inset-0 bg-black/40 flex justify-end z-50">
          <div
            className="absolute inset-0"
            onClick={() => setSelectedCustomer(null)}
          />
          <div className="relative bg-white w-full max-w-sm shadow-xl overflow-y-auto">
            <div className="sticky top-0 bg-white border-b border-slate-100 px-5 py-4 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-900">
                تفاصيل العميل
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
                  {selectedCustomer.name}
                </h4>
                {selectedCustomer.is_unsubscribed && (
                  <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-700 text-center space-y-1">
                    <p className="font-semibold flex items-center justify-center gap-1">
                      <BellOff className="w-3.5 h-3.5" />
                      ألغى الاشتراك
                    </p>
                    <p className="text-red-500">مستثنى من الحملات والطيار الآلي والذكاء</p>
                    <p className="text-slate-500 text-[10px]">يعود تلقائياً عند إرساله أي رسالة</p>
                  </div>
                )}
                {!selectedCustomer.is_unsubscribed && selectedCustomer.pending_unsubscribe && (
                  <div className="bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-xs text-amber-800 text-center space-y-1">
                    <p className="font-semibold flex items-center justify-center gap-1">
                      <BellOff className="w-3.5 h-3.5" />
                      بانتظار تأكيد إلغاء الاشتراك
                    </p>
                    <p className="text-amber-700">طلب الإلغاء — أُرسلت له رسالة تأكيد بزرين</p>
                    <p className="text-slate-500 text-[10px]">يتم إيقاف الأتمتة والذكاء حتى يضغط "نعم متأكد" أو "تراجع"</p>
                  </div>
                )}
                <div className="flex flex-wrap items-center justify-center gap-1.5">
                  <Badge
                    label={selectedCustomer.status_label}
                    variant={segmentVariant(selectedCustomer.status)}
                  />
                  <span className="text-[10px] text-slate-400 px-1">تصنيف ذكي</span>
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
                  <p className="text-xs text-slate-500">الطلبات</p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {(selectedCustomer.total_spent ?? selectedCustomer.total_spend).toLocaleString('ar-SA')}
                  </p>
                  <p className="text-xs text-slate-500">الإنفاق (ر.س)</p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {(selectedCustomer.avg_order_value ?? selectedCustomer.average_order_value).toLocaleString(
                      'ar-SA',
                    )}
                  </p>
                  <p className="text-xs text-slate-500">
                    متوسط الطلب (ر.س)
                  </p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {Math.round(selectedCustomer.churn_risk_score * 100)}%
                  </p>
                  <p className="text-xs text-slate-500">خطر المغادرة</p>
                </div>
              </div>

              <div className="space-y-2 text-xs">
                <div className="flex justify-between">
                  <span className="text-slate-500">قطاع RFM</span>
                  <span className="text-slate-700">
                    {selectedCustomer.rfm_segment_label || '—'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">درجة RFM</span>
                  <span className="text-slate-700 font-mono">
                    {selectedCustomer.rfm_scores?.code || selectedCustomer.rfm_code || '000'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">أول طلب</span>
                  <span className="text-slate-700">
                    {formatDate(selectedCustomer.first_order_date ?? selectedCustomer.first_order_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">آخر طلب</span>
                  <span className="text-slate-700">
                    {formatDate(selectedCustomer.last_order_date ?? selectedCustomer.last_order_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">أول ظهور</span>
                  <span className="text-slate-700">
                    {formatDate(selectedCustomer.first_seen_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">آخر إعادة حساب</span>
                  <span className="text-slate-700">
                    {formatDate(selectedCustomer.metrics_computed_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">عميل متكرر</span>
                  <span className="text-slate-700">
                    {selectedCustomer.is_returning ? 'نعم' : 'لا'}
                  </span>
                </div>
              </div>

              <button
                onClick={() => handleDelete(selectedCustomer.id)}
                className="w-full text-xs text-red-600 hover:text-red-700 hover:bg-red-50 rounded-lg py-2 transition-colors"
              >
                حذف العميل
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
  customer: CustomerRecord
  segments: CustomerSegmentMeta[]
  onChange: (next: CustomerRecord) => void | Promise<void>
  onRequireListReload?: () => void
}

function ManualSegmentsSection({
  customer, segments, onChange, onRequireListReload,
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
        setError(res.message || 'تعذر تحديث التصنيف')
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
      const manual_segments_labels = manual_segments.map(
        k => segments.find(s => s.key === k)?.label_ar || k,
      )
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
      setError(err?.detail || err?.message || 'تعذر تحديث التصنيف')
    } finally {
      setBusy(null)
    }
  }

  const handleToggleOptOut = async () => {
    setBusy('__opt__')
    setError('')
    try {
      const res = await customersApi.updateMarketingPreferences(customer.id, {
        marketing_opt_out_manual: !optedOut,
      })
      await onChange({
        ...customer,
        marketing_opt_out_manual: res.marketing_opt_out_manual,
        marketing_opt_out_manual_at: res.marketing_opt_out_manual_at,
      })
      onRequireListReload?.()
    } catch (err: any) {
      setError(err?.detail || err?.message || 'تعذر تحديث التفضيل')
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
      setError(err?.detail || err?.message || 'تعذر تحديث قائمة الاختبار')
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
          شرائح هذا العميل
        </h5>
        <span className="text-[10px] text-slate-400">
          التصنيف الذكي مع إمكانية التعديل
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
            ? 'مستبعد يدويًا'
            : src.manual_include
              ? 'مضاف يدويًا'
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
                      ? 'العميل ضمن هذه الشريحة حالياً.'
                      : 'العميل خارج هذه الشريحة حالياً.'
                  }
                >
                  {s.label_ar}
                </span>
                {isOverridden && (
                  <span
                    className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
                      src.manual_exclude
                        ? 'bg-rose-50 text-rose-700 border border-rose-100'
                        : 'bg-amber-50 text-amber-700 border border-amber-100'
                    }`}
                    title="تم تعديل التصنيف يدوياً — اضغط ↻ للعودة للتصنيف التلقائي."
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
                  title="إضافة لهذا التصنيف"
                >
                  <Plus className="w-3.5 h-3.5" />
                </button>
                <button
                  type="button"
                  disabled={busy === s.key || src.manual_exclude}
                  onClick={() => handleOverride(s.key, 'force_exclude')}
                  className="p-1 rounded hover:bg-rose-50 text-rose-600 disabled:opacity-30 disabled:hover:bg-transparent"
                  title="استبعاد من هذا التصنيف"
                >
                  <Minus className="w-3.5 h-3.5" />
                </button>
                <button
                  type="button"
                  disabled={busy === s.key || !isOverridden}
                  onClick={() => handleOverride(s.key, 'auto')}
                  className="p-1 rounded hover:bg-slate-100 text-slate-500 disabled:opacity-30 disabled:hover:bg-transparent"
                  title="العودة للتصنيف التلقائي"
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
            className="flex-1 ps-2 pe-2 py-1.5 text-xs border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none bg-white"
            disabled={!!busy}
          >
            <option value="">إضافة سريعة لشريحة…</option>
            {addableSegments.map(s => (
              <option key={s.key} value={s.key}>{s.label_ar}</option>
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
            إضافة
          </button>
        </div>
      )}

      {error && (
        <p className="text-[11px] text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1">
          {error}
        </p>
      )}

      {/* Marketing opt-out (manual exclusion) */}
      <div className="border-t border-slate-100 pt-3 space-y-2">
        <label className="flex items-start gap-2.5 cursor-pointer select-none">
          <button
            type="button"
            role="switch"
            aria-checked={optedOut}
            onClick={handleToggleOptOut}
            disabled={busy === '__opt__'}
            className={`mt-0.5 relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
              optedOut ? 'bg-red-500' : 'bg-slate-200'
            } ${busy === '__opt__' ? 'opacity-50' : ''}`}
          >
            <span
              className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                optedOut ? 'translate-x-4' : 'translate-x-0.5'
              }`}
            />
          </button>
          <div className="flex-1 -mt-0.5">
            <p className="text-xs font-semibold text-slate-800 flex items-center gap-1">
              <ShieldOff className="w-3.5 h-3.5 text-slate-400" />
              استبعاد من الحملات التسويقية
            </p>
            <p className="text-[11px] text-slate-500 leading-snug">
              لن يدخل في أي حملة تسويقية يدوية. لا يؤثر على رسائل
              الطلبات أو الأتمتات الخدمية أو نافذة 24 ساعة.
            </p>
          </div>
        </label>

        {/* Quick "add to campaign test list" — internal flag, no
            merchant-visible tag is created. */}
        <label className="flex items-start gap-2.5 cursor-pointer select-none">
          <button
            type="button"
            role="switch"
            aria-checked={isTestRecipient}
            onClick={handleToggleTest}
            disabled={busy === '__test__'}
            className={`mt-0.5 relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
              isTestRecipient ? 'bg-emerald-500' : 'bg-slate-200'
            } ${busy === '__test__' ? 'opacity-50' : ''}`}
          >
            <span
              className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                isTestRecipient ? 'translate-x-4' : 'translate-x-0.5'
              }`}
            />
          </button>
          <div className="flex-1 -mt-0.5">
            <p className="text-xs font-semibold text-slate-800 flex items-center gap-1">
              <Beaker className="w-3.5 h-3.5 text-slate-400" />
              إضافة إلى قائمة اختبار الحملات
            </p>
            <p className="text-[11px] text-slate-500 leading-snug">
              مجموعة داخلية صغيرة لاختبار الحملة قبل الإطلاق الكامل.
              لا يُنشئ تصنيفاً ظاهراً للتاجر.
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
  segments: CustomerSegmentMeta[]
  loading: boolean
  active: string
  onSelect: (key: string) => void
}

function SegmentChips({ segments, loading, active, onSelect }: SegmentChipsProps) {
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

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 overflow-x-auto pb-1 -mb-1">
        {segments.map(seg => {
          const isActive = active === seg.key
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
                <span>{seg.label_ar}</span>
                <span
                  className={
                    'inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 rounded-full text-[10px] font-semibold ' +
                    (seg.key === 'unsubscribed'
                      ? (isActive ? 'bg-white/20 text-white' : 'bg-red-100 text-red-600')
                      : (isActive ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-500'))
                  }
                >
                  {seg.customer_count.toLocaleString('ar-SA')}
                </span>
                <button
                  type="button"
                  aria-label={`تعريف شريحة ${seg.label_ar}`}
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
        })}
      </div>

      {/* Lightweight inline info card — opens under the chips, NOT a modal,
          so the merchant can keep scanning the table while reading. */}
      {popoverSeg && (
        <div className="relative bg-white border border-slate-200 rounded-lg shadow-sm p-4">
          <button
            type="button"
            onClick={() => setOpenInfo(null)}
            className="absolute top-3 end-3 text-slate-400 hover:text-slate-600"
            aria-label="إغلاق التعريف"
          >
            <X className="w-4 h-4" />
          </button>
          <p className="text-sm font-semibold text-slate-800 mb-1">
            {popoverSeg.label_ar}
          </p>
          <p className="text-xs text-slate-500 leading-relaxed mb-3">
            {popoverSeg.criteria_ar || popoverSeg.description_ar}
          </p>
          <div className="flex flex-wrap gap-3 text-[11px] text-slate-400">
            <span>عدد الحالي: <span className="text-slate-600 font-medium">{popoverSeg.customer_count.toLocaleString('ar-SA')}</span></span>
            {popoverSeg.crm_statuses.length > 0 && (
              <span>حالات CRM: <span className="font-mono text-slate-600">{popoverSeg.crm_statuses.join(' · ')}</span></span>
            )}
            {popoverSeg.rfm_buckets.length > 0 && (
              <span>RFM: <span className="font-mono text-slate-600">{popoverSeg.rfm_buckets.join(' · ')}</span></span>
            )}
          </div>
        </div>
      )}

      {/* Active filter hint — visible even when no info popover is open. */}
      {activeSeg && active !== 'all' && active !== 'unsubscribed' && (
        <p className="text-[11px] text-slate-500 ps-1">
          عرض {activeSeg.customer_count.toLocaleString('ar-SA')} عميل ضمن «{activeSeg.label_ar}»
        </p>
      )}

      {/* Special notice when merchant is viewing the Unsubscribed segment */}
      {active === 'unsubscribed' && (
        <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-lg px-3 py-2.5 text-xs text-red-700">
          <BellOff className="w-4 h-4 mt-0.5 shrink-0 text-red-400" />
          <div className="space-y-0.5">
            <p className="font-semibold">هؤلاء العملاء ألغوا الاشتراك في التواصل</p>
            <p className="text-red-600">مستثنون تلقائياً من جميع الحملات والطيار الآلي والذكاء الاصطناعي.</p>
            <p className="text-slate-500 mt-1">
              💡 إذا كنت تريد استعادتهم، ننصحك بالتواصل معهم <strong>شخصياً</strong> لمعرفة الأسباب ومحاولة استعادتهم. عند إرسالهم أي رسالة سيعودون تلقائياً للقوائم العادية.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
