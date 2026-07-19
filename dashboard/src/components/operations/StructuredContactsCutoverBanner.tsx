import { Link } from 'react-router-dom'
import { useLanguage } from '../../i18n/context'
import { NAVIGATION_PATHS } from '../../lib/navigationPolicy'

export function StructuredContactsCutoverBanner() {
  const { lang } = useLanguage()
  const copy = lang === 'ar'
    ? {
        prefix: 'تنبيه: أرقام الموظفين والتصعيد يجب إدارتها من',
        link: 'الفروع والتواصل والتصعيد',
        suffix: '، وليس من قاعدة المعرفة.',
      }
    : {
        prefix: 'Notice: staff numbers and escalation must be managed from',
        link: 'Branches, contacts, and escalation',
        suffix: ', not from the Knowledge Base.',
      }

  return (
    <p className="text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-1.5 leading-relaxed">
      {copy.prefix}{' '}
      <Link to={NAVIGATION_PATHS.structuredContacts} className="font-semibold underline">
        {copy.link}
      </Link>
      {copy.suffix}
    </p>
  )
}
