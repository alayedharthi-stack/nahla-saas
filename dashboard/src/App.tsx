import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { LanguageProvider } from './i18n/context'
import Layout from './components/layout/Layout'
import ProtectedRoute from './components/ProtectedRoute'
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

export default function App() {
  return (
    <LanguageProvider>
      <BrowserRouter>
        <Routes>
          {/* Public */}
          <Route path="/login"                element={<Login />} />
          <Route path="/onboarding"           element={<Onboarding />} />
          <Route path="/billing/payment-result" element={<BillingResult />} />

          {/* Protected */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route index element={<Navigate to="/overview" replace />} />
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
            <Route path="handoff-queue"      element={<HandoffQueue />} />
            <Route path="system-status"      element={<SystemStatus />} />
            <Route path="merchants"          element={<Merchants />} />
            <Route path="billing"            element={<Billing />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </LanguageProvider>
  )
}
