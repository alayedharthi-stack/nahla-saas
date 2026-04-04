interface AppStoreBadgesProps {
  lang?: 'ar' | 'en'
  size?: 'sm' | 'md' | 'lg'
  showPwa?: boolean
  className?: string
}

const APP_STORE_URL    = 'https://apps.apple.com/app/nahlah-ai/id0000000'   // placeholder
const PLAY_STORE_URL   = 'https://play.google.com/store/apps/details?id=ai.nahlah.app' // placeholder

export default function AppStoreBadges({
  lang = 'ar',
  size = 'md',
  showPwa = true,
  className = '',
}: AppStoreBadgesProps) {
  const ar = lang === 'ar'

  const heights: Record<string, string> = {
    sm: 'h-8',
    md: 'h-10',
    lg: 'h-12',
  }
  const h = heights[size]

  const handlePwaInstall = () => {
    if ('standalone' in navigator && (navigator as Navigator & { standalone?: boolean }).standalone) {
      return
    }
    alert(ar
      ? 'افتح متصفح Safari أو Chrome وانقر "إضافة إلى الشاشة الرئيسية" لتثبيت التطبيق'
      : 'Open Safari or Chrome and tap "Add to Home Screen" to install the app'
    )
  }

  return (
    <div className={`flex flex-wrap items-center gap-3 ${className}`} dir="ltr">
      {/* App Store */}
      <a
        href={APP_STORE_URL}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={ar ? 'حمّل على App Store' : 'Download on App Store'}
        className="group"
      >
        <div className={`${h} flex items-center gap-2 bg-black text-white rounded-xl px-3 py-1.5 border border-white/10 hover:bg-gray-900 transition-all duration-200 group-hover:scale-105`}>
          <svg viewBox="0 0 24 24" className="w-5 h-5 fill-white flex-shrink-0">
            <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.8-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z" />
          </svg>
          <div className="flex flex-col leading-none">
            <span className="text-[9px] opacity-80">{ar ? 'متوفر قريباً' : 'Coming Soon'}</span>
            <span className="text-sm font-semibold">App Store</span>
          </div>
        </div>
      </a>

      {/* Google Play */}
      <a
        href={PLAY_STORE_URL}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={ar ? 'احصل عليه على Google Play' : 'Get it on Google Play'}
        className="group"
      >
        <div className={`${h} flex items-center gap-2 bg-black text-white rounded-xl px-3 py-1.5 border border-white/10 hover:bg-gray-900 transition-all duration-200 group-hover:scale-105`}>
          <svg viewBox="0 0 24 24" className="w-5 h-5 fill-white flex-shrink-0">
            <path d="M3.18 23.76c.31.17.66.22 1.02.14l12.2-7.03-2.66-2.66-10.56 9.55zM.54 1.3C.2 1.67 0 2.2 0 2.9v18.2c0 .7.2 1.23.54 1.6l.09.08 10.2-10.2v-.24L.63 1.22l-.09.08zM20.3 10.27l-2.9-1.67-2.98 2.98 2.98 2.98 2.92-1.68c.83-.48.83-1.26-.02-1.61zM4.2.1L16.4 7.13l-2.66 2.66L3.18.24C3.5.08 3.89.05 4.2.1z" />
          </svg>
          <div className="flex flex-col leading-none">
            <span className="text-[9px] opacity-80">{ar ? 'متوفر قريباً' : 'Coming Soon'}</span>
            <span className="text-sm font-semibold">Google Play</span>
          </div>
        </div>
      </a>

      {/* PWA install hint */}
      {showPwa && (
        <button
          onClick={handlePwaInstall}
          className={`${h} flex items-center gap-2 bg-amber-500/10 text-amber-600 rounded-xl px-3 py-1.5 border border-amber-400/30 hover:bg-amber-500/20 transition-all duration-200 hover:scale-105`}
        >
          <svg viewBox="0 0 24 24" className="w-4 h-4 fill-current flex-shrink-0">
            <path d="M17 1.01L7 1c-1.1 0-2 .9-2 2v18c0 1.1.9 2 2 2h10c1.1 0 2-.9 2-2V3c0-1.1-.9-1.99-2-1.99zM17 19H7V5h10v14zm-4.2-5.78v1.75l3.2-2.99L12.8 9v1.7c-3.11.43-4.35 2.56-4.8 4.7 1.11-1.49 2.93-2.07 4.8-2.18z" />
          </svg>
          <div className="flex flex-col leading-none text-right">
            <span className="text-[9px] opacity-80">{ar ? 'ثبّت التطبيق' : 'Install App'}</span>
            <span className="text-xs font-semibold">{ar ? 'الشاشة الرئيسية' : 'Home Screen'}</span>
          </div>
        </button>
      )}
    </div>
  )
}
