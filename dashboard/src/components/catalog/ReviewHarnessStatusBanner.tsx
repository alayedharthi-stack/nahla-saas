import type { ReviewHarnessStatus } from '../../api/catalog'

/** English-only App Review filming banner. Hidden when the harness is off. */
export default function ReviewHarnessStatusBanner({
  status,
}: {
  status?: ReviewHarnessStatus | null
}) {
  if (!status?.active) return null
  const synced = status.ui_status === 'connected_and_synced'
  return (
    <div
      dir="ltr"
      lang="en"
      className={
        synced
          ? 'rounded-2xl border border-emerald-200 bg-emerald-50 p-5 text-left'
          : 'rounded-2xl border border-amber-200 bg-amber-50 p-5 text-left'
      }
    >
      <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        WhatsApp catalog
      </p>
      <p className={`mt-1 text-lg font-bold ${synced ? 'text-emerald-800' : 'text-amber-900'}`}>
        {status.ui_label}
      </p>
      {status.error_code === 'REAUTH_REQUIRED' && (
        <p className="mt-2 text-sm text-amber-800">
          Reconnect WhatsApp and grant the requested Meta permissions.
        </p>
      )}
    </div>
  )
}
