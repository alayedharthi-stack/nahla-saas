import { Navigate, useLocation } from 'react-router-dom'

/**
 * Templates Hub — shared library first, then the two template areas.
 */
export default function TemplatesHub() {
  const { search } = useLocation()
  return <Navigate to={`/templates${search}`} replace />
}
