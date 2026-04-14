import LegalFooter from '../components/LegalFooter'

/**
 * PrivacyPolicy.tsx
 * Public page — no auth required
 * Route: /privacy
 * Submitted to: Meta Developers → App Settings → Basic → Privacy Policy URL
 */
export default function PrivacyPolicy() {
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
            <h1 className="text-2xl font-black text-slate-800">Privacy Policy</h1>
            <p className="text-sm text-slate-500">Nahlah AI — nahlah.ai</p>
          </div>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-8 space-y-7 text-slate-700 leading-relaxed text-sm">

          {/* Introduction */}
          <section>
            <p className="text-base">
              Nahlah AI ("<strong>we</strong>", "<strong>our</strong>", or
              "<strong>us</strong>") is an AI-powered sales assistant platform
              that helps merchants automate customer conversations, manage
              orders, and run marketing campaigns through WhatsApp Business
              Platform. This Privacy Policy explains how we collect, use,
              store, and protect your personal data when you use our services.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Data We Collect */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">1. Data We Collect</h2>
            <p className="mb-2">
              We collect the following categories of personal data to operate
              our service:
            </p>
            <ul className="list-disc list-inside space-y-1.5 text-slate-600">
              <li>
                <span className="font-medium text-slate-800">WhatsApp phone number</span>
                {' '}— the phone number linked to your WhatsApp Business account
                or used to interact with a merchant's WhatsApp chatbot
              </li>
              <li>
                <span className="font-medium text-slate-800">Customer name</span>
                {' '}— provided voluntarily during conversation or synced from
                the merchant's store
              </li>
              <li>
                <span className="font-medium text-slate-800">Conversation messages</span>
                {' '}— messages exchanged via WhatsApp that are processed by
                the AI assistant to generate responses
              </li>
              <li>
                <span className="font-medium text-slate-800">Order and purchase history</span>
                {' '}— synced from the merchant's Salla or Zid store to enable
                personalized responses and automation
              </li>
              <li>
                <span className="font-medium text-slate-800">Merchant store data</span>
                {' '}— store name, product catalog, coupons, and customer
                segments linked to the merchant's account
              </li>
              <li>
                <span className="font-medium text-slate-800">Account credentials</span>
                {' '}— email address and hashed password for merchant dashboard
                login
              </li>
            </ul>
          </section>

          <hr className="border-slate-100" />

          {/* How We Use Your Data */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">2. How We Use Your Data</h2>
            <p className="mb-2">We use collected data solely to:</p>
            <ul className="list-disc list-inside space-y-1.5 text-slate-600">
              <li>Operate the AI-powered WhatsApp chatbot and respond to customer messages</li>
              <li>Sync products, orders, and customers from connected stores</li>
              <li>Generate and send marketing campaigns and discount coupons</li>
              <li>Provide merchants with analytics and customer segmentation insights</li>
              <li>Improve the accuracy and quality of AI responses</li>
              <li>Send service notifications and billing communications to merchants</li>
            </ul>
            <p className="mt-2 text-slate-500">
              We do <strong className="text-slate-800">not</strong> use your
              data for advertising, sell it to third parties, or share it
              beyond what is necessary to operate the service.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Data Storage & Security */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">3. Data Storage &amp; Security</h2>
            <p className="mb-2">
              All data is stored on secure cloud infrastructure (Railway /
              PostgreSQL) with encryption at rest and in transit (TLS).
              Merchant passwords are stored as bcrypt hashes and are never
              accessible in plaintext.
            </p>
            <p>
              We retain conversation and order data for as long as the merchant
              account is active. Upon account deletion or a formal data deletion
              request, all associated personal data is permanently removed
              within 30 days.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Third-Party Services */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">4. Third-Party Services</h2>
            <p className="mb-3">
              Nahlah AI integrates with the following third-party services to
              deliver its features:
            </p>
            <div className="space-y-2">
              {[
                {
                  name: 'Meta — WhatsApp Cloud API',
                  desc: 'Used to send and receive WhatsApp messages on behalf of merchants. Meta\'s data handling is governed by Meta\'s own Privacy Policy.',
                },
                {
                  name: 'Salla',
                  desc: 'E-commerce platform integration to sync products, orders, and customers.',
                },
                {
                  name: 'Zid',
                  desc: 'E-commerce platform integration to sync products, orders, and customers.',
                },
                {
                  name: 'OpenAI / AI Providers',
                  desc: 'Used to generate AI responses. Conversation context may be sent to the AI provider to produce a reply.',
                },
              ].map(({ name, desc }) => (
                <div key={name} className="bg-slate-50 rounded-xl border border-slate-100 px-4 py-3">
                  <p className="font-semibold text-slate-800">{name}</p>
                  <p className="text-slate-500 mt-0.5">{desc}</p>
                </div>
              ))}
            </div>
          </section>

          <hr className="border-slate-100" />

          {/* WhatsApp Cloud API */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">5. Use of WhatsApp Cloud API</h2>
            <p>
              Nahlah AI uses the <strong>Meta WhatsApp Cloud API</strong> (part
              of the WhatsApp Business Platform) to send automated messages,
              respond to customer inquiries, and deliver marketing campaigns on
              behalf of merchants. By connecting their WhatsApp Business number
              to Nahlah AI, merchants authorize us to send and receive messages
              through their account in accordance with{' '}
              <a
                href="https://www.whatsapp.com/legal/business-policy"
                target="_blank"
                rel="noreferrer"
                className="text-violet-600 hover:underline"
              >
                WhatsApp Business Policy
              </a>.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Data Retention */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">6. Data Retention</h2>
            <p>
              We retain personal data for as long as necessary to provide the
              service. Merchants may delete their account at any time. Customer
              conversation data is retained for up to 12 months after the last
              interaction unless a deletion request is made earlier.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Your Rights */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">7. Your Rights &amp; Data Deletion</h2>
            <p className="mb-2">
              You have the right to request access to, correction of, or
              deletion of your personal data at any time. To submit a data
              deletion request:
            </p>
            <a
              href="/data-deletion"
              className="inline-block px-4 py-2.5 bg-violet-600 text-white text-sm font-semibold rounded-xl hover:bg-violet-500 transition-colors"
            >
              Data Deletion Instructions →
            </a>
            <p className="mt-3 text-slate-500">
              All deletion requests are processed within{' '}
              <strong className="text-slate-800">30 days</strong> of receipt.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Children */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">8. Children's Privacy</h2>
            <p>
              Nahlah AI is not directed to children under the age of 13. We do
              not knowingly collect personal information from children. If you
              believe a child has provided us with personal data, please contact
              us immediately.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Changes */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">9. Changes to This Policy</h2>
            <p>
              We may update this Privacy Policy from time to time. Significant
              changes will be communicated to merchants via email or in-app
              notification. Continued use of the service after changes take
              effect constitutes acceptance of the updated policy.
            </p>
          </section>

          <hr className="border-slate-100" />

          {/* Contact */}
          <section>
            <h2 className="text-base font-bold text-slate-900 mb-2">10. Contact Us</h2>
            <p>
              If you have any questions about this Privacy Policy or how we
              handle your data, please contact us at:
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
        <div className="mt-6">
          <LegalFooter variant="light" />
        </div>
      </div>
    </div>
  )
}
