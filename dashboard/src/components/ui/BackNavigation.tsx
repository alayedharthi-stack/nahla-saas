import { ArrowLeft } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useLanguage } from '../../i18n/context'
import { resolvePageMetaSelector } from '../../lib/pageMetadata'
import { parentHref } from '../../navigation/upNavigation'
import { useUpNavigation } from '../../navigation/useUpNavigation'

interface BackNavigationProps {
  currentTitle: string
}

export default function BackNavigation({ currentTitle }: BackNavigationProps) {
  const { t, dir } = useLanguage()
  const { match, goUp } = useUpNavigation()

  if (!match.showBack || !match.parentPath) return null

  const parentSelector = resolvePageMetaSelector(match.parentPath)
  const parentTitle = parentSelector
    ? t(parentSelector).title
    : t(tr => tr.nav.back)
  const aria = t(tr => tr.nav.backTo).replace('{parent}', parentTitle)
  const parentTo = parentHref(match.parentPath)

  return (
    <div
      className="flex items-center gap-1 min-w-0"
      data-testid="platform-up-nav"
      data-dir={dir}
      data-parent={match.parentPath}
      data-kind={match.kind}
    >
      <button
        type="button"
        onClick={goUp}
        aria-label={aria}
        title={aria}
        className="shrink-0 w-9 h-9 flex items-center justify-center rounded-lg hover:bg-slate-50 dark:hover:bg-slate-800 text-slate-600 dark:text-slate-300 transition-colors"
      >
        <ArrowLeft
          className="w-5 h-5 rtl:-scale-x-100"
          aria-hidden="true"
        />
      </button>
      {match.showBreadcrumb && (
        <nav
          aria-label={aria}
          className="hidden lg:flex items-center gap-1.5 min-w-0 text-xs text-slate-400"
        >
          <Link
            to={parentTo}
            className="truncate max-w-[10rem] hover:text-slate-700 dark:hover:text-slate-200"
          >
            {parentTitle}
          </Link>
          <span aria-hidden="true">/</span>
          <span className="truncate max-w-[12rem] text-slate-500 dark:text-slate-400">
            {currentTitle}
          </span>
        </nav>
      )}
    </div>
  )
}
