/**
 * Contact.tsx
 * Public page — no auth required
 * Route: /contact
 * Required by Meta for WhatsApp Business Platform app review.
 */
import { COMPANY_INFO } from '../config/companyInfo'

export default function Contact() {
  return (
    <div className="min-h-screen bg-slate-50 py-12 px-4" lang="en" dir="ltr">
      <div className="max-w-2xl mx-auto">

        {/* Header */}
        <div className="flex items-center gap-3 mb-8">
          <img
            src="/logo.png"
            alt={COMPANY_INFO.product.nameEn}
            className="w-10 h-10 rounded-xl"
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <div>
            <h1 className="text-2xl font-black text-slate-800">Contact Us</h1>
            <p className="text-sm text-slate-500">
              {COMPANY_INFO.product.nameEn} — {COMPANY_INFO.website.display}
            </p>
          </div>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-8 space-y-7 text-slate-700 leading-relaxed text-sm">

          <section>
            <p className="text-base">
              If you need support or have questions about the{' '}
              <strong>{COMPANY_INFO.product.nameEn}</strong> platform, we're here to help.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* About */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">About {COMPANY_INFO.product.nameEn}</h2>
            <p>
              {COMPANY_INFO.product.nameEn} is a platform designed to help businesses automate
              customer communication and sales through WhatsApp using
              artificial intelligence. Merchants can connect their online
              stores, manage conversations, send campaigns, and recover
              abandoned carts — all in one place.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Official company information */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">
              Official Company Information · معلومات الشركة الرسمية
            </h2>
            <div className="grid gap-6 sm:grid-cols-2 sm:gap-8">
              <div className="space-y-3" dir="ltr">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    Legal Entity
                  </p>
                  <p className="text-slate-800">{COMPANY_INFO.legal.nameEn}</p>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    National Unified Number
                  </p>
                  <p className="text-slate-800 font-mono">{COMPANY_INFO.nationalUnifiedNumber}</p>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    National Address
                  </p>
                  <p className="text-slate-600">
                    {COMPANY_INFO.address.enLines.map((line) => (
                      <span key={line}>
                        {line}
                        <br />
                      </span>
                    ))}
                  </p>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    Website
                  </p>
                  <a
                    href={COMPANY_INFO.website.url}
                    className="text-violet-600 hover:underline"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {COMPANY_INFO.website.url}
                  </a>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    Email
                  </p>
                  <a href={`mailto:${COMPANY_INFO.email}`} className="text-violet-600 hover:underline">
                    {COMPANY_INFO.email}
                  </a>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    Phone
                  </p>
                  <a href={COMPANY_INFO.phone.href} className="text-violet-600 hover:underline">
                    {COMPANY_INFO.phone.display}
                  </a>
                </div>
              </div>

              <div className="space-y-3 text-slate-600" dir="rtl">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    الكيان القانوني
                  </p>
                  <p className="text-slate-800">{COMPANY_INFO.legal.nameAr}</p>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    الرقم الوطني الموحد
                  </p>
                  <p className="text-slate-800 font-mono">{COMPANY_INFO.nationalUnifiedNumber}</p>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    العنوان الوطني
                  </p>
                  <p>{COMPANY_INFO.address.ar}</p>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    الموقع الإلكتروني
                  </p>
                  <a
                    href={COMPANY_INFO.website.url}
                    className="text-violet-600 hover:underline"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {COMPANY_INFO.website.display}
                  </a>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    البريد الإلكتروني
                  </p>
                  <a href={`mailto:${COMPANY_INFO.email}`} className="text-violet-600 hover:underline">
                    {COMPANY_INFO.email}
                  </a>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-1">
                    الهاتف
                  </p>
                  <a href={COMPANY_INFO.phone.href} className="text-violet-600 hover:underline">
                    {COMPANY_INFO.phone.display}
                  </a>
                </div>
              </div>
            </div>
          </section>

          <hr className="border-slate-100" />

          {/* Email */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">Email Support</h2>
            <p className="mb-3">
              For general inquiries, technical support, billing, or partnership
              requests, send us an email and we will respond as soon as
              possible:
            </p>
            <a
              href={`mailto:${COMPANY_INFO.email}`}
              className="inline-flex items-center gap-2 px-5 py-3 bg-violet-600 text-white text-sm font-bold rounded-xl hover:bg-violet-500 transition-colors"
            >
              {COMPANY_INFO.email}
            </a>
          </section>

          <hr className="border-slate-100" />

          {/* Data deletion */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">Data Deletion Requests</h2>
            <p className="mb-3">
              To request deletion of your personal data, please visit our
              dedicated data deletion page:
            </p>
            <a
              href="/data-deletion"
              className="inline-block px-4 py-2.5 bg-slate-800 text-white text-sm font-semibold rounded-xl hover:bg-slate-700 transition-colors"
            >
              Data Deletion Instructions →
            </a>
          </section>

          <hr className="border-slate-100" />

          {/* Legal */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">Legal</h2>
            <div className="flex flex-wrap gap-3">
              <a href="/privacy"       className="text-violet-600 hover:underline">Privacy Policy</a>
              <span className="text-slate-300">·</span>
              <a href="/terms"         className="text-violet-600 hover:underline">Terms of Service</a>
              <span className="text-slate-300">·</span>
              <a href="/data-deletion" className="text-violet-600 hover:underline">Data Deletion</a>
            </div>
          </section>

          {/* Footer */}
          <div className="pt-2 border-t border-slate-100">
            <p className="text-xs text-slate-400">
              © 2026 {COMPANY_INFO.product.nameEn} · {COMPANY_INFO.website.display} · Made in Saudi Arabia 🇸🇦
            </p>
          </div>

        </div>

        {/* Legal nav */}
        <div className="mt-6 flex flex-wrap justify-center gap-4 text-xs text-slate-400">
          <a href="/privacy"       className="hover:text-violet-600 transition-colors">Privacy Policy</a>
          <span>·</span>
          <a href="/data-deletion" className="hover:text-violet-600 transition-colors">Data Deletion</a>
          <span>·</span>
          <a href="/terms"         className="hover:text-violet-600 transition-colors">Terms of Service</a>
          <span>·</span>
          <a href="/contact"       className="font-semibold text-violet-600">Contact</a>
        </div>
      </div>
    </div>
  )
}
