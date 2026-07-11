import { useCallback, useRef, useState } from 'react'
import { ImagePlus, Loader2, Trash2 } from 'lucide-react'
import { useLanguage } from '../../i18n/context'

const MAX_BYTES = 5 * 1024 * 1024
const ACCEPT = 'image/jpeg,image/png,image/webp'

export interface UploadedCatalogImage {
  image_url: string
  media_id: string
  content_type: string
  size_bytes: number
}

interface ManualProductImageUploadProps {
  disabled?: boolean
  uploading?: boolean
  value: UploadedCatalogImage | null
  previewUrl: string | null
  error: string | null
  onPreviewChange: (url: string | null) => void
  onChange: (value: UploadedCatalogImage | null) => void
  onError: (message: string | null) => void
  onUpload: (file: File) => Promise<UploadedCatalogImage>
}

export default function ManualProductImageUpload(props: ManualProductImageUploadProps) {
  const { tStatic, dir } = useLanguage()
  const copy = tStatic(tr => tr.catalogMgmt.manual)
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)

  const validateFile = useCallback((file: File): string | null => {
    if (!ACCEPT.split(',').includes(file.type)) {
      return copy.imageTypeInvalid
    }
    if (file.size > MAX_BYTES) {
      return copy.imageTooLarge
    }
    return null
  }, [copy.imageTooLarge, copy.imageTypeInvalid])

  const clearPreview = useCallback(() => {
    if (props.previewUrl?.startsWith('blob:')) {
      URL.revokeObjectURL(props.previewUrl)
    }
    props.onPreviewChange(null)
    props.onChange(null)
    props.onError(null)
    if (inputRef.current) inputRef.current.value = ''
  }, [props])

  const handleFile = useCallback(async (file: File | undefined) => {
    if (!file || props.disabled || props.uploading) return
    const validationError = validateFile(file)
    if (validationError) {
      props.onError(validationError)
      return
    }
    props.onError(null)
    const localPreview = URL.createObjectURL(file)
    if (props.previewUrl?.startsWith('blob:')) {
      URL.revokeObjectURL(props.previewUrl)
    }
    props.onPreviewChange(localPreview)
    try {
      const uploaded = await props.onUpload(file)
      props.onChange(uploaded)
    } catch (e: unknown) {
      clearPreview()
      const msg = e instanceof Error ? e.message : copy.imageUploadFailed
      props.onError(msg || copy.imageUploadFailed)
    }
  }, [clearPreview, copy.imageUploadFailed, props, validateFile])

  const onInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    void handleFile(e.target.files?.[0])
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    void handleFile(e.dataTransfer.files?.[0])
  }

  const showPreview = Boolean(props.previewUrl)

  return (
    <div className="space-y-2" dir={dir}>
      <label className="block text-xs font-semibold text-slate-700">
        {copy.imageLabel} <span className="text-rose-500">*</span>
      </label>

      {showPreview ? (
        <div className="relative rounded-xl border border-slate-200 bg-slate-50 overflow-hidden">
          <img
            src={props.previewUrl ?? undefined}
            alt=""
            className="w-full max-h-56 object-contain bg-white"
          />
          <div className="absolute top-2 end-2 flex gap-2">
            {props.uploading && (
              <span className="inline-flex items-center gap-1 rounded-lg bg-white/90 px-2 py-1 text-xs text-slate-600 shadow">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                {copy.imageUploading}
              </span>
            )}
            <button
              type="button"
              disabled={props.disabled || props.uploading}
              onClick={clearPreview}
              className="inline-flex items-center gap-1 rounded-lg bg-white/95 px-2 py-1 text-xs font-semibold text-rose-700 shadow hover:bg-white disabled:opacity-50"
            >
              <Trash2 className="w-3.5 h-3.5" />
              {copy.imageRemove}
            </button>
          </div>
        </div>
      ) : (
        <div
          role="button"
          tabIndex={0}
          onKeyDown={e => {
            if (e.key === 'Enter' || e.key === ' ') inputRef.current?.click()
          }}
          onClick={() => !props.disabled && !props.uploading && inputRef.current?.click()}
          onDragOver={e => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          className={[
            'rounded-xl border-2 border-dashed px-4 py-8 text-center transition cursor-pointer',
            dragOver ? 'border-emerald-400 bg-emerald-50/50' : 'border-slate-200 bg-slate-50 hover:border-emerald-300',
            props.disabled || props.uploading ? 'opacity-60 pointer-events-none' : '',
          ].join(' ')}
        >
          <div className="flex flex-col items-center gap-2 text-slate-600">
            {props.uploading
              ? <Loader2 className="w-8 h-8 animate-spin text-emerald-600" />
              : <ImagePlus className="w-8 h-8 text-emerald-600" />}
            <p className="text-sm font-medium text-slate-700">{copy.imageDropHint}</p>
            <p className="text-xs text-slate-500">{copy.imageChoose}</p>
          </div>
        </div>
      )}

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        className="hidden"
        disabled={props.disabled || props.uploading}
        onChange={onInputChange}
      />

      {props.error && (
        <p className="text-xs text-rose-600">{props.error}</p>
      )}
    </div>
  )
}
