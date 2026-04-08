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
import Coupons from './pages/Coupons'
import Campaigns from './pages/Campaigns'
import Templates from './pages/Templates'
import SmartAutomations from './pages/SmartAutomations'
import Intelligence from './pages/Intelligence'
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
import SallaOAuthSuccess from './pages/SallaOAuthSuccess'
import SallaOAuthError from './pages/SallaOAuthError'
import SallaCallback from './pages/SallaCallback'
import ZidCallback   from './pages/ZidCallback'
import Register from './pages/Register'
import WhatsAppConnect from './pages/WhatsAppConnect'
import WaUsage        from './pages/WaUsage'
import PrivacyPolicy  from './pages/PrivacyPolicy'
import VerifyEmail from './pages/VerifyEmail'
import ForgotPassword from './pages/ForgotPassword'
import ResetPassword from './pages/ResetPassword'
import MerchantAddons from './pages/MerchantAddons'

export default function App() {
  return (
    <LanguageProvider>
      <BrowserRouter>
        <Routes>
          {/* Public — marketing */}
          <Route path="/landing"              element={<Landing />} />
          <Route path="/privacy"              element={<PrivacyPolicy />} />

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
            <Route path="coupons"            element={<Coupons />} />
            <Route path="campaigns"          element={<Campaigns />} />
            <Route path="templates"          element={<Templates />} />
            <Route path="smart-automations"  element={<SmartAutomations />} />
            <Route path="automations"        element={<Navigate to="/smart-automations" replace />} />
            <Route path="intelligence"       element={<Intelligence />} />
            <Route path="integrations"       element={<Integrations />} />
            <Route path="analytics"          element={<Analytics />} />
            <Route path="settings"           element={<Settings />} />
            <Route path="ai-sales-logs"      element={<AiSalesLogs />} />
            <Route path="store-integration"  element={<StoreIntegration />} />
            <Route path="whatsapp-connect"   element={<WhatsAppConnect />} />
            <Route path="wa-usage"           element={<WaUsage />} />
            <Route path="handoff-queue"      element={<HandoffQueue />} />
            <Route path="system-status"      element={<SystemStatus />} />
            <Route path="merchants"          element={<Merchants />} />
            <Route path="admin"              element={<AdminDashboard />} />
            <Route path="admin/merchants"    element={<AdminMerchants />} />
            <Route path="admin/revenue"      element={<AdminRevenue />} />
            <Route path="admin/team"         element={<AdminTeam />} />
            <Route path="billing"            element={<Billing />} />
            <Route path="addons"             element={<MerchantAddons />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </LanguageProvider>
  )
}
