import { COMPANY_INFO } from '../config/companyInfo'

const CANONICAL_ORIGIN = 'https://nahlah.ai'
const OG_IMAGE = `${CANONICAL_ORIGIN}/logo.png`
const SITE_NAME = COMPANY_INFO.product.nameEn
const MANAGED_ATTR = 'data-nahla-seo'
const JSON_LD_SCRIPT_ID = 'nahla-public-seo-jsonld'

export interface PublicRouteSeo {
  title: string
  description: string
  /** Path segment used to build the canonical URL (e.g. `/` or `/privacy`). */
  canonicalPath: string
  includeSoftwareApplication: boolean
  ogLocale: 'ar_SA' | 'en_US'
}

const LANDING_DESCRIPTION =
  'Nahlah AI is an AI-powered WhatsApp sales platform that helps merchants automate customer conversations, orders, and support.'

export const PUBLIC_ROUTE_SEO: Record<string, PublicRouteSeo> = {
  '/': {
    title: 'Nahlah AI | AI-powered WhatsApp Sales Platform',
    description: LANDING_DESCRIPTION,
    canonicalPath: '/',
    includeSoftwareApplication: true,
    ogLocale: 'ar_SA',
  },
  '/landing': {
    title: 'Nahlah AI | AI-powered WhatsApp Sales Platform',
    description: LANDING_DESCRIPTION,
    canonicalPath: '/',
    includeSoftwareApplication: true,
    ogLocale: 'ar_SA',
  },
  '/privacy': {
    title: 'Privacy Policy | Nahlah AI',
    description:
      'Read the Nahlah AI privacy policy covering how we collect, use, store, and protect personal data on our WhatsApp commerce platform.',
    canonicalPath: '/privacy',
    includeSoftwareApplication: false,
    ogLocale: 'en_US',
  },
  '/terms': {
    title: 'Terms of Service | Nahlah AI',
    description:
      'Review the Nahlah AI terms of service governing access to and use of our AI-powered WhatsApp sales platform.',
    canonicalPath: '/terms',
    includeSoftwareApplication: false,
    ogLocale: 'en_US',
  },
  '/contact': {
    title: 'Contact | Nahlah AI',
    description:
      'Contact Nahlah AI for product inquiries, support, and business verification using our official company details.',
    canonicalPath: '/contact',
    includeSoftwareApplication: false,
    ogLocale: 'en_US',
  },
  '/data-deletion': {
    title: 'Data Deletion | Nahlah AI',
    description:
      'Learn how to request deletion of your personal data from Nahlah AI in accordance with our data retention policies.',
    canonicalPath: '/data-deletion',
    includeSoftwareApplication: false,
    ogLocale: 'en_US',
  },
}

function normalizePathname(pathname: string): string {
  if (!pathname || pathname === '/') return '/'
  const trimmed = pathname.replace(/\/+$/, '')
  return trimmed || '/'
}

function canonicalUrl(canonicalPath: string): string {
  if (canonicalPath === '/') return `${CANONICAL_ORIGIN}/`
  return `${CANONICAL_ORIGIN}${canonicalPath}`
}

function ensureMetaByName(name: string, content: string): void {
  const matches = Array.from(
    document.head.querySelectorAll<HTMLMetaElement>(`meta[name="${name}"]`),
  )
  let el = matches[0]
  if (!el) {
    el = document.createElement('meta')
    el.setAttribute('name', name)
    document.head.appendChild(el)
  }
  el.setAttribute(MANAGED_ATTR, '')
  el.setAttribute('content', content)
  matches.filter((candidate) => candidate !== el).forEach((candidate) => candidate.remove())
}

function ensureMetaByProperty(property: string, content: string): void {
  const matches = Array.from(
    document.head.querySelectorAll<HTMLMetaElement>(`meta[property="${property}"]`),
  )
  let el = matches[0]
  if (!el) {
    el = document.createElement('meta')
    el.setAttribute('property', property)
    document.head.appendChild(el)
  }
  el.setAttribute(MANAGED_ATTR, '')
  el.setAttribute('content', content)
  matches.filter((candidate) => candidate !== el).forEach((candidate) => candidate.remove())
}

function ensureCanonicalLink(href: string): void {
  const matches = Array.from(
    document.head.querySelectorAll<HTMLLinkElement>('link[rel="canonical"]'),
  )
  let el = matches[0]
  if (!el) {
    el = document.createElement('link')
    el.setAttribute('rel', 'canonical')
    document.head.appendChild(el)
  }
  el.setAttribute(MANAGED_ATTR, '')
  el.setAttribute('href', href)
  matches.filter((candidate) => candidate !== el).forEach((candidate) => candidate.remove())
}

function removeCanonicalLink(): void {
  document.head
    .querySelectorAll<HTMLLinkElement>('link[rel="canonical"]')
    .forEach((el) => el.remove())
}

