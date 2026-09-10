export function WaBubblePreview({
  body,
  footer,
  headerImageUrl,
}: {
  body: string
  footer?: string
  headerImageUrl?: string | null
}) {
  return (
    <div
      className="wa-bubble-preview bg-[#e5ddd5] rounded-xl p-4 flex items-end min-h-28"
      dir="rtl"
      data-testid="wa-bubble-preview"
    >
      <div className="bg-white rounded-2xl rounded-bl-sm shadow-sm max-w-xs w-full overflow-hidden">
        {headerImageUrl ? (
          <img
            src={headerImageUrl}
            alt=""
            className="w-full h-32 object-cover border-b border-slate-100"
            loading="lazy"
          />
        ) : null}
        <div className="p-3 space-y-1">
          {body ? (
            <p className="text-slate-800 text-xs leading-relaxed whitespace-pre-line">{body}</p>
          ) : (
            <p className="text-slate-400 text-xs italic">—</p>
          )}
          {footer && <p className="text-[10px] text-slate-400 mt-1">{footer}</p>}
          <p className="text-[10px] text-slate-300 text-end">✓✓</p>
        </div>
      </div>
    </div>
  )
}
