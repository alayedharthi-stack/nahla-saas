import { useCallback, useEffect, useMemo, useState } from 'react'
import { Loader2, X } from 'lucide-react'
import { catalogApi } from '../../api/catalog'
import ConfirmModal from '../ui/ConfirmModal'
import { useLanguage } from '../../i18n/context'
import ManualProductImageUpload, { type UploadedCatalogImage } from './ManualProductImageUpload'

interface ManualProductModalProps {
  open: boolean
  onClose: () => void
  onCreated: (title: string) => Promise<void>
}

function hasUnsavedData(state: {
  title: string
  description: string
  price: string
  productUrl: string
  sku: string
  uploaded: UploadedCatalogImage | null
}): boolean {
  return Boolean(
    state.title.trim()
    || state.description.trim()
    || state.price.trim()
    || state.productUrl.trim()
    || state.sku.trim()
    || state.uploaded,
  )
}

export default function ManualProductModal({ open, onClose, onCreated }: ManualProductModalProps) {
  const { tStatic, dir } = useLanguage()
  const manual = tStatic(tr => tr.catalogMgmt.manual)
  const msgs = tStatic(tr => tr.catalogMgmt.messages)

  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [price, setPrice] = useState('')
  const [productUrl, setProductUrl] = useState('')
  const [sku, setSku] = useState('')
  const [inStock, setInStock] = useState(true)
  const [showAdvanced, setShowAdvanced] = useState(false)

  const [uploaded, setUploaded] = useState<UploadedCatalogImage | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [imageError, setImageError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)

  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmClose, setConfirmClose] = useState(false)

  const dirty = useMemo(
    () => hasUnsavedData({ title, description, price, productUrl, sku, uploaded }),
    [title, description, price, productUrl, sku, uploaded],
  )

  useEffect(() => {
    if (!open) return
    setTitle('')
    setDescription('')
    setPrice('')
    setProductUrl('')
    setSku('')
    setInStock(true)
    setShowAdvanced(false)
    setUploaded(null)
    setPreviewUrl(null)
    setImageError(null)
    setUploading(false)
    setSaving(false)
    setError(null)
    setConfirmClose(false)
  }, [open])

  const requestClose = useCallback(() => {
    if (saving || uploading) return
    if (dirty) {
      setConfirmClose(true)
      return
    }
    onClose()
  }, [dirty, onClose, saving, uploading])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') requestClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, requestClose])

  const uploadImage = async (file: File) => {
    setUploading(true)
    try {
      const result = await catalogApi.uploadManualProductImage(file)
      return result
    } finally {
      setUploading(false)
    }
  }

  const validatePrice = (raw: string): string | null => {
    const t = raw.trim()
    if (!t) return manual.priceRequired
    const normalised = t.replace(/,/g, '').replace(/\s+/g, '')
    if (!/^\d+(\.\d{1,2})?$/.test(normalised)) {
      return manual.priceInvalid
    }
    return null
  }

  const onSave = async () => {
    if (saving || uploading) return
    setError(null)

    const productTitle = title.trim()
    if (!productTitle) {
      setError(manual.nameRequired)
      return
    }
    if (!uploaded?.image_url) {
      setImageError(manual.imageRequired)
      return
    }
    const priceErr = validatePrice(price)
    if (priceErr) {
      setError(priceErr)
      return
    }

    setSaving(true)
    try {
      const priceValue = price.trim()
      const created = await catalogApi.createManualProduct({
        title: productTitle,
        description: description.trim() || undefined,
        price: priceValue,
        image_url: uploaded.image_url,
        product_url: productUrl.trim() || undefined,
        sku: sku.trim() || undefined,
        in_stock: inStock,
      })
      await onCreated(created.title)
      onClose()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : msgs.addProductFailed
      setError(msg || msgs.addProductFailed)
    } finally {
      setSaving(false)
    }
  }

  if (!open) return null

  return (
    <>
      <div
        className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
        role="dialog"
        aria-modal="true"
        aria-labelledby="manual-product-modal-title"
        onClick={e => {
          if (e.target === e.currentTarget) requestClose()
        }}
      >
        <div
          className="bg-white rounded-2xl w-full max-w-lg max-h-[min(90vh,720px)] shadow-xl flex flex-col overflow-hidden"
          dir={dir}
        >
          <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between gap-3 shrink-0">
            <h3 id="manual-product-modal-title" className="font-bold text-slate-900">
              {manual.modalTitle}
            </h3>
            <button
              type="button"
              className="p-1 rounded-lg text-slate-500 hover:bg-slate-100"
              disabled={saving || uploading}
              onClick={requestClose}
              aria-label={manual.cancel}
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          <div className="p-5 space-y-4 overflow-y-auto flex-1">
            <ManualProductImageUpload
              disabled={saving}
              uploading={uploading}
              value={uploaded}
              previewUrl={previewUrl ?? uploaded?.image_url ?? null}
              error={imageError}
              onPreviewChange={setPreviewUrl}
              onChange={setUploaded}
              onError={setImageError}
              onUpload={uploadImage}
            />

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                {manual.productName} <span className="text-rose-500">*</span>
              </label>
              <input
                value={title}
                onChange={e => setTitle(e.target.value)}
                placeholder={manual.productNamePh}
                disabled={saving || uploading}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none disabled:bg-slate-50"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{manual.description}</label>
              <textarea
                value={description}
                onChange={e => setDescription(e.target.value)}
                rows={3}
                placeholder={manual.descriptionPh}
                disabled={saving || uploading}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none resize-none disabled:bg-slate-50"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                {manual.price} <span className="text-rose-500">*</span>
              </label>
              <div className="flex gap-2">
                <input
                  value={price}
                  onChange={e => setPrice(e.target.value)}
                  placeholder={manual.pricePh}
                  inputMode="decimal"
                  dir="ltr"
                  disabled={saving || uploading}
                  className="flex-1 rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none disabled:bg-slate-50"
                />
                <span className="inline-flex items-center rounded-lg border border-slate-200 bg-slate-50 px-3 text-sm font-semibold text-slate-600 shrink-0">
                  {manual.currencyLabel}
                </span>
              </div>
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{manual.productUrl}</label>
              <input
                value={productUrl}
                onChange={e => setProductUrl(e.target.value)}
                placeholder="https://"
                dir="ltr"
                disabled={saving || uploading}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none disabled:bg-slate-50"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">{manual.availability}</label>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={saving || uploading}
                  onClick={() => setInStock(true)}
                  className={[
                    'flex-1 rounded-lg border px-3 py-2 text-sm font-semibold transition',
                    inStock
                      ? 'border-emerald-300 bg-emerald-50 text-emerald-800'
                      : 'border-slate-200 text-slate-600 hover:bg-slate-50',
                  ].join(' ')}
                >
                  {manual.inStock}
                </button>
                <button
                  type="button"
                  disabled={saving || uploading}
                  onClick={() => setInStock(false)}
                  className={[
                    'flex-1 rounded-lg border px-3 py-2 text-sm font-semibold transition',
                    !inStock
                      ? 'border-amber-300 bg-amber-50 text-amber-800'
                      : 'border-slate-200 text-slate-600 hover:bg-slate-50',
                  ].join(' ')}
                >
                  {manual.outOfStock}
                </button>
              </div>
            </div>

            <div>
              <button
                type="button"
                className="text-xs font-semibold text-emerald-700 hover:text-emerald-800"
                onClick={() => setShowAdvanced(v => !v)}
              >
                {manual.additionalOptions}
              </button>
              {showAdvanced && (
                <div className="mt-2">
                  <label className="block text-xs font-semibold text-slate-700 mb-1">{manual.sku}</label>
                  <input
                    value={sku}
                    onChange={e => setSku(e.target.value)}
                    placeholder={manual.skuPh}
                    dir="ltr"
                    disabled={saving || uploading}
                    className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-emerald-400 focus:ring-1 focus:ring-emerald-200 outline-none disabled:bg-slate-50"
                  />
                </div>
              )}
            </div>

            {error && (
              <p className="text-sm text-rose-700 bg-rose-50 border border-rose-100 rounded-lg px-3 py-2">
                {error}
              </p>
            )}
          </div>

          <div className="px-5 py-4 border-t border-slate-100 flex justify-end gap-2 shrink-0">
            <button
              type="button"
              onClick={requestClose}
              disabled={saving || uploading}
              className="px-4 py-2 rounded-xl text-sm font-semibold text-slate-600 hover:bg-slate-100 transition disabled:opacity-50"
            >
              {manual.cancel}
            </button>
            <button
              type="button"
              onClick={onSave}
              disabled={saving || uploading || !uploaded}
              className="inline-flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-5 py-2 rounded-xl text-sm transition"
            >
              {(saving || uploading) && <Loader2 className="w-4 h-4 animate-spin" />}
              {saving ? manual.saving : manual.save}
            </button>
          </div>
        </div>
      </div>

      <ConfirmModal
        open={confirmClose}
        title={manual.unsavedCloseTitle}
        message={manual.unsavedCloseMessage}
        confirmLabel={manual.unsavedCloseConfirm}
        cancelLabel={manual.cancel}
        destructive
        onCancel={() => setConfirmClose(false)}
        onConfirm={() => {
          setConfirmClose(false)
          onClose()
        }}
      />
    </>
  )
}
