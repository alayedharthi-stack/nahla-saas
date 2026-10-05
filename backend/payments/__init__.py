"""Dormant, provider-neutral merchant payments foundation for Nahlah AI.

No module here is mounted into the live checkout, WhatsApp, billing or webhook
paths, and none opens a network connection. Activation requires a separately
reviewed integration and a signed provider agreement.

Modules:

* ``models`` — tenant-bound financial tables (0115 foundation, 0119 readiness)
* ``fees`` / ``observations`` — provisional fee quotes over provider-confirmed payments
* ``provider`` — dormant Platform API, Payments and Payout boundaries (fail closed)
* ``onboarding`` / ``activation`` / ``secret_refs`` — merchant state and activation gate
* ``webhook_ledger`` — durable, deduplicated, redacted inbound delivery ledger
* ``settlements`` / ``reconciliation`` — provider settlement evidence and labelled reads
"""
