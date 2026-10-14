/**
 * Local-only harness for scripts/check-inbound-media-cache-isolation.mts.
 *
 * Drives the real session code (auth.ts loginDetailed / logout /
 * setApiBaseOverride) against a local fake-account API and mounts the real
 * MessagePresentationCard -> InboundMediaPreview -> useAuthedMediaBlob /
 * useAuthedDocumentBlob path. Nothing here touches the browser cache,
 * intercepts requests or replaces the service worker. Served by the Vite
 * dev server only; not part of the production bundle.
 */
import React from 'react'
import ReactDOM from 'react-dom/client'

import {
  getTenantId,
  getToken,
  loginDetailed,
  logout,
  setApiBaseOverride,
} from '../../../src/auth'
import MessagePresentationCard, {
  type MessagePresentationLabels,
} from '../../../src/components/conversations/MessagePresentationCard'
import type { DashboardMessageMedia, MessagePresentation } from '../../../src/api/featureReality'

export interface CacheCase {
  id: string
  kind: 'image' | 'document'
  url: string
}

const labels: MessagePresentationLabels = {
  template: 'WhatsApp template',
  lifecycle: 'Order update',
  product: 'Product',
  available: 'Available',
  unavailable: 'Unavailable',
  mediaUnavailable: 'Media unavailable',
  openProduct: 'View product',
  openCatalog: 'Open catalog',
  openMedia: 'Open attachment',
}

// The API origin comes from the page URL, set through the same runtime
// override the login diagnostics panel uses.
const apiOrigin = new URLSearchParams(window.location.search).get('api')
if (apiOrigin) setApiBaseOverride(apiOrigin)

function presentationFor(c: CacheCase): MessagePresentation {
  return {
    version: 'message_presentation_v1',
    kind: 'media',
    body: '',
    text_direction: 'auto',
    media: { kind: c.kind, url: c.url, load_state: 'ok' },
    actions: [],
  }
}

function legacyFor(c: CacheCase, index: number): DashboardMessageMedia {
  if (c.kind === 'image') {
    return {
      kind: 'image',
      message_event_id: 7000 + index,
      storage_url: c.url,
      mime_type: 'image/png',
      description: null,
      image_kind: null,
      vision_status: 'ok',
      download_status: 'ok',
      ai_used: false,
      caption: null,
      error: null,
    }
  }
  return {
    kind: 'document',
    message_event_id: 7000 + index,
    storage_url: c.url,
    mime_type: 'application/pdf',
    filename: 'synthetic.pdf',
    byte_size: null,
    download_status: 'ok',
    pdf_kind: 'unknown',
    pdf_text_status: null,
    summary: null,
    payment_evidence_hints: null,
    payment_evidence_status: null,
    caption: null,
    error: null,
  }
}

function Cards({ cases }: { cases: CacheCase[] }) {
  return (
    <div style={{ display: 'grid', gap: 12, padding: 12, maxWidth: 420 }}>
      {cases.map((c, index) => (
        <section key={c.id} data-case={c.id}>
          <MessagePresentationCard
            presentation={presentationFor(c)}
            isOutbound={false}
            labels={labels}
            legacyMedia={legacyFor(c, index)}
          />
        </section>
      ))}
    </div>
  )
}

const root = ReactDOM.createRoot(document.getElementById('root')!)

declare global {
  interface Window {
    __cacheHarness?: {
      login: (email: string, password: string) => Promise<{ ok: boolean; tenantId: number | null }>
      logout: () => void
      logoutAndNavigate: (url: string) => void
      show: (cases: CacheCase[]) => void
      hide: () => void
      session: () => { tenantId: number | null; hasToken: boolean }
    }
  }
}

window.__cacheHarness = {
  // Real login: POST /auth/login-form, then the real session persistence.
  login: async (email, password) => {
    const result = await loginDetailed(email, password)
    return { ok: result.ok, tenantId: getTenantId() }
  },
  // Header.tsx handleLogout: logout() then an in-app route change.
  logout: () => logout(),
  // api/client.ts forced logout: logout() then a full document navigation.
  logoutAndNavigate: (url: string) => {
    logout()
    window.location.href = url
  },
  // Conversation view mounts the cards (StrictMode, like main.tsx).
  show: cases => root.render(
    <React.StrictMode>
      <Cards cases={cases} />
    </React.StrictMode>,
  ),
  // Route change away from the conversation view unmounts every card.
  hide: () => root.render(<></>),
  session: () => ({ tenantId: getTenantId(), hasToken: !!getToken() }),
}

document.documentElement.dataset.harnessReady = '1'
