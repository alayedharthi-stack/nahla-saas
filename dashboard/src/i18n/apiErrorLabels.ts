/**
 * Transport-level error labels for the shared API client.
 *
 * `api/client.ts` runs outside React, so it cannot use `useLanguage()`.
 * It reads the persisted language (same key the provider writes) and picks
 * a static label here. These are UI strings about the connection itself —
 * never backend/merchant content.
 */
import { readStoredLang } from './context'

type Lang = 'ar' | 'en'

const LABELS = {
  ar: {
    networkUnreachable: 'تعذر الوصول إلى الخادم. قد يكون السبب CORS أو انقطاع الشبكة أو خطأ مؤقت في API.',
    timeout:            (seconds: number) => `انتهت مهلة الطلب (${seconds}s). تحقق من الخادم أو الشبكة.`,
    unexpected:         'حدث خطأ غير متوقع أثناء الاتصال بالخادم.',
    validation:         (parts: string) => `بيانات الطلب غير صالحة — ${parts}`,
    validationJoiner:   '؛ ',
    sessionExpired:     'انتهت صلاحية الجلسة — يرجى تسجيل الدخول مجدداً',
    unauthorized:       'غير مصرح',
    uploadFailed:       'تعذر رفع الصورة — تحقق من الاتصال بالخادم.',
    uploadSessionEnded: 'انتهت الجلسة — سجّل الدخول مجدداً.',
  },
  en: {
    networkUnreachable: 'Could not reach the server. Possible causes: CORS, a network outage, or a temporary API error.',
    timeout:            (seconds: number) => `The request timed out (${seconds}s). Check the server or your network.`,
    unexpected:         'An unexpected error occurred while contacting the server.',
    validation:         (parts: string) => `Invalid request data — ${parts}`,
    validationJoiner:   '; ',
    sessionExpired:     'Your session has expired — please sign in again.',
    unauthorized:       'Not authorized',
    uploadFailed:       'Could not upload the image — check the connection to the server.',
    uploadSessionEnded: 'Session ended — sign in again.',
  },
} as const

export function apiErrorLabels(lang: Lang = readStoredLang()) {
  return LABELS[lang]
}
