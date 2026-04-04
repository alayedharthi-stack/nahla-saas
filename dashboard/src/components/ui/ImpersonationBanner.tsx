import { getImpersonation, stopImpersonation } from '../../auth'

export default function ImpersonationBanner() {
  const info = getImpersonation()

  if (!info) return null

  const handleExit = () => {
    stopImpersonation()
    // Hard redirect to admin — ensures token is freshly read on mount
    window.location.href = '/admin'
  }

  return (
    <div
      dir="rtl"
      className="w-full bg-amber-500 text-white text-sm flex items-center justify-between px-4 py-2 z-50"
    >
      <div className="flex items-center gap-2">
        <span className="text-lg">👑</span>
        <span>
          أنت تدير متجر:{' '}
          <strong className="font-semibold">{info.storeName || info.merchantEmail}</strong>
          <span className="opacity-75 mr-2">({info.merchantEmail})</span>
        </span>
      </div>
      <button
        onClick={handleExit}
        className="bg-white text-amber-600 font-semibold px-3 py-1 rounded-lg text-xs hover:bg-amber-50 transition"
      >
        العودة للوحة المالك
      </button>
    </div>
  )
}
