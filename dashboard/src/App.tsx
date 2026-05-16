import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { LanguageProvider } from './i18n/context'
import Layout from './components/layout/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import { isPlatformOwner } from './auth'
import Landing from './pages/Landing'
import Login from './pages/Login'
import Onboarding from './pages/Onboarding'
import Billing from './pages/Billing'
import BillingResult from './pages/BillingResult'
import Overview from './pages/Overview'
import Conversations from './pages/Conversations'
import Orders from './pages/Orders'
import OrderDetail from './pages/OrderDetail'
import Coupons from './pages/Coupons'
import Promotions from './pages/Promotions'
import Campaigns from './pages/Campaigns'
import Templates from './pages/Templates'
import SmartAutomations from './pages/SmartAutomations'
import Intelligence from './pages/Intelligence'
import KnowledgeBase from './pages/KnowledgeBase'
import Integrations from './pages/Integrations'
import Analytics from './pages/Analytics'
import Settings from './pages/Settings'
import AiSalesLogs from './pages/AiSalesLogs'
import StoreIntegration from './pages/StoreIntegration'
import HandoffQueue from './pages/HandoffQueue'
import SystemStatus from './pages/SystemStatus'
import Merchants from './pages/Merchants'
import AdminDashboard from './pages/AdminDashboard'
import AdminMerchants from './pages/AdminMerchants'
import AdminRevenue from './pages/AdminRevenue'
import AdminTeam from './pages/AdminTeam'
import AdminTenants from './pages/AdminTenants'
import AdminAiUsage from './pages/AdminAiUsage'
import AdminFeatures from './pages/AdminFeatures'
import AdminTroubleshooting from './pages/AdminTroubleshooting'
import AdminCoexistence from './pages/AdminCoexistence'
import AdminSystemStatus from './pages/AdminSystemStatus'
import AdminTools from './pages/AdminTools'
import AdminWebhookHealth from './pages/AdminWebhookHealth'
import AdminTenantIntegrity from './pages/AdminTenantIntegrity'
import AdminSallaActivations from './pages/AdminSallaActivations'
import AdminSallaTokenStatus from './pages/AdminSallaTokenStatus'
import SallaOAuthSuccess from './pages/SallaOAuthSuccess'
import SallaOAuthError from './pages/SallaOAuthError'
import SallaCallback from './pages/SallaCallback'
import SallaEmbedded    from './pages/SallaEmbedded'
import SallaEntryScreen from './pages/SallaEntryScreen'
import SallaLaunch      from './pages/SallaLaunch'
import SallaPricing     from './pages/SallaPricing'
import SallaSetup       from './pages/SallaSetup'
import InviteFlow       from './pages/InviteFlow'
import ZidCallback      from './pages/ZidCallback'
import Register from './pages/Register'
import WhatsAppConnect from './pages/WhatsAppConnect'
import WaUsage        from './pages/WaUsage'
import DeliveryQuality from './pages/DeliveryQuality'
import PrivacyPolicy  from './pages/PrivacyPolicy'
import DataDeletion   from './pages/DataDeletion'
import Terms          from './pages/Terms'
import Contact        from './pages/Contact'
import VerifyEmail from './pages/VerifyEmail'
import ForgotPassword from './pages/ForgotPassword'
import ResetPassword from './pages/ResetPassword'
import MerchantWidgets from './pages/MerchantWidgets'
import Customers from './pages/Customers'
import CustomersImport from './pages/CustomersImport'
import WhatsAppManualSetup from './pages/WhatsAppManualSetup'
import WhatsAppCatalog from './pages/WhatsAppCatalog'
import AdminCatalog from './pages/AdminCatalog'
import ManualCouponCampaign from './pages/ManualCouponCampaign'

