import type { CatalogProductDiagRow } from '../../api/catalog'
import { useLanguage } from '../../i18n/context'

function fmtProductPrice(
  price: string | null,
  currency: string | null | undefined,
): string {
  if (!price) return '—'
  const amount = formatCatalogPriceAmount(price)
  if (!amount) return '—'
  const c = currency?.trim()
  return c ? `${amount} ${c}` : amount
}

function formatCatalogPriceAmount(value: string | null | undefined): string {
  if (!value) return ''
  const n = Number(value)
  if (!Number.isFinite(n)) return ''
  if (Number.isInteger(n)) return String(n)
  return String(parseFloat(n.toFixed(2)))
}

export function CatalogProductPriceCell({ row }: { row: CatalogProductDiagRow }) {
  const { tStatic } = useLanguage()
  const discountedBadge = tStatic(tr => tr.catalogMgmt.importedProducts.discountedPriceBadge)
  const sale = formatCatalogPriceAmount(row.sale_price)
  const regular = formatCatalogPriceAmount(row.regular_price)
  if (
    row.is_on_sale
    && sale
    && regular
    && sale !== regular
  ) {
    const suffix = row.currency?.trim() ? ` ${row.currency.trim()}` : ' ريال'
    return (
      <div className="flex flex-col gap-1 min-w-[7rem]">
        <span className="text-[10px] font-semibold text-orange-700 bg-orange-50 border border-orange-200 rounded px-1.5 py-0.5 w-fit shrink-0">
          {discountedBadge}
        </span>
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="text-slate-400 text-sm line-through whitespace-nowrap">
            {regular}{suffix}
          </span>
          <span className="font-semibold text-slate-900 text-sm whitespace-nowrap">
            {sale}{suffix}
          </span>
        </div>
      </div>
    )
  }
  return <span className="whitespace-nowrap">{fmtProductPrice(row.price, row.currency)}</span>
}
