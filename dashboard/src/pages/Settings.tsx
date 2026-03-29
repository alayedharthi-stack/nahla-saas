import { useState } from 'react'
import { Save, Bot, CreditCard, Store, ToggleLeft, ToggleRight, CheckCircle, Crown } from 'lucide-react'
import Badge from '../components/ui/Badge'

function Section({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="card">
      <div className="px-5 py-4 border-b border-slate-100">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {description && <p className="text-xs text-slate-400 mt-0.5">{description}</p>}
      </div>
      <div className="p-5">{children}</div>
    </div>
  )
}

function Toggle({ label, hint, defaultOn = false }: { label: string; hint?: string; defaultOn?: boolean }) {
  const [on, setOn] = useState(defaultOn)
  return (
    <div className="flex items-start justify-between py-3 border-b border-slate-50 last:border-0">
      <div>
        <p className="text-sm text-slate-800">{label}</p>
        {hint && <p className="text-xs text-slate-400 mt-0.5">{hint}</p>}
      </div>
      <button onClick={() => setOn(!on)} className="ms-4 shrink-0">
        {on
          ? <ToggleRight className="w-6 h-6 text-brand-500" />
          : <ToggleLeft  className="w-6 h-6 text-slate-300" />}
      </button>
    </div>
  )
}

export default function Settings() {
  const [saved, setSaved] = useState(false)

  const handleSave = () => {
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  return (
    <div className="space-y-5 max-w-3xl">
      {/* Store Settings */}
      <Section title="Store Settings" description="Basic information about your store">
        <div className="grid sm:grid-cols-2 gap-4">
          <div>
            <label className="label">Store Name</label>
            <input className="input" defaultValue="متجر أحمد للملابس" />
          </div>
          <div>
            <label className="label">Store Domain</label>
            <input className="input" defaultValue="ahmed-clothing.salla.sa" />
          </div>
          <div>
            <label className="label">Contact Email</label>
            <input className="input" type="email" defaultValue="hello@ahmed-clothing.com" />
          </div>
          <div>
            <label className="label">WhatsApp Number</label>
            <input className="input" defaultValue="+966 50 123 4567" />
          </div>
          <div className="sm:col-span-2">
            <label className="label">Store Address</label>
            <input className="input" defaultValue="Riyadh, Al Olaya District, King Fahd Road" />
          </div>
          <div>
            <label className="label">Same-Day Delivery</label>
            <select className="input">
              <option>Enabled</option>
              <option>Disabled</option>
            </select>
          </div>
          <div>
            <label className="label">Pickup Orders</label>
            <select className="input">
              <option>Enabled</option>
              <option>Disabled</option>
            </select>
          </div>
        </div>

        <div className="mt-5 flex items-center gap-3">
          <button onClick={handleSave} className="btn-primary text-sm">
            <Save className="w-4 h-4" />
            {saved ? 'Saved!' : 'Save Changes'}
          </button>
          {saved && <CheckCircle className="w-4 h-4 text-emerald-500" />}
        </div>
      </Section>

      {/* AI Permissions */}
      <Section
        title="AI Permissions"
        description="Control what Nahla AI is allowed to do on behalf of your store"
      >
        <div className="space-y-0">
          <Toggle label="Enable AI responses"           hint="AI answers customer messages automatically"                                        defaultOn={true}  />
          <Toggle label="Allow AI to create orders"     hint="AI can initiate orders and send payment links"                                     defaultOn={true}  />
          <Toggle label="Allow AI to apply coupons"     hint="AI can send discount codes during conversations"                                   defaultOn={true}  />
          <Toggle label="Allow AI to recommend products"hint="AI can suggest products based on conversation context"                             defaultOn={true}  />
          <Toggle label="Allow AI to share price info"  hint="AI can disclose product prices and totals"                                         defaultOn={true}  />
          <Toggle label="Allow AI to book delivery"     hint="AI can confirm delivery slots with customers"                                      defaultOn={false} />
          <Toggle label="Block competitor mentions"     hint="AI will avoid mentioning or comparing with competing stores"                       defaultOn={true}  />
          <Toggle label="Human escalation on complaint" hint="AI will hand off to a human agent when a complaint or urgent issue is detected"    defaultOn={true}  />
        </div>

        <div className="mt-4 p-3 bg-amber-50 rounded-lg border border-amber-200">
          <p className="text-xs text-amber-700 flex items-start gap-2">
            <Bot className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            Changes to AI permissions take effect immediately for new conversations. Ongoing conversations are not affected.
          </p>
        </div>
      </Section>

      {/* Branding */}
      <Section title="Branding" description="Customize how Nahla appears in your store">
        <Toggle
          label='Show "Powered by Nahla 🐝" in messages'
          hint="Displayed at the bottom of AI-generated messages"
          defaultOn={true}
        />
        <div className="mt-4">
          <label className="label">Custom branding text</label>
          <input className="input text-sm" defaultValue="🐝 Powered by Nahla" />
          <p className="text-xs text-slate-400 mt-1">Available on Growth plan and above</p>
        </div>
      </Section>

      {/* Billing */}
      <Section title="Billing & Plan" description="Your current subscription and usage">
        <div className="flex items-start gap-4">
          <div className="w-10 h-10 bg-brand-50 rounded-xl flex items-center justify-center shrink-0">
            <Crown className="w-5 h-5 text-brand-500" />
          </div>
          <div className="flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-sm font-semibold text-slate-900">Growth Plan</h3>
              <Badge label="Active" variant="green" dot />
            </div>
            <p className="text-xs text-slate-500 mt-0.5">SAR 899 / month · renews May 1, 2026</p>

            <div className="grid sm:grid-cols-3 gap-3 mt-4">
              {[
                { label: 'Conversations',  used: 4890, limit: 10000 },
                { label: 'Orders via AI',  used: 1248, limit: 5000 },
                { label: 'Campaigns sent', used: 3427, limit: 10000 },
              ].map(({ label, used, limit }) => (
                <div key={label} className="bg-slate-50 rounded-lg p-3">
                  <p className="text-xs text-slate-400">{label}</p>
                  <p className="text-sm font-semibold text-slate-900 mt-0.5">{used.toLocaleString()}</p>
                  <div className="flex-1 bg-slate-200 rounded-full h-1 mt-2">
                    <div
                      className="bg-brand-500 h-1 rounded-full"
                      style={{ width: `${Math.min((used / limit) * 100, 100)}%` }}
                    />
                  </div>
                  <p className="text-xs text-slate-400 mt-1">of {limit.toLocaleString()}</p>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3 mt-5 pt-5 border-t border-slate-100">
          <button className="btn-primary text-sm">
            <CreditCard className="w-4 h-4" /> Upgrade to Pro
          </button>
          <button className="btn-secondary text-sm">
            <Store className="w-4 h-4" /> Manage Billing
          </button>
        </div>
      </Section>
    </div>
  )
}
