import { useCallback, useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { resolveRouteHierarchy } from './routeHierarchy'
import { parentHref, recordLocationContext, shouldUseHistoryBack } from './upNavigation'

export function useUpNavigation() {
  const location = useLocation()
  const navigate = useNavigate()
  const match = resolveRouteHierarchy(location.pathname)

  useEffect(() => {
    recordLocationContext(location.pathname, location.search)
  }, [location.pathname, location.search])

  const goUp = useCallback(() => {
    if (!match.parentPath) return
    const useHistory = shouldUseHistoryBack({
      historyIdx: window.history.state?.idx,
      referrer: document.referrer,
      currentOrigin: window.location.origin,
      parentPath: match.parentPath,
    })
    if (useHistory) {
      navigate(-1)
      return
    }
    navigate(parentHref(match.parentPath))
  }, [match.parentPath, navigate])

  return { match, goUp }
}
