import { useNavigate } from 'react-router-dom'
import { getImpersonation, stopImpersonation } from '../../auth'
import { ShieldAlert, Settings } from 'lucide-react'
import { API_BASE } from '../../api/client'

export default function ImpersonationBanner() {
  const info = getImpersonation()
  const navigate = useNavigate()

  if (!info) return null

  const handleExit = async () => {
    // Resolve the support session: marks request as "resolved", disables access,
    // stores a thank-you notification for the merchant, and writes audit log.
    try {
      await fetch(`${API_BASE}/merchant/support-access/resolve`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('nahla_token') ?? ''}`,
        },
        body: JSON.stringify({}),
      })
    } catch { /* ignore — we still exit regardless */ }
    stopImpersonation()
    window.location.href = '/admin'
  }

  return (
    <div
      dir="rtl"
      className="w-full bg-red-600 text-white text-sm z-50 sticky top-0"
    >
      {/* Pulsing dot + main content */}
      <div className="flex items-center justify-between px-4 py-2.5 gap-3">
        <div className="flex items-center gap-2.5 min-w-0">
          {/* Pulsing indicator */}
          <span className="relative flex h-2.5 w-2.5 shrink-0">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-white opacity-60" />
            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-white" />
          </span>
          <ShieldAlert className="w-4 h-4 shrink-0" />
          <span className="font-semibold truncate">
            تنبيه: فريق الدعم يدير متجر{' '}
            <strong>{info.storeName || info.merchantEmail}</strong>
          </span>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => navigate('/settings?tab=security')}
            className="flex items-center gap-1.5 bg-white/20 hover:bg-white/30 text-white font-semibold px-3 py-1 rounded-lg text-xs transition"
          >
            <Settings className="w-3.5 h-3.5" />
            إدارة الوصول
          </button>
          <button
            onClick={handleExit}
            className="bg-white text-red-600 font-semibold px-3 py-1 rounded-lg text-xs hover:bg-red-50 transition"
          >
            إنهاء الجلسة
          </button>
        </div>
      </div>
    </div>
  )
}
