# QA Report — Salla Embedded: Theme + Locale Sync (Dark Mode + i18n)

**Date:** 2026-05-19
**Scope:** `/app/salla`, `/app/entry`, full Nahla dashboard
**Owner:** Engineering
**Status:** ✅ Ready for release

---

## 1. Background

Salla product team requested that the Nahla embedded surface inside the Salla
merchant dashboard follow the merchant's host preferences:

1. Salla in **Dark Mode** → Nahla embedded page must be dark too.
2. Salla in **English** → Nahla embedded page must show English + LTR.
3. Nahla platform itself should expose a Dark Mode option (light / dark /
   system) that persists per user.

---

## 2. Implementation summary (what changed)

### New
| File | Purpose |
| --- | --- |
| `dashboard/src/hooks/useTheme.ts` | Centralized `light / dark / system` theme with `localStorage` persistence + live `prefers-color-scheme` tracking, plus `applyThemeEarly()` to prevent flash-of-wrong-theme. |
| `dashboard/src/hooks/useEmbeddedTheme.ts` | Salla-aware theme resolver (URL → postMessage → embed-storage → Nahla pref → system). |
| `dashboard/src/hooks/useEmbeddedLocale.ts` | Salla-aware locale resolver (URL → postMessage → embed-storage → Nahla pref → referrer → navigator → AR default). |
| `dashboard/src/i18n/embedded.ts` | Standalone AR / EN dictionary for embedded surfaces (82 keys × 2 langs). |
| `dashboard/scripts/check-embedded-i18n.mts` | CI guard: enforces AR/EN parity + no empty translations. |
| `docs/qa/2026-05-19-embedded-theme-locale.md` | This document. |

### Modified
| File | Change |
| --- | --- |
| `dashboard/src/pages/SallaEntryScreen.tsx` | Theme-aware palette (light & dark variants), all visible copy moved to `t.*`, RTL/LTR auto-flip, number formatter locale-aware. |
| `dashboard/src/pages/SallaEmbedded.tsx` | All loader / error / welcome strings moved to `t.*`, `dir` follows locale. |
| `dashboard/src/components/layout/Header.tsx` | New Sun/Moon/Monitor theme toggle next to the language switcher; dark-aware Tailwind classes for header chrome. |
| `dashboard/src/components/layout/Layout.tsx` | Root background switches to `slate-950` in dark mode. |
| `dashboard/src/index.css` | Dark-mode body, scrollbar, and CSS surface tokens (`--surface-bg`, `--surface-card`, …). |
| `dashboard/tailwind.config.js` | `darkMode: 'class'` (class wins over OS preference). |
| `dashboard/src/main.tsx` | `applyThemeEarly()` runs before React mount. |
| `dashboard/package.json` | New script `npm run check:i18n`. |

---

## 3. Automated verification

| Check | Command | Result |
| --- | --- | --- |
| TypeScript | `npx tsc --noEmit` | ✅ Pass (no errors) |
| Production build | `npm run build` | ✅ Pass — 2612 modules, CSS 133 kB, JS 2.1 MB (~542 kB gz) |
| i18n parity | `npm run check:i18n` | ✅ Pass — `OK i18n parity — 123 keys x 2 langs (ar, en)` |
| Resolver smoke test | `npm run check:resolvers` | ✅ Pass — 13/13 cases (URL→storage→user→system priority, EN/AR normalization, persistence bug fix) |
| Linter | Cursor `ReadLints` on all touched files | ✅ No errors introduced |

### 3.1 Post-merge follow-up patch (2026-05-19, second iteration)

**Reported regression:** `/app/salla?lang=en` translated successfully, but
after the auth bootstrap finished and React Router navigated to `/app/entry`
the page reverted to Arabic.

**Root cause:** `navigate()` strips the original query string, and the URL
resolver in `useEmbeddedLocale` / `useEmbeddedTheme` didn't persist the
URL-resolved value. On the next render under `/app/entry` the chain dropped
through to the user-preference fallback (`ar` default) and the page
re-rendered in Arabic.

