import { useEffect, useState } from 'react'
import { CheckCircle, XCircle, Loader2, Mail } from 'lucide-react'
import { Link } from 'react-router-dom'

type Status = 'loading' | 'success' | 'invalid' | 'not_found' | 'pending'

export default function VerifyEmail() {
  const [status, setStatus] = useState<Status>('loading')

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const s = params.get('status')
    const token = params.get('token')

    if (s === 'success')   { setStatus('success');   return }
    if (s === 'invalid')   { setStatus('invalid');   return }
    if (s === 'not_found') { setStatus('not_found'); return }

    // If there's a token in URL, redirect to backend to process
    if (token) {
      window.location.href = `/api/auth/verify-email?token=${token}`
      return
    }

    // No token, no status — show "check your email"
    setStatus('pending')
  }, [])

  if (status === 'loading') {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center" dir="rtl">
        <Loader2 className="w-8 h-8 text-brand-400 animate-spin" />
      </div>
    )
  }

  const configs = {
    success: {
      icon: <CheckCircle className="w-10 h-10 text-emerald-500" />,
      bg: 'bg-emerald-50',
      title: 'تم تأكيد بريدك الإلكتروني ✅',
      body: 'حسابك مفعّل بالكامل. يمكنك الآن الدخول للوحة التحكم.',
      action: <Link to="/overview" className="block w-full text-center px-4 py-2.5 rounded-lg bg-brand-500 text-white text-sm font-semibold hover:bg-brand-600 transition-colors">الدخول إلى لوحة التحكم</Link>,
    },
    invalid: {
      icon: <XCircle className="w-10 h-10 text-red-500" />,
      bg: 'bg-red-50',
      title: 'رابط التحقق غير صالح',
      body: 'الرابط منتهي الصلاحية أو غير صحيح. سجّل دخولك وسنرسل لك رابطاً جديداً.',
      action: <Link to="/login" className="block w-full text-center px-4 py-2.5 rounded-lg border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors">تسجيل الدخول</Link>,
    },
    not_found: {
      icon: <XCircle className="w-10 h-10 text-red-500" />,
      bg: 'bg-red-50',
      title: 'البريد غير موجود',
      body: 'لم يتم العثور على حساب بهذا البريد.',
      action: <Link to="/register" className="block w-full text-center px-4 py-2.5 rounded-lg bg-brand-500 text-white text-sm font-semibold hover:bg-brand-600 transition-colors">إنشاء حساب جديد</Link>,
    },
    pending: {
      icon: <Mail className="w-10 h-10 text-brand-500" />,
      bg: 'bg-brand-50',
      title: 'تحقق من بريدك الإلكتروني',
      body: 'أرسلنا لك رابط تأكيد. افتح بريدك وانقر على الرابط لتفعيل حسابك.',
      action: <Link to="/login" className="block w-full text-center px-4 py-2.5 rounded-lg border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors">تسجيل الدخول</Link>,
    },
  }

  const cfg = configs[status]

  return (
    <div className="min-h-screen bg-slate-900 flex items-center justify-center px-4" dir="rtl">
      <div className="bg-white rounded-2xl shadow-xl p-8 max-w-sm w-full text-center space-y-5">
        <div className={`w-16 h-16 ${cfg.bg} rounded-full flex items-center justify-center mx-auto`}>
          {cfg.icon}
        </div>
        <div className="space-y-2">
          <h2 className="text-lg font-bold text-slate-900">{cfg.title}</h2>
          <p className="text-slate-500 text-sm">{cfg.body}</p>
        </div>
        {cfg.action}
      </div>
    </div>
  )
}
