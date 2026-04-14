/**
 * Terms.tsx
 * Public page — no auth required
 * Route: /terms
 * Required by Meta for WhatsApp Business Platform app review.
 */
export default function Terms() {
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
            <h1 className="text-2xl font-black text-slate-800">Terms of Service</h1>
            <p className="text-sm text-slate-500">Nahlah AI — nahlah.ai</p>
          </div>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-8 space-y-7 text-slate-700 leading-relaxed text-sm">

          <section>
            <p className="text-base">
              These Terms of Service govern the use of the{' '}
              <strong>Nahlah AI</strong> platform. By accessing or using our
              platform, you agree to comply with these terms in full.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Service Description */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">1. Service Description</h2>
            <p className="mb-2">
              Nahlah AI is a software platform that helps online stores
              automate customer communication and sales through WhatsApp and
              AI-powered tools. The platform allows merchants to:
            </p>
            <ul className="list-disc list-inside space-y-1.5 text-slate-600">
              <li>Manage customer conversations via WhatsApp</li>
              <li>Automate AI-powered responses</li>
              <li>Recover abandoned carts and follow up with customers</li>
              <li>Send targeted marketing campaigns</li>
              <li>Generate checkout links and discount offers</li>
            </ul>
          </section>

          <hr className="border-slate-100" />

          {/* Acceptable Use */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">2. Acceptable Use</h2>
            <p className="mb-2">Users agree <strong>not</strong> to use the platform for:</p>
            <ul className="list-disc list-inside space-y-1.5 text-slate-600">
              <li>Sending unsolicited bulk or spam messages</li>
              <li>Harassment, abuse, or threatening communication</li>
              <li>Any illegal activities or fraudulent behavior</li>
              <li>Violating WhatsApp Business Platform policies or Meta's terms</li>
              <li>Impersonating any person or organization</li>
            </ul>
            <p className="mt-3 text-slate-600">
              Nahlah AI reserves the right to suspend or terminate accounts
              that violate these rules without prior notice.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Data Usage */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">3. Data Usage</h2>
            <p>
              Nahlah AI processes customer messages and phone numbers solely
              for the purpose of delivering the service to merchants. We do
              not sell, share, or distribute customer data to third parties
              outside the scope of operating the platform. For full details,
              please refer to our{' '}
              <a href="/privacy" className="text-violet-600 hover:underline">
                Privacy Policy
              </a>.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Merchant Responsibilities */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">4. Merchant Responsibilities</h2>
            <p className="mb-2">Merchants using Nahlah AI are responsible for:</p>
            <ul className="list-disc list-inside space-y-1.5 text-slate-600">
              <li>Obtaining proper customer consent before sending marketing messages</li>
              <li>Complying with WhatsApp Business Policy and Meta's terms</li>
              <li>Maintaining the security of their account credentials</li>
              <li>Ensuring all communications comply with applicable local laws</li>
            </ul>
          </section>

          <hr className="border-slate-100" />

          {/* Service Availability */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">5. Service Availability</h2>
            <p>
              While we aim to maintain continuous service availability,
              temporary interruptions may occur due to maintenance, updates,
              or issues with external service providers (including Meta,
              WhatsApp, or cloud infrastructure). We will make reasonable
              efforts to notify users of planned downtime in advance.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Limitation of Liability */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">6. Limitation of Liability</h2>
            <p>
              Nahlah AI is provided "<strong>as is</strong>" without warranties
              of any kind, express or implied. We are not responsible for
              damages resulting from service interruptions, data loss, or
              issues arising from third-party platforms including Meta,
              WhatsApp, Salla, or Zid.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Changes to Terms */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">7. Changes to These Terms</h2>
            <p>
              We reserve the right to update these Terms of Service at any
              time. Changes will be communicated via email or in-app
              notification. Continued use of the platform after updates
              constitutes acceptance of the revised terms.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Contact */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">8. Contact</h2>
            <p>
              For questions regarding these Terms of Service, please contact:
            </p>
            <a
              href="mailto:support@nahlah.ai"
              className="inline-block mt-3 px-4 py-2.5 bg-slate-800 text-white text-sm font-semibold rounded-xl hover:bg-slate-700 transition-colors"
            >
              support@nahlah.ai
            </a>
          </section>

          {/* Footer */}
          <div className="pt-2 border-t border-slate-100">
            <p className="text-xs text-slate-400">
              Last updated: April 2026 · Nahlah AI · nahlah.ai
            </p>
          </div>

        </div>

        {/* Legal nav */}
        <div className="mt-6 flex flex-wrap justify-center gap-4 text-xs text-slate-400">
          <a href="/privacy"       className="hover:text-violet-600 transition-colors">Privacy Policy</a>
          <span>·</span>
          <a href="/data-deletion" className="hover:text-violet-600 transition-colors">Data Deletion</a>
          <span>·</span>
          <a href="/terms"         className="font-semibold text-violet-600">Terms of Service</a>
          <span>·</span>
          <a href="/contact"       className="hover:text-violet-600 transition-colors">Contact</a>
        </div>
      </div>
    </div>
  )
}
