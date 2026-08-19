import { useCallback, useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { resolveRouteHierarchy } from './routeHierarchy'
import { parentHref, recordLocationContext } from './upNavigation'

export function useUpNavigation() {
  const location = useLocation()
  const navigate = useNavigate()
  const match = resolveRouteHierarchy(location.pathname)

  useEffect(() => {
    recordLocationContext(location.pathname, location.search)
  }, [location.pathname, location.search])

  const goUp = useCallback(() => {
    if (!match.parentPath) return
    // Always the canonical in-app parent. Browser Back remains native and
    // separate; this Up action must not pop to an external site or Home.
    navigate(parentHref(match.parentPath))
  }, [match.parentPath, navigate])

  return { match, goUp }
}
