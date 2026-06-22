/**
 * CatalogIntelligence.tsx — /catalog-intelligence
 *
 * Phase 2 — merchant dashboard for catalog groups, product membership,
 * alternatives/relations, best-seller flags, and catalog intelligence settings.
 * Consumes Phase 1 backend APIs only; no AI runtime wiring.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  CheckCircle,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  Star,
  Trash2,
  X,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import Badge from '../components/ui/Badge'
import { useLanguage } from '../i18n/context'
import { catalogApi } from '../api/catalog'
import {
  catalogIntelligenceApi,
  type CatalogIntelligenceSettings,
  type CatalogValidationReport,
  type ProductGroup,
  type ProductGroupItem,
  type ProductRelation,
} from '../api/catalogIntelligence'

type TabKey = 'groups' | 'settings' | 'uncategorized'

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message
  return String(err ?? 'unknown_error')
}

export default function CatalogIntelligence() {
  const { t } = useLanguage()
  const [tab, setTab] = useState<TabKey>('groups')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [groups, setGroups] = useState<ProductGroup[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [selectedGroup, setSelectedGroup] = useState<ProductGroup | null>(null)
  const [settings, setSettings] = useState<CatalogIntelligenceSettings | null>(null)
  const [validation, setValidation] = useState<CatalogValidationReport | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const [newLabel, setNewLabel] = useState('')
  const [editLabel, setEditLabel] = useState('')
  const [editMatch, setEditMatch] = useState('')
  const [productQuery, setProductQuery] = useState('')
  const [productOptions, setProductOptions] = useState<Array<{ id: number; title: string }>>([])
  const [pickProductId, setPickProductId] = useState<number | ''>('')
  const [relations, setRelations] = useState<ProductRelation[]>([])
  const [relationTargetId, setRelationTargetId] = useState<number | ''>('')
  const [focusProductId, setFocusProductId] = useState<number | null>(null)
  const [allProducts, setAllProducts] = useState<Array<{ id: number; title: string }>>([])
  const [groupedProductIds, setGroupedProductIds] = useState<Set<number>>(new Set())
  const [bestSellerByProduct, setBestSellerByProduct] = useState<Map<number, boolean>>(new Map())

  const selectedItems = useMemo(
    () => selectedGroup?.items ?? [],
    [selectedGroup],
  )

  const uncategorized = useMemo(
    () => allProducts.filter(p => !groupedProductIds.has(p.id)),
    [allProducts, groupedProductIds],
  )

  const loadGroups = useCallback(async () => {
    const res = await catalogIntelligenceApi.listGroups(true)
    setGroups(res.groups)
    return res.groups
  }, [])

  const loadSettings = useCallback(async () => {
    const [settingsRes, validationRes] = await Promise.all([
      catalogIntelligenceApi.getSettings(),
      catalogIntelligenceApi.getValidation(),
    ])
    setSettings(settingsRes.catalog_intelligence)
    setValidation(validationRes)
  }, [])

  const loadGroupedProductIds = useCallback(async (groupList: ProductGroup[]) => {
    const ids = new Set<number>()
    for (const g of groupList) {
      const detail = await catalogIntelligenceApi.getGroup(g.id)
      for (const item of detail.group.items ?? []) {
        ids.add(item.product_id)
      }
    }
    setGroupedProductIds(ids)
  }, [])

  const loadAllProducts = useCallback(async () => {
    const res = await catalogApi.products(200, 0, { catalog_visibility: 'active' })
    const rows = (res.rows ?? []).map(p => ({ id: p.id, title: p.title }))
    setAllProducts(rows)
  }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [groupList] = await Promise.all([
        loadGroups(),
        loadSettings(),
        loadAllProducts(),
      ])
      await loadGroupedProductIds(groupList)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [loadAllProducts, loadGroupedProductIds, loadGroups, loadSettings])

  useEffect(() => { void refresh() }, [refresh])

  const loadSelectedGroup = useCallback(async (groupId: number) => {
    const res = await catalogIntelligenceApi.getGroup(groupId)
    setSelectedGroup(res.group)
    setEditLabel(res.group.label)
    setEditMatch(res.group.catalog_match)
    setSelectedId(groupId)
    setFocusProductId(null)
    setRelations([])

    const items = res.group.items ?? []
    if (items.length === 0) {
      setBestSellerByProduct(new Map())
      return
    }
    const rankings = await Promise.all(
      items.map(item =>
        catalogIntelligenceApi.getRanking(item.product_id).catch(() => null),
      ),
    )
    const next = new Map<number, boolean>()
    items.forEach((item, idx) => {
      next.set(item.product_id, rankings[idx]?.ranking?.is_best_seller ?? false)
    })
    setBestSellerByProduct(next)
  }, [])

  useEffect(() => {
    if (selectedId && !selectedGroup) {
      void loadSelectedGroup(selectedId)
    }
  }, [loadSelectedGroup, selectedId, selectedGroup])

  const searchProducts = useCallback(async (q: string) => {
    const res = await catalogApi.products(30, 0, { q, catalog_visibility: 'active' })
    setProductOptions((res.rows ?? []).map(p => ({ id: p.id, title: p.title })))
  }, [])

  useEffect(() => {
    const handle = setTimeout(() => {
      if (productQuery.trim()) void searchProducts(productQuery.trim())
      else setProductOptions([])
    }, 300)
    return () => clearTimeout(handle)
  }, [productQuery, searchProducts])

  async function handleCreateGroup() {
    if (!newLabel.trim()) return
    setSaving(true)
    setError(null)
    try {
      await catalogIntelligenceApi.createGroup({ label: newLabel.trim() })
      setNewLabel('')
      const list = await loadGroups()
      await loadGroupedProductIds(list)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  async function handleSaveGroup() {
    if (!selectedId) return
    setSaving(true)
    setError(null)
    try {
      const res = await catalogIntelligenceApi.updateGroup(selectedId, {
        label: editLabel.trim(),
        catalog_match: editMatch.trim(),
      })
      setSelectedGroup(res.group)
      await loadGroups()
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  async function handleDeleteGroup(groupId: number) {
    if (!window.confirm(t(tr => tr.pages.catalogIntelligence.confirmDeleteGroup))) return
    setSaving(true)
    try {
      await catalogIntelligenceApi.deleteGroup(groupId)
      if (selectedId === groupId) {
        setSelectedId(null)
        setSelectedGroup(null)
      }
      const list = await loadGroups()
      await loadGroupedProductIds(list)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  async function moveGroup(groupId: number, direction: 'up' | 'down') {
    const ids = groups.map(g => g.id)
    const idx = ids.indexOf(groupId)
    if (idx < 0) return
    const swap = direction === 'up' ? idx - 1 : idx + 1
    if (swap < 0 || swap >= ids.length) return
    ;[ids[idx], ids[swap]] = [ids[swap], ids[idx]]
    const res = await catalogIntelligenceApi.reorderGroups(ids)
    setGroups(res.groups)
  }

  async function handleAddProduct() {
    if (!selectedId || pickProductId === '') return
    setSaving(true)
    try {
      await catalogIntelligenceApi.addGroupItem(selectedId, { product_id: Number(pickProductId) })
      await loadSelectedGroup(selectedId)
      const list = await loadGroups()
      await loadGroupedProductIds(list)
      setPickProductId('')
      setProductQuery('')
      setProductOptions([])
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  async function handleRemoveItem(item: ProductGroupItem) {
    if (!selectedId) return
    setSaving(true)
    try {
      await catalogIntelligenceApi.deleteGroupItem(selectedId, item.id)
      await loadSelectedGroup(selectedId)
      const list = await loadGroups()
      await loadGroupedProductIds(list)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  async function handleToggleBestSeller(productId: number) {
    const current = bestSellerByProduct.get(productId) ?? false
    setSaving(true)
    try {
      await catalogIntelligenceApi.saveRanking(productId, { is_best_seller: !current })
      setBestSellerByProduct(prev => {
        const next = new Map(prev)
        next.set(productId, !current)
        return next
      })
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  async function loadRelationsForProduct(productId: number) {
    setFocusProductId(productId)
    const res = await catalogIntelligenceApi.listRelations(productId)
    setRelations(res.relations)
  }

  async function handleAddRelation() {
    if (!focusProductId || relationTargetId === '') return
    setSaving(true)
    try {
      await catalogIntelligenceApi.createRelation(focusProductId, {
        target_product_id: Number(relationTargetId),
        relation_type: 'alternative',
      })
      const res = await catalogIntelligenceApi.listRelations(focusProductId)
      setRelations(res.relations)
      setRelationTargetId('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  async function handleDeleteRelation(relationId: number) {
    if (!focusProductId) return
    await catalogIntelligenceApi.deleteRelation(focusProductId, relationId)
    const res = await catalogIntelligenceApi.listRelations(focusProductId)
    setRelations(res.relations)
  }

  async function handleSaveSettings() {
    if (!settings) return
    setSaving(true)
    try {
      const res = await catalogIntelligenceApi.saveSettings(settings)
      setSettings(res.catalog_intelligence)
      const validationRes = await catalogIntelligenceApi.getValidation()
      setValidation(validationRes)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 className="w-8 h-8 animate-spin text-brand-500" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title={t(tr => tr.pages.catalogIntelligence.title)}
        subtitle={t(tr => tr.pages.catalogIntelligence.subtitle)}
        action={(
          <button type="button" className="btn-secondary text-sm" onClick={() => void refresh()}>
            <RefreshCw className="w-4 h-4" />
            {t(tr => tr.pages.catalogIntelligence.refresh)}
          </button>
        )}
      />

      {error && (
        <div className="flex items-center gap-2 text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-4 py-3">
          <AlertCircle className="w-4 h-4 shrink-0" />
          {error}
        </div>
      )}

      <div className="flex gap-2 flex-wrap">
        {(['groups', 'settings', 'uncategorized'] as TabKey[]).map(key => (
          <button
            key={key}
            type="button"
            className={tab === key ? 'btn-primary text-sm' : 'btn-secondary text-sm'}
            onClick={() => setTab(key)}
          >
            {t(tr => tr.pages.catalogIntelligence.tabs[key])}
          </button>
        ))}
      </div>

      {tab === 'groups' && (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
          <div className="card p-4 space-y-4 xl:col-span-1">
            <h2 className="text-sm font-semibold text-slate-900">
              {t(tr => tr.pages.catalogIntelligence.groupsTitle)}
            </h2>
            <div className="flex gap-2">
              <input
                className="input flex-1"
                value={newLabel}
                onChange={e => setNewLabel(e.target.value)}
                placeholder={t(tr => tr.pages.catalogIntelligence.newGroupPlaceholder)}
              />
              <button type="button" className="btn-primary" disabled={saving} onClick={() => void handleCreateGroup()}>
                <Plus className="w-4 h-4" />
              </button>
            </div>
            <div className="space-y-2">
              {groups.map(group => (
                <div
                  key={group.id}
                  className={`flex items-center gap-2 p-3 rounded-lg border cursor-pointer ${
                    selectedId === group.id ? 'border-brand-300 bg-brand-50' : 'border-slate-100 hover:bg-slate-50'
                  }`}
                >
                  <button type="button" className="flex-1 text-start" onClick={() => void loadSelectedGroup(group.id)}>
                    <p className="text-sm font-medium text-slate-900">{group.label}</p>
                    <p className="text-xs text-slate-500">{group.slug} · {group.product_count}</p>
                  </button>
                  <div className="flex items-center gap-1">
                    <button type="button" className="p-1 text-slate-400 hover:text-slate-700" onClick={() => void moveGroup(group.id, 'up')}>
                      <ArrowUp className="w-4 h-4" />
                    </button>
                    <button type="button" className="p-1 text-slate-400 hover:text-slate-700" onClick={() => void moveGroup(group.id, 'down')}>
                      <ArrowDown className="w-4 h-4" />
                    </button>
                    {!group.is_active && <Badge variant="slate" label={t(tr => tr.pages.catalogIntelligence.inactive)} />}
                  </div>
                </div>
              ))}
              {groups.length === 0 && (
                <p className="text-sm text-slate-500">{t(tr => tr.pages.catalogIntelligence.noGroups)}</p>
              )}
            </div>
          </div>

          <div className="card p-5 space-y-5 xl:col-span-2">
            {!selectedGroup ? (
              <p className="text-sm text-slate-500">{t(tr => tr.pages.catalogIntelligence.selectGroupHint)}</p>
            ) : (
              <>
                <div className="flex items-start justify-between gap-3">
                  <div className="space-y-3 flex-1">
                    <input className="input" value={editLabel} onChange={e => setEditLabel(e.target.value)} />
                    <input
                      className="input"
                      value={editMatch}
                      onChange={e => setEditMatch(e.target.value)}
                      placeholder={t(tr => tr.pages.catalogIntelligence.catalogMatchPlaceholder)}
                    />
                  </div>
                  <button
                    type="button"
                    className="p-2 rounded-lg border border-red-200 text-red-600 hover:bg-red-50"
                    onClick={() => void handleDeleteGroup(selectedGroup.id)}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
                <div className="flex items-center gap-3">
                  <button type="button" className="btn-primary text-sm" disabled={saving} onClick={() => void handleSaveGroup()}>
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    {t(tr => tr.pages.catalogIntelligence.saveGroup)}
                  </button>
                  {saved && (
                    <span className="text-sm text-emerald-600 flex items-center gap-1">
                      <CheckCircle className="w-4 h-4" /> {t(tr => tr.pages.catalogIntelligence.saved)}
                    </span>
                  )}
                </div>

                <div className="border-t border-slate-100 pt-4 space-y-3">
                  <h3 className="text-sm font-semibold text-slate-900">
                    {t(tr => tr.pages.catalogIntelligence.productsInGroup)}
                  </h3>
                  {selectedItems.map(item => {
                    const isBestSeller = bestSellerByProduct.get(item.product_id) ?? false
                    return (
                    <div key={item.id} className="flex items-center gap-3 p-3 rounded-lg bg-slate-50">
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-slate-900 truncate">{item.product_title || `#${item.product_id}`}</p>
                        <p className="text-xs text-slate-500">ID {item.product_id}</p>
                      </div>
                      <button
                        type="button"
                        className="btn-secondary text-xs"
                        onClick={() => void loadRelationsForProduct(item.product_id)}
                      >
                        {t(tr => tr.pages.catalogIntelligence.alternatives)}
                      </button>
                      <button
                        type="button"
                        className={`p-2 ${isBestSeller ? 'text-amber-500' : 'text-slate-300 hover:text-amber-500'}`}
                        title={t(tr => tr.pages.catalogIntelligence.bestSeller)}
                        onClick={() => void handleToggleBestSeller(item.product_id)}
                      >
                        <Star className={`w-4 h-4 ${isBestSeller ? 'fill-current' : ''}`} />
                      </button>
                      <button type="button" className="p-2 text-red-500 hover:text-red-600" onClick={() => void handleRemoveItem(item)}>
                        <X className="w-4 h-4" />
                      </button>
                    </div>
                    )
                  })}

                  <div className="flex flex-col sm:flex-row gap-2">
                    <input
                      className="input flex-1"
                      value={productQuery}
                      onChange={e => setProductQuery(e.target.value)}
                      placeholder={t(tr => tr.pages.catalogIntelligence.searchProductPlaceholder)}
                    />
                    <select
                      className="input sm:w-56"
                      value={pickProductId}
                      onChange={e => setPickProductId(e.target.value ? Number(e.target.value) : '')}
                    >
                      <option value="">{t(tr => tr.pages.catalogIntelligence.pickProduct)}</option>
                      {productOptions.map(p => (
                        <option key={p.id} value={p.id}>{p.title}</option>
                      ))}
                    </select>
                    <button type="button" className="btn-primary" disabled={saving || pickProductId === ''} onClick={() => void handleAddProduct()}>
                      <Plus className="w-4 h-4" />
                    </button>
                  </div>
                </div>

                {focusProductId && (
                  <div className="border-t border-slate-100 pt-4 space-y-3">
                    <h3 className="text-sm font-semibold text-slate-900">
                      {t(tr => tr.pages.catalogIntelligence.alternativesFor)} #{focusProductId}
                    </h3>
                    {relations.map(rel => (
                      <div key={rel.id} className="flex items-center justify-between p-2 rounded bg-slate-50 text-sm">
                        <span>{rel.target_product_title || `#${rel.target_product_id}`}</span>
                        <button type="button" className="text-red-500" onClick={() => void handleDeleteRelation(rel.id)}>
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    ))}
                    <div className="flex gap-2">
                      <select
                        className="input flex-1"
                        value={relationTargetId}
                        onChange={e => setRelationTargetId(e.target.value ? Number(e.target.value) : '')}
                      >
                        <option value="">{t(tr => tr.pages.catalogIntelligence.pickAlternative)}</option>
                        {allProducts
                          .filter(p => p.id !== focusProductId)
                          .map(p => (
                            <option key={p.id} value={p.id}>{p.title}</option>
                          ))}
                      </select>
                      <button type="button" className="btn-secondary" disabled={relationTargetId === ''} onClick={() => void handleAddRelation()}>
                        {t(tr => tr.pages.catalogIntelligence.addAlternative)}
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {tab === 'settings' && settings && (
        <div className="card p-5 space-y-4 max-w-2xl">
          <div className="flex items-center gap-2 text-slate-900 font-semibold text-sm">
            <Settings2 className="w-4 h-4" />
            {t(tr => tr.pages.catalogIntelligence.settingsTitle)}
          </div>
          <label className="block text-sm">
            <span className="text-slate-600">{t(tr => tr.pages.catalogIntelligence.bestSellerMode)}</span>
            <select
              className="input mt-1"
              value={settings.best_seller_mode}
              onChange={e => setSettings({ ...settings, best_seller_mode: e.target.value })}
            >
              <option value="manual">manual</option>
              <option value="auto">auto</option>
              <option value="hybrid">hybrid</option>
            </select>
          </label>
          <label className="block text-sm">
            <span className="text-slate-600">{t(tr => tr.pages.catalogIntelligence.defaultGroupSlug)}</span>
            <input
              className="input mt-1"
              value={settings.default_group_slug}
              onChange={e => setSettings({ ...settings, default_group_slug: e.target.value })}
            />
          </label>
          <label className="block text-sm">
            <span className="text-slate-600">{t(tr => tr.pages.catalogIntelligence.maxRelations)}</span>
            <input
              type="number"
              min={1}
              max={50}
              className="input mt-1"
              value={settings.max_relations_per_product}
              onChange={e => setSettings({ ...settings, max_relations_per_product: Number(e.target.value) })}
            />
          </label>
          <button type="button" className="btn-primary text-sm" disabled={saving} onClick={() => void handleSaveSettings()}>
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            {t(tr => tr.pages.catalogIntelligence.saveSettings)}
          </button>

          {validation && (
            <div className="border-t border-slate-100 pt-4 space-y-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
                {validation.ok ? (
                  <CheckCircle className="w-4 h-4 text-emerald-600" />
                ) : (
                  <AlertCircle className="w-4 h-4 text-amber-600" />
                )}
                {t(tr => tr.pages.catalogIntelligence.validationTitle)}
              </div>
              <p className="text-xs text-slate-500">
                {t(tr => tr.pages.catalogIntelligence.validationSummary)
                  .replace('{groups}', String(validation.summary.active_groups))
                  .replace('{grouped}', String(validation.summary.grouped_products))
                  .replace('{uncategorized}', String(validation.summary.uncategorized_products))
                  .replace('{bestSellers}', String(validation.summary.best_sellers))}
              </p>
              {validation.issues.length > 0 ? (
                <ul className="space-y-2 max-h-56 overflow-y-auto">
                  {validation.issues.map(issue => (
                    <li
                      key={`${issue.code}-${issue.message}`}
                      className="text-xs p-2 rounded bg-slate-50 text-slate-700 flex gap-2"
                    >
                      <Badge
                        label={issue.severity}
                        variant={
                          issue.severity === 'warning'
                            ? 'amber'
                            : issue.severity === 'error'
                              ? 'red'
                              : 'slate'
                        }
                      />
                      <span>{issue.message}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-emerald-700">{t(tr => tr.pages.catalogIntelligence.validationOk)}</p>
              )}
            </div>
          )}
        </div>
      )}

      {tab === 'uncategorized' && (
        <div className="card p-5 space-y-4">
          <p className="text-sm text-slate-600">{t(tr => tr.pages.catalogIntelligence.uncategorizedHint)}</p>
          <p className="text-xs text-slate-500">
            {t(tr => tr.pages.catalogIntelligence.uncategorizedCount).replace('{count}', String(uncategorized.length))}
          </p>
          <div className="space-y-2 max-h-[480px] overflow-y-auto">
            {uncategorized.map(p => (
              <div key={p.id} className="flex items-center justify-between p-3 rounded-lg bg-slate-50 text-sm">
                <span>{p.title}</span>
                <span className="text-slate-400">#{p.id}</span>
              </div>
            ))}
            {uncategorized.length === 0 && (
              <p className="text-sm text-emerald-700">{t(tr => tr.pages.catalogIntelligence.allCategorized)}</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
