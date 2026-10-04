"""Tenant-scoped reconciliation that labels every amount by its evidence.

Nothing in this report is a balance, a receivable or a payable. Each figure
says where it came from:

* ``confirmed_gross`` — payments the provider confirmed (``observe_payment``).
* ``settled_gross`` — the part of that gross named by a provider settlement
  ``payment`` line. This is the only figure that may be shown as settled.
* ``awaiting_settlement_gross`` — confirmed but not yet named by any line.
* ``provider_reported_settlement_total`` — settlement totals as reported,
  including amounts whose lines were not fetched or did not match.
* ``unmatched_line_*`` — provider ``payment`` lines naming no observed payment.
  Fee, refund, chargeback and adjustment lines without a payment link are
  normal and only count toward ``settlement_line_total``.
* ``provisional_*`` — Nahlah AI's own fee quote snapshots; estimates only.

``evidence_state`` is ``inconsistent`` whenever the figures disagree with each
other, so a dashboard can refuse to render a number as final.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .models import (
    MerchantPaymentAllocation,
    MerchantPaymentSettlement,
    MerchantPaymentSettlementLine,
    MerchantPaymentTransaction,
)

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class PaymentSettlementState:
    transaction_id: int
    provider_payment_ref: str
    gross_amount: Decimal
    settlement_state: str  # awaiting_provider_settlement | provider_settled | adjusted_after_settlement
    settlement_reference: str | None


@dataclass(frozen=True)
class ReconciliationReport:
    tenant_id: int
    provider: str
    environment: str
    currency: str
    confirmed_payment_count: int
    confirmed_gross: Decimal
    settled_payment_count: int
    settled_gross: Decimal
    awaiting_settlement_gross: Decimal
    provider_reported_settlement_total: Decimal
    settlement_count: int
    settlement_line_total: Decimal
    unmatched_line_count: int
    unmatched_line_total: Decimal
    provisional_platform_fees: Decimal
    provisional_merchant_share: Decimal
    evidence_state: str  # none | partial | complete | inconsistent
    payments: Tuple[PaymentSettlementState, ...]


def _dec(value) -> Decimal:
    return Decimal(value if value is not None else 0).quantize(Decimal("0.01"))


def reconcile_merchant(
    engine: Engine, *, tenant_id: int, provider: str, environment: str, currency: str = "SAR"
) -> ReconciliationReport:
    if tenant_id <= 0 or not provider or environment not in ("test", "live"):
        raise ValueError("Invalid reconciliation scope")
    if len(currency) != 3 or not currency.isalpha() or currency != currency.upper():
        raise ValueError("Invalid currency")

    payments = MerchantPaymentTransaction.__table__
    allocations = MerchantPaymentAllocation.__table__
    settlements = MerchantPaymentSettlement.__table__
    lines = MerchantPaymentSettlementLine.__table__
    scope = lambda table: (  # noqa: E731
        table.c.tenant_id == tenant_id,
        table.c.provider == provider,
        table.c.environment == environment,
        table.c.currency == currency,
    )

    with engine.connect() as connection:
        confirmed = connection.execute(
            sa.select(payments.c.id, payments.c.provider_payment_ref, payments.c.gross_amount)
            .where(*scope(payments), payments.c.verification_state == "provider_confirmed")
            .order_by(payments.c.id)
        ).mappings().all()
        line_rows = connection.execute(
            sa.select(lines.c.transaction_id, lines.c.line_type, lines.c.amount, lines.c.provider_settlement_ref)
            .where(*scope(lines))
        ).mappings().all()
        settlement_row = connection.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(settlements.c.reported_amount), 0),
                sa.func.count(settlements.c.id),
            ).where(*scope(settlements))
        ).one()
        allocation_row = connection.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(allocations.c.platform_fee_amount), 0),
                sa.func.coalesce(sa.func.sum(allocations.c.merchant_gross_share), 0),
            ).select_from(
                allocations.join(
                    payments,
                    sa.and_(
                        allocations.c.tenant_id == payments.c.tenant_id,
                        allocations.c.transaction_id == payments.c.id,
                    ),
                )
            ).where(*scope(payments), payments.c.verification_state == "provider_confirmed")
        ).one()

    payment_lines: dict[int, list] = {}
    unmatched_count, unmatched_total, line_total = 0, ZERO, ZERO
    for row in line_rows:
        line_total += _dec(row["amount"])
        if row["transaction_id"] is not None:
            payment_lines.setdefault(row["transaction_id"], []).append(row)
        elif row["line_type"] == "payment":
            # A provider payment line naming no payment Nahlah AI observed: it
            # is stored as evidence but settles nothing until matched.
            unmatched_count += 1
            unmatched_total += _dec(row["amount"])

    states, settled_gross, settled_count, inconsistent = [], ZERO, 0, False
    for payment in confirmed:
        gross = _dec(payment["gross_amount"])
        linked = payment_lines.get(payment["id"], [])
        paid_lines = [row for row in linked if row["line_type"] == "payment"]
        adjustments = [row for row in linked if row["line_type"] in ("refund", "chargeback", "adjustment")]
        if not paid_lines:
            state, ref = "awaiting_provider_settlement", None
        else:
            if len(paid_lines) > 1 or _dec(paid_lines[0]["amount"]) != gross:
                inconsistent = True
            settled_gross += gross
            settled_count += 1
            state = "adjusted_after_settlement" if adjustments else "provider_settled"
            ref = paid_lines[0]["provider_settlement_ref"]
        states.append(PaymentSettlementState(
            payment["id"], payment["provider_payment_ref"], gross, state, ref,
        ))

    confirmed_gross = sum((_dec(p["gross_amount"]) for p in confirmed), ZERO)
    reported_total = _dec(settlement_row[0])
    if settled_gross > confirmed_gross or (line_rows and settlement_row[1] == 0):
        inconsistent = True
    if inconsistent:
        evidence = "inconsistent"
    elif not confirmed and not line_rows and settlement_row[1] == 0:
        evidence = "none"
    elif confirmed and settled_count == len(confirmed) and unmatched_count == 0:
        evidence = "complete"
    else:
        evidence = "partial"

    return ReconciliationReport(
        tenant_id=tenant_id, provider=provider, environment=environment, currency=currency,
        confirmed_payment_count=len(confirmed), confirmed_gross=confirmed_gross,
        settled_payment_count=settled_count, settled_gross=settled_gross,
        awaiting_settlement_gross=confirmed_gross - settled_gross,
        provider_reported_settlement_total=reported_total, settlement_count=int(settlement_row[1]),
        settlement_line_total=line_total, unmatched_line_count=unmatched_count,
        unmatched_line_total=unmatched_total,
        provisional_platform_fees=_dec(allocation_row[0]),
        provisional_merchant_share=_dec(allocation_row[1]),
        evidence_state=evidence, payments=tuple(states),
    )