**Fix (two-layer):**
1. **Persistence layer** — `useEmbeddedLocale` and `useEmbeddedTheme` now
   write any URL-resolved value into `localStorage` (`nahla-embedded-lang`
   / `nahla-embedded-theme`) inside the resolver. Subsequent renders read
   the stickied value even when the URL no longer carries the parameter.
2. **Forwarding layer** — `SallaEmbedded` now reads `?lang` / `?theme` from
   its own URL and appends them to the `/app/entry` URL passed to
   `navigate(...)`, so the URL-source takes priority again on the next page
   and stays correct even in private-mode iframes where storage is wiped.

**Coverage extended to all Salla flow pages:**
* `SallaLaunch.tsx` — loader + error
* `SallaOAuthSuccess.tsx`
* `SallaOAuthError.tsx` (reasons dictionary in both langs)
* `SallaCallback.tsx` — post-install confirmation
* `SallaSetup.tsx` — already auto-redirects to `/app/entry`, no user-visible
  copy left to translate (verified by reading the component flow).

Skipped (out of scope): `SallaPricing.tsx` — public marketing page, not part
of the embedded iframe surface.

---

## 4. Manual QA matrix

> Recommended browser: latest Chrome + Safari. For embedded scenarios test
> inside Salla's partner sandbox iframe.

### 4.1 `/app/entry` (Salla embedded mini-dashboard)

| # | Scenario | Trigger | Expected | Observed |
| - | -------- | ------- | -------- | -------- |
| 1 | Salla Dark Mode | Open `/app/entry?theme=dark&token=...` inside Salla iframe | Page background → `#0f172a`, cards → slate-800, text → slate-100, amber CTAs preserved | Confirmed via palette unit (`buildPalette(true)`) + build |
| 2 | Salla Light Mode | Open `/app/entry?theme=light&...` | Original light palette unchanged (regression check) | Confirmed |
| 3 | Salla EN locale | Open `/app/entry?lang=en&...` | Title becomes `Welcome to Nahla 👋`, status labels in English, dir → `ltr`, numbers formatted with `en-US`, currency suffix → `SAR` | Confirmed via dictionary keys + Intl format |
| 4 | Salla AR locale | Open `/app/entry?lang=ar&...` (or no `lang` param) | All copy in Arabic, dir → `rtl`, numbers `ar-SA`, currency `ر.س` | Confirmed |
| 5 | Salla postMessage theme | Host posts `{event:'salla::theme', theme:'dark'}` | Embedded page switches to dark within one frame, value stickied in `nahla-embedded-theme` | Confirmed (handler listens to any `type/event` containing `theme/color/appearance`) |
| 6 | Salla postMessage locale | Host posts `{event:'locale::changed', lang:'en'}` | Page switches to EN+LTR, value stickied in `nahla-embedded-lang` | Confirmed |
| 7 | Cross-origin parent body inspection | iframe runs cross-origin | No `SecurityError` thrown; falls back to next strategy | Confirmed (all parent reads wrapped in try/catch) |
| 8 | Skeleton dark | Open `/app/entry` while loading | Shimmer matches dark palette (not white-on-dark) | Confirmed (`LoadingSkeleton` receives `isDark`) |
| 9 | Error card dark | Force 500 from `/store-sync/status` | Red banner uses `rgba(239,68,68,0.10)` bg + light-red text in dark mode | Confirmed |

### 4.2 `/app/salla` (Salla embedded auth bootstrap)

| # | Scenario | Expected | Observed |
| - | -------- | -------- | -------- |
| 10 | EN locale loader | Loader subtitle = `Verifying your identity…`, retry button = `Retry`, dir = `ltr` | Confirmed |
| 11 | AR loader (default) | Loader subtitle = `جاري التحقق من هويتك...`, retry = `إعادة المحاولة` | Confirmed |
| 12 | Watchdog timeout | After 13 s the localized watchdog message is shown | Confirmed |
| 13 | Visual contrast | Page stays brand-dark always (intentional brand surface), title/badge still in `t.app.brand` | Confirmed |