function removeMetaByName(name: string): void {
  document.head
    .querySelectorAll<HTMLMetaElement>(`meta[name="${name}"]`)
    .forEach((el) => el.remove())
}

function removeSocialMeta(): void {
  document.head
    .querySelectorAll<HTMLMetaElement>('meta[property^="og:"], meta[name^="twitter:"]')
    .forEach((el) => el.remove())
}

function buildOrganizationSchema() {
  const [streetLine, districtLine, localityLine, countryLine] =
    COMPANY_INFO.address.enLines
  const localityMatch = localityLine.match(/^(.*)\s+(\d{5}),?$/)
  const addressLocality = localityMatch?.[1] || localityLine.replace(/,$/, '')
  const postalCode = localityMatch?.[2]
  return {
    '@type': 'Organization',
    name: COMPANY_INFO.product.nameEn,
    legalName: COMPANY_INFO.legal.nameEn,
    url: COMPANY_INFO.website.url,
    email: COMPANY_INFO.email,
    telephone: COMPANY_INFO.phone.href.replace(/^tel:/, ''),
    identifier: {
      '@type': 'PropertyValue',
      propertyID: 'National Unified Number',
      value: COMPANY_INFO.nationalUnifiedNumber,
    },
    address: {
      '@type': 'PostalAddress',
      streetAddress: `${streetLine.replace(/,$/, '')}, ${districtLine.replace(/,$/, '')}`,
      addressLocality,
      ...(postalCode ? { postalCode } : {}),
      addressCountry: countryLine.replace(/\.$/, ''),
    },
  }
}

function buildWebSiteSchema() {
  return {
    '@type': 'WebSite',
    name: COMPANY_INFO.product.nameEn,
    url: COMPANY_INFO.website.url,
  }
}

function buildSoftwareApplicationSchema(description: string) {
  return {
    '@type': 'SoftwareApplication',
    name: COMPANY_INFO.product.nameEn,
    applicationCategory: 'BusinessApplication',
    operatingSystem: 'Web',
    url: COMPANY_INFO.website.url,
    description,
  }
}

function applyJsonLd(route: PublicRouteSeo): void {
  const graph = [buildOrganizationSchema(), buildWebSiteSchema()]
  if (route.includeSoftwareApplication) {
    graph.push(buildSoftwareApplicationSchema(route.description))
  }

  let script = document.getElementById(JSON_LD_SCRIPT_ID) as HTMLScriptElement | null
  if (!script) {
    script = document.createElement('script')
    script.id = JSON_LD_SCRIPT_ID
    script.type = 'application/ld+json'
    script.setAttribute(MANAGED_ATTR, '')
    document.head.appendChild(script)
  }

  script.textContent = JSON.stringify({
    '@context': 'https://schema.org',
    '@graph': graph,
  })
}

function removeJsonLd(): void {
  document.getElementById(JSON_LD_SCRIPT_ID)?.remove()
}

function applyIndexableRouteMeta(route: PublicRouteSeo, pageUrl: string): void {
  document.title = route.title
  ensureMetaByName('description', route.description)
  ensureMetaByName('robots', 'index,follow')
  ensureCanonicalLink(pageUrl)

  ensureMetaByProperty('og:title', route.title)
  ensureMetaByProperty('og:description', route.description)
  ensureMetaByProperty('og:type', 'website')
  ensureMetaByProperty('og:url', pageUrl)
  ensureMetaByProperty('og:site_name', SITE_NAME)
  ensureMetaByProperty('og:locale', route.ogLocale)
  ensureMetaByProperty('og:image', OG_IMAGE)
  ensureMetaByProperty('og:image:alt', `${SITE_NAME} logo`)

  ensureMetaByName('twitter:card', 'summary')
  ensureMetaByName('twitter:title', route.title)
  ensureMetaByName('twitter:description', route.description)
  ensureMetaByName('twitter:image', OG_IMAGE)
  ensureMetaByName('twitter:image:alt', `${SITE_NAME} logo`)

  applyJsonLd(route)
}

function applyNoIndexMeta(): void {
  document.title = SITE_NAME
  removeMetaByName('description')
  ensureMetaByName('robots', 'noindex,nofollow')
  removeSocialMeta()
  removeCanonicalLink()
  removeJsonLd()
}

/**
 * Apply route-aware SEO metadata for the initial SPA pathname before React renders.
 * Safe to call repeatedly; managed tags are updated in place without duplication.
 */
export function applyPublicSeo(pathname: string = window.location.pathname): void {
  const normalizedPath = normalizePathname(pathname)
  const route = PUBLIC_ROUTE_SEO[normalizedPath]

  if (route) {
    applyIndexableRouteMeta(route, canonicalUrl(route.canonicalPath))
    return
  }

  applyNoIndexMeta()
}
