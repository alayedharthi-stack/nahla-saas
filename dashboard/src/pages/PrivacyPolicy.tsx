/**
 * PrivacyPolicy.tsx
 * Public page — no auth required
 * Route: /privacy
 * Used as Meta App privacy policy URL
 */
export default function PrivacyPolicy() {
  return (
    <div className="min-h-screen bg-slate-50 py-12 px-4" dir="rtl">
      <div className="max-w-2xl mx-auto">

        {/* Header */}
        <div className="flex items-center gap-3 mb-8">
          <img
            src="/logo.png"
            alt="نحلة"
            className="w-10 h-10 rounded-xl"
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <div>
            <h1 className="text-2xl font-black text-slate-800">سياسة الخصوصية</h1>
            <p className="text-sm text-slate-500">منصة نحلة — Nahlah.ai</p>
          </div>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-8 space-y-7 text-slate-700 leading-relaxed">

          <section>
            <p className="text-base">
              تحترم منصة <strong>نحلة</strong> خصوصية المستخدمين والتجار الذين يستخدمون خدماتنا.
            </p>
          </section>

          <section>
            <h2 className="text-base font-bold text-slate-800 mb-3">البيانات التي نجمعها</h2>
            <p className="text-sm text-slate-500 mb-2">
              تقوم منصة نحلة بجمع بعض البيانات التقنية الضرورية لتشغيل الخدمة، وتشمل:
            </p>
            <ul className="space-y-1.5 text-sm">
              {[
                'رقم واتساب المرتبط بالمتجر',
                'بيانات المحادثات مع العملاء',
                'معلومات المتجر المرتبطة بالخدمة',
              ].map(item => (
                <li key={item} className="flex items-start gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-400 mt-2 shrink-0" />
                  {item}
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h2 className="text-base font-bold text-slate-800 mb-3">كيف نستخدم هذه البيانات</h2>
            <p className="text-sm text-slate-500 mb-2">تُستخدم هذه البيانات فقط لأغراض:</p>
            <ul className="space-y-1.5 text-sm">
              {[
                'تشغيل نظام الرد الذكي',
                'تحسين جودة الخدمة',
                'إدارة محادثات العملاء',
              ].map(item => (
                <li key={item} className="flex items-start gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-brand-400 mt-2 shrink-0" />
                  {item}
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h2 className="text-base font-bold text-slate-800 mb-2">مشاركة البيانات</h2>
            <p className="text-sm">
              لا تقوم منصة نحلة ببيع أو مشاركة بيانات المستخدمين مع أي طرف ثالث خارج نطاق تشغيل الخدمة.
            </p>
          </section>

          <section>
            <h2 className="text-base font-bold text-slate-800 mb-2">الخدمات الخارجية</h2>
            <p className="text-sm mb-2">قد تستخدم المنصة خدمات خارجية لتشغيل بعض الميزات:</p>
            <div className="bg-slate-50 rounded-xl border border-slate-100 px-4 py-3 text-sm">
              <strong>Meta (WhatsApp Business API)</strong>
              <span className="text-slate-500"> — لتشغيل خدمة التواصل عبر واتساب.</span>
            </div>
          </section>

          <section>
            <h2 className="text-base font-bold text-slate-800 mb-2">الموافقة</h2>
            <p className="text-sm">
              باستخدامك لمنصة نحلة فإنك توافق على هذه السياسة.
            </p>
          </section>

          <section>
            <h2 className="text-base font-bold text-slate-800 mb-2">التواصل معنا</h2>
            <p className="text-sm">
              لأي استفسار يتعلق بالخصوصية، يمكنك التواصل معنا عبر:{' '}
              <a
                href="mailto:admin@nahlah.ai"
                className="text-brand-600 font-semibold hover:underline"
              >
                admin@nahlah.ai
              </a>
            </p>
          </section>

          <div className="pt-4 border-t border-slate-100 text-xs text-slate-400 text-center">
            آخر تحديث: أبريل 2026 · منصة نحلة · nahlah.ai
          </div>
        </div>

      </div>
    </div>
  )
}
