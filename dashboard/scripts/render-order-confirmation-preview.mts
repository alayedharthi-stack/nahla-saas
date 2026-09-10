/**
 * Render WaBubblePreview-equivalent HTML and capture a PNG for PR evidence.
 *
 * Run: npx --yes tsx@4 scripts/render-order-confirmation-preview.mts
 */
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import {
  ORDER_CONFIRMATION_PREVIEW_SAMPLES,
  buildOrderUpdatePreviewBody,
} from '../src/components/settings/orderUpdatesPreview.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = join(__dirname, '..', '..')
const OUT_DIR = join(REPO_ROOT, 'docs', 'evidence')
const OUT_FILE = join(OUT_DIR, 'pr976-order-confirmation-preview-r3.png')

const R2_HEADER =
  'https://pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev/platform/order-updates/order-confirmation-header-v1.jpg'

const r3Body =
  'تم استلام طلبك يا {{1}} 📦\n\nمن {{4}}\nرقم الطلب: #{{2}}\nالمبلغ الإجمالي: {{3}} ريال\n\nسنبدأ تجهيز طلبك فوراً ونُعلمك بكل جديد.'

const previewBody = buildOrderUpdatePreviewBody(
  r3Body,
  ['customer_name', 'order_number', 'order_total', 'store_name'],
  ORDER_CONFIRMATION_PREVIEW_SAMPLES,
)

const html = `<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8" />
  <style>
    body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: #f8fafc; }
    .wrap { padding: 24px; display: flex; justify-content: center; }
    .bubble-bg { background: #e5ddd5; border-radius: 12px; padding: 16px; display: flex; align-items: flex-end; min-height: 112px; width: 320px; }
    .bubble { background: #fff; border-radius: 16px; border-bottom-left-radius: 4px; box-shadow: 0 1px 2px rgba(0,0,0,.08); max-width: 288px; width: 100%; overflow: hidden; }
    .header img { width: 100%; height: 128px; object-fit: cover; border-bottom: 1px solid #f1f5f9; display: block; }
    .content { padding: 12px; }
    .body { color: #1e293b; font-size: 12px; line-height: 1.6; white-space: pre-line; margin: 0; }
    .ticks { color: #cbd5e1; font-size: 10px; text-align: end; margin-top: 8px; }
    .caption { margin-top: 12px; text-align: center; color: #64748b; font-size: 11px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div>
      <div class="bubble-bg">
        <div class="bubble">
          <div class="header"><img src="${R2_HEADER}" alt="" /></div>
          <div class="content">
            <p class="body">${previewBody.replace(/\n/g, '<br/>')}</p>
            <div class="ticks">✓✓</div>
          </div>
        </div>
      </div>
      <p class="caption">معاينة تأكيد الطلب (r3) — HEADER بالصورة، بدون تذييل نحلة</p>
    </div>
  </div>
</body>
</html>`

await mkdir(OUT_DIR, { recursive: true })
const tmpHtml = join(OUT_DIR, 'pr976-order-confirmation-preview-r3.html')
await writeFile(tmpHtml, html, 'utf8')

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 420, height: 520 } })
await page.goto(`file://${tmpHtml.replace(/\\/g, '/')}`)
await page.waitForTimeout(1500)
await page.screenshot({ path: OUT_FILE })
await browser.close()

console.log(`render-order-confirmation-preview: wrote ${OUT_FILE}`)