export default function App() {
  return (
    <LanguageProvider>
      <BrowserRouter>
        <Routes>
          {/* Root → landing page */}
          <Route index element={<Navigate to="/landing" replace />} />

          {/* Public — marketing */}
          <Route path="/landing"              element={<Landing />} />
          <Route path="/privacy"              element={<PrivacyPolicy />} />
          <Route path="/data-deletion"        element={<DataDeletion />} />
          <Route path="/terms"               element={<Terms />} />
          <Route path="/contact"             element={<Contact />} />

          {/* Public — auth */}
          <Route path="/login"                element={<Login />} />
          <Route path="/onboarding"           element={<Onboarding />} />
          <Route path="/billing/payment-result" element={<BillingResult />} />
          <Route path="/register"                   element={<Register />} />
          <Route path="/verify-email"               element={<VerifyEmail />} />
          <Route path="/forgot-password"            element={<ForgotPassword />} />
          <Route path="/reset-password"             element={<ResetPassword />} />
          <Route path="/integrations/salla/success" element={<SallaOAuthSuccess />} />
          <Route path="/integrations/salla/error"   element={<SallaOAuthError />} />
          <Route path="/salla-callback"             element={<SallaCallback />} />
          {/* Zero-Friction embedded entry — set /app/salla as the iframe URL in Salla partner portal */}
          <Route path="/app/salla"                  element={<SallaEmbedded />} />
          {/* Mini-dashboard — "استخدام التطبيق" inside Salla iframe (status + onboarding + metrics + CTAs) */}
          <Route path="/app/entry"                  element={<SallaEntryScreen />} />
          {/* Pricing page — no Navbar/Sidebar; CTAs open app.nahlah.ai/billing externally */}
          <Route path="/app/pricing"                element={<SallaPricing />} />
          {/* Quick Setup — shown to new Salla merchants before entry screen */}
          <Route path="/app/salla/setup"            element={<SallaSetup />} />
          {/* Auto-login landing — exchanges a short-lived token for a full session */}
          <Route path="/app/salla/launch"           element={<SallaLaunch />} />
          {/* Legacy entry kept for backwards compatibility */}
          <Route path="/salla"                      element={<SallaEmbedded />} />
          {/* Direct-invite onboarding (outside Salla) */}
          <Route path="/invite/:code"               element={<InviteFlow />} />
          <Route path="/zid-callback"               element={<ZidCallback />} />

          {/* Protected dashboard — all existing routes unchanged */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route index element={<Navigate to={isPlatformOwner() ? '/admin' : '/overview'} replace />} />
            <Route path="overview"           element={<Overview />} />
            <Route path="conversations"      element={<Conversations />} />
            <Route path="orders"             element={<Orders />} />
            <Route path="orders/:orderId"    element={<OrderDetail />} />
            <Route path="customers"          element={<Customers />} />
            <Route path="customers/import"   element={<CustomersImport />} />
            <Route path="coupons"            element={<Coupons />} />
            <Route path="promotions"         element={<Promotions />} />
            <Route path="campaigns"          element={<Campaigns />} />
            <Route path="campaigns/manual-coupon" element={<ManualCouponCampaign />} />
            <Route path="templates"          element={<Templates />} />
            <Route path="templates/manual-coupon" element={<ManualCouponCampaign />} />
            <Route path="smart-automations"  element={<SmartAutomations />} />
            <Route path="automations"        element={<Navigate to="/smart-automations" replace />} />
            <Route path="intelligence"       element={<Intelligence />} />
            <Route path="knowledge-base"     element={<KnowledgeBase />} />
            <Route path="integrations"       element={<Integrations />} />
            <Route path="analytics"          element={<Analytics />} />
            <Route path="settings"           element={<Settings />} />
            <Route path="ai-sales-logs"      element={<AiSalesLogs />} />
            <Route path="store-integration"  element={<StoreIntegration />} />
            <Route path="whatsapp-connect"   element={<WhatsAppConnect />} />
            {/* Product Catalog — first-class asset, channel-agnostic.
                  /catalog is the canonical path; /whatsapp-catalog is a
                  legacy alias so deep-linked bookmarks + the merchant
                  onboarding email keep working. Both render the same
                  component. */}
            <Route path="catalog"            element={<WhatsAppCatalog />} />
            <Route path="whatsapp-catalog"   element={<WhatsAppCatalog />} />
            <Route path="wa-usage"           element={<WaUsage />} />
            <Route path="delivery-quality"   element={<DeliveryQuality />} />
            <Route path="handoff-queue"      element={<HandoffQueue />} />
            <Route path="system-status"      element={<SystemStatus />} />
            <Route path="merchants"          element={<Merchants />} />
            <Route path="admin"              element={<AdminDashboard />} />
            <Route path="admin/tenants"      element={<AdminTenants />} />
            <Route path="admin/merchants"    element={<AdminMerchants />} />
            <Route path="admin/revenue"      element={<AdminRevenue />} />
            <Route path="admin/ai-usage"     element={<AdminAiUsage />} />
            <Route path="admin/features"     element={<AdminFeatures />} />
            <Route path="admin/troubleshooting" element={<AdminTroubleshooting />} />
            <Route path="admin/coexistence"  element={<AdminCoexistence />} />
            <Route path="admin/team"         element={<AdminTeam />} />
            <Route path="admin/system"       element={<AdminSystemStatus />} />
            <Route path="admin/tools"          element={<AdminTools />} />
            <Route path="admin/webhook-health"    element={<AdminWebhookHealth />} />
            <Route path="admin/tenant-integrity" element={<AdminTenantIntegrity />} />
            <Route path="admin/catalog"           element={<AdminCatalog />} />
            <Route path="admin/salla-activations" element={<AdminSallaActivations />} />
            <Route path="admin/salla/integrations/token-status" element={<AdminSallaTokenStatus />} />
            <Route path="admin/salla/diagnose/:tenantId" element={<AdminSallaTokenStatus />} />
            <Route path="billing"            element={<Billing />} />
            <Route path="widgets"            element={<MerchantWidgets />} />
            <Route path="help/whatsapp-manual-setup" element={<WhatsAppManualSetup />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </LanguageProvider>
  )
}
