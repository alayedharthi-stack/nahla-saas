import { useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import Sidebar from './Sidebar'
import Header from './Header'
import TrialBanner from '../ui/TrialBanner'
import ImpersonationBanner from '../ui/ImpersonationBanner'
import { useLanguage } from '../../i18n/context'
import type { Translations } from '../../i18n/types'

type MetaSelector = (tr: Translations) => { title: string; subtitle: string }

const PAGE_META: Record<string, MetaSelector> = {
  '/overview':           tr => tr.pages.overview,
  '/conversations':      tr => tr.pages.conversations,
  '/orders':             tr => tr.pages.orders,
  '/customers':          _tr => ({ title: 'العملاء', subtitle: 'إدارة وتصنيف العملاء' }),
  '/coupons':            tr => tr.pages.coupons,
  '/campaigns':          tr => tr.pages.campaigns,
  '/integrations':       tr => tr.pages.integrations,
  '/analytics':          tr => tr.pages.analytics,
  '/settings':           tr => tr.pages.settings,
  '/smart-automations':  _tr => ({ title: 'الطيار الآلي', subtitle: 'إدارة الأتمتة الذكية' }),
  '/billing':            _tr => ({ title: 'الاشتراك والفوترة', subtitle: 'إدارة خطة نحلة' }),
  '/admin':              _tr => ({ title: 'لوحة المنصة', subtitle: 'نظرة عامة على أداء المنصة' }),
  '/admin/tenants':      _tr => ({ title: 'المتاجر', subtitle: 'إدارة الـ tenants وحالتها' }),
  '/admin/merchants':    _tr => ({ title: 'وصول التجار', subtitle: 'إدارة الوصول والدعم والتمثيل' }),
  '/admin/revenue':      _tr => ({ title: 'الإيرادات', subtitle: 'ملخص مالي على مستوى المنصة' }),
  '/admin/ai-usage':     _tr => ({ title: 'استخدام AI', subtitle: 'تكلفة ومزودات الاستخدام التقديرية' }),
  '/admin/features':     _tr => ({ title: 'Feature Flags', subtitle: 'تحكم مركزي في خصائص المنصة' }),
  '/admin/troubleshooting': _tr => ({ title: 'Troubleshooting', subtitle: 'فحص سريع لمشكلات المتاجر' }),
  '/admin/team':         _tr => ({ title: 'الفريق', subtitle: 'سياسة الأدوار التشغيلية للمنصة' }),
  '/admin/system':       _tr => ({ title: 'حالة النظام', subtitle: 'الصحة العامة والتبعيات والأحداث' }),
}

export default function Layout() {
  const { pathname } = useLocation()
  const { t } = useLanguage()
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const metaSelector = PAGE_META[pathname] ?? (() => ({ title: 'نحلة', subtitle: '' }))
  const meta = t(metaSelector)

  return (
    <div className="min-h-dvh flex bg-slate-50 overflow-x-hidden">
      <Sidebar isOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      {/*
       * ms-0 on mobile (sidebar overlays as a drawer).
       * ms-60 on lg+ (sidebar is always visible and takes up 240 px).
       */}
      <div className="flex-1 ms-0 lg:ms-60 flex flex-col min-h-dvh overflow-x-hidden">
        <Header
          title={meta.title}
          subtitle={meta.subtitle}
          onMenuClick={() => setSidebarOpen(o => !o)}
        />
        <ImpersonationBanner />
        <TrialBanner />
        <main className="flex-1 p-3 md:p-6 overflow-x-auto">
          <Outlet />
        </main>
        {/* iOS home-bar safe area */}
        <div className="pb-safe-bottom" />
      </div>
    </div>
  )
}
