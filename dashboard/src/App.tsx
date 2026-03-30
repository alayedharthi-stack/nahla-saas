import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { LanguageProvider } from './i18n/context'
import Layout from './components/layout/Layout'
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

export default function App() {
  return (
    <LanguageProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Layout />}>
            <Route index element={<Navigate to="/overview" replace />} />
            <Route path="overview"       element={<Overview />} />
            <Route path="conversations"  element={<Conversations />} />
            <Route path="orders"         element={<Orders />} />
            <Route path="coupons"        element={<Coupons />} />
            <Route path="campaigns"      element={<Campaigns />} />
            <Route path="templates"      element={<Templates />} />
            <Route path="automations"    element={<SmartAutomations />} />
            <Route path="intelligence"   element={<Intelligence />} />
            <Route path="integrations"   element={<Integrations />} />
            <Route path="analytics"      element={<Analytics />} />
            <Route path="settings"       element={<Settings />} />
            <Route path="ai-sales-logs"  element={<AiSalesLogs />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </LanguageProvider>
  )
}
