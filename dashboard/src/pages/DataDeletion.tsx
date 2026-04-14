/**
 * DataDeletion.tsx
 * Public page — no auth required
 * Route: /data-deletion
 * Required by Meta for WhatsApp Business Platform app review.
 * URL submitted in: Meta Developers → App Settings → Data Deletion Instructions URL
 */
export default function DataDeletion() {
  return (
    <div className="min-h-screen bg-slate-50 py-12 px-4" dir="ltr">
      <div className="max-w-2xl mx-auto">

        {/* Header */}
        <div className="flex items-center gap-3 mb-8">
          <img
            src="/logo.png"
            alt="Nahlah AI"
            className="w-10 h-10 rounded-xl"
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <div>
            <h1 className="text-2xl font-black text-slate-800">Data Deletion Instructions</h1>
            <p className="text-sm text-slate-500">Nahlah AI — nahlah.ai</p>
          </div>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-8 space-y-7 text-slate-700 leading-relaxed text-sm">

          <section>
            <p className="text-base">
              Nahlah AI respects the privacy of all users and is committed to
              protecting personal data in compliance with applicable privacy laws
              and the Meta Platform Policies.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* What data we collect */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">What Data We Collect</h2>
            <p>
              When merchants or their customers interact with Nahlah AI through
              WhatsApp Business Platform, we may process the following data:
            </p>
            <ul className="list-disc list-inside mt-2 space-y-1 text-slate-600">
              <li>WhatsApp phone numbers</li>
              <li>Customer names (if provided in conversation)</li>
              <li>Order and purchase history (synced from the merchant's store)</li>
              <li>Conversation messages exchanged via WhatsApp</li>
              <li>Coupon and campaign interaction data</li>
            </ul>
          </section>

          <hr className="border-slate-100" />

          {/* How to request deletion */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">How to Request Data Deletion</h2>
            <p>
              You can request deletion of your personal data associated with
              your use of Nahlah AI or the WhatsApp integration at any time by
              sending an email to:
            </p>
            <a
              href="mailto:support@nahlah.ai"
              className="inline-block mt-3 px-4 py-2.5 bg-violet-600 text-white text-sm font-semibold rounded-xl hover:bg-violet-500 transition-colors"
            >
              support@nahlah.ai
            </a>
          </section>

          <hr className="border-slate-100" />

          {/* What to include */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">What to Include in Your Request</h2>
            <p>To help us locate and delete your data efficiently, please include:</p>
            <ul className="list-disc list-inside mt-2 space-y-1 text-slate-600">
              <li>
                <span className="font-medium text-slate-800">WhatsApp phone number</span>
                {' '}— the phone number linked to your WhatsApp account
              </li>
              <li>
                <span className="font-medium text-slate-800">Store name</span>
                {' '}— the name of the merchant's store (if applicable)
              </li>
              <li>
                <span className="font-medium text-slate-800">Request description</span>
                {' '}— a brief description of what data you want deleted
              </li>
            </ul>
          </section>

          <hr className="border-slate-100" />

          {/* Processing time */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">Processing Time</h2>
            <p>
              We will process all data deletion requests within{' '}
              <span className="font-semibold text-slate-900">30 days</span> of
              receipt. Once processed, you will receive a confirmation email at
              the address from which the request was sent.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Contact */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">Contact Us</h2>
            <p>
              If you have any questions about this policy or your data, please
              contact our support team at{' '}
              <a
                href="mailto:support@nahlah.ai"
                className="text-violet-600 underline underline-offset-2 hover:text-violet-500"
              >
                support@nahlah.ai
              </a>.
            </p>
          </section>

          {/* Footer note */}
          <div className="pt-2 border-t border-slate-100">
            <p className="text-xs text-slate-400">
              Last updated: April 2026 · Nahlah AI · nahlah.ai
            </p>
          </div>

        </div>
      </div>
    </div>
  )
}