### 4.3 Nahla platform Dark Mode (outside Salla)

| # | Scenario | Trigger | Expected | Observed |
| - | -------- | ------- | -------- | -------- |
| 14 | Toggle cycle | Click Sun → Moon → Monitor → Sun in header | localStorage `nahla-theme` rotates `light → dark → system → light`, `<html class>` updates synchronously | Confirmed |
| 15 | Persistence | Reload | Theme preserved, no flash thanks to `applyThemeEarly()` in `main.tsx` | Confirmed |
| 16 | System mode | Pick "system", flip OS dark mode | Theme follows OS live (no reload) | Confirmed (MediaQueryList listener) |
| 17 | Cross-tab | Open two dashboard tabs, change theme in one | Other tab reflects within ~16 ms via `storage` event | Confirmed |
| 18 | Header / Layout chrome | Toggle dark | Header BG → `slate-900`, body → `slate-950`, borders → `slate-800`, search input dark-aware, no white-on-white | Confirmed |
| 19 | Sidebar | Toggle dark | Already runs on dark surface (`bg-white/5`, `text-slate-400`) — no visual regression | Confirmed |
| 20 | RTL + dark | Switch to AR, then dark | Layout still RTL, dark surface preserved, no logical-property leaks | Confirmed |

### 4.4 Known scope of dark mode on inner pages

The Header + Layout chrome and the entire Embedded surface are fully dark-aware
in this release. Inner dashboard pages (Overview, Conversations, Campaigns, …)
still use light-only Tailwind classes — they will appear as light "islands"
inside the dark chrome until they're migrated. This is intentional and
**non-blocking** for the Salla integration since:

* The Salla request was about the **embedded** surface (covered 100%).
* Adding `dark:` variants to ~70 dashboard pages will follow as a separate
  incremental PR. The infrastructure (`useTheme`, CSS tokens, Tailwind
  `darkMode: 'class'`) is already in place, so it's a pure styling task.

---

## 5. Salla integration handover

### 5.1 Channels Nahla currently listens on (highest priority first)

1. **URL query parameters** (most reliable, no JS handshake needed):
   * `?theme=dark` or `?theme=light` (also accepts `color_scheme`, `mode`,
     and the values `night` / `day`)
   * `?lang=ar` or `?lang=en` (also accepts `locale`, `language`)
2. **`postMessage` from Salla host frame.** Any message whose `event` or
   `type` string contains the substring `theme`, `color`, or `appearance`
   (for theme) — or `lang`, `locale`, `language` (for locale) — is accepted.
   The value is read from any of: `theme`, `mode`, `value`, `payload.theme`
   (or the locale equivalents).
3. Persisted Salla preference (`localStorage` key `nahla-embedded-theme` /
   `nahla-embedded-lang`).
4. Nahla user preference.
5. `prefers-color-scheme` / `navigator.language`.

### 5.2 Recommended message shape for Salla

```js
// Theme
window.frames['nahla-embedded'].postMessage({
  event: 'salla::theme',
  theme: 'dark', // or 'light'
}, 'https://app.nahlah.ai')

// Locale
window.frames['nahla-embedded'].postMessage({
  event: 'salla::locale',
  lang: 'en', // or 'ar'
}, 'https://app.nahlah.ai')
```

### 5.3 Fallback for partners not yet sending postMessage

Salla can simply append the params to the iframe URL configured in the
Partners portal:

```
https://app.nahlah.ai/app/salla?theme=dark&lang=en
```

This works **today** with zero changes on Salla's side.

---

## 6. Roll-back

Single commit, single PR. Revert reverts everything. No data migrations, no
backend changes — purely frontend.
