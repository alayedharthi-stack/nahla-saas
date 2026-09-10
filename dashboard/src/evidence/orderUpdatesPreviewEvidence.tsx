import '../index.css'
import { createRoot } from 'react-dom/client'
import { WaBubblePreview } from '../components/settings/WaBubblePreview'
import {
  ORDER_CONFIRMATION_PREVIEW_SAMPLES,
  resolveOrderUpdatePreview,
} from '../components/settings/orderUpdatesPreview'
import { ACTIVE_TEXT_PENDING_IMAGE_DETAIL } from './fixtures/activeTextPendingImageDetail'

const variableKeys = ['customer_name', 'order_number', 'order_total', 'store_name']
const preview = resolveOrderUpdatePreview(
  ACTIVE_TEXT_PENDING_IMAGE_DETAIL,
  variableKeys,
  ORDER_CONFIRMATION_PREVIEW_SAMPLES,
)

const root = document.getElementById('root')
if (!root) throw new Error('missing #root')

createRoot(root).render(
  <div className="min-h-screen bg-slate-50 p-6" dir="rtl">
    <p className="text-xs font-semibold text-slate-600 mb-2">معاينة</p>
    <WaBubblePreview
      body={preview.body}
      footer={preview.footer}
      headerImageUrl={preview.headerImageUrl}
    />
    <p className="text-[11px] text-slate-500 mt-3 text-center">
      نشط نصي + مسودة IMAGE — مصدر موحّد من استجابة API (مسودة أولاً)
    </p>
  </div>,
)
