# Event catalog

Envelope (JSON, Kafka value):

```json
{"id": "uuid7", "type": "order.paid", "version": 1, "occurred_at": "…", "source": "order",
 "key": "<aggregate id, also the partition key>", "tenant_id": "…", "data": {…}}
```

Topic = `gj.<first segment of type>`; failed events land in `gj.<x>.dlq`. Money values in `data` are strings.

| Event | Producer | Consumers | Purpose |
|---|---|---|---|
| `identity.user.registered` | identity | legacy-bridge | Link the new account to a patient record in the existing system |
| `legacy.patient.linked` | legacy-bridge | identity | Store `legacy_patient_id` on the user |
| `tenant.created` | tenant | billing | Start subscription, issue setup + first-month invoice |
| `tenant.updated` / `tenant.active` / `tenant.suspended` / `tenant.admin_added` | tenant | — | Audit / future consumers |
| `tenant.network.member_upserted` / `_removed` | tenant | catalog, billing, legacy-bridge | Offer visibility, clinic-group commission, doctor filter cache |
| `partner.registered` | partner | tenant, catalog | Clinic-group-owned providers join that tenant's network |
| `partner.verified` / `rejected` / `suspended` / `updated` / `submitted` | partner | catalog, notification, legacy-bridge | Visibility, notices, doctor ↔ partner mapping |
| `catalog.stock.low` | catalog | notification | Partner inbox alert (best effort) |
| `order.placed` | order | — | Audit / analytics |
| `order.paid` | order | pharmacy, lab, legacy-bridge, billing, notification | Start fulfilment, book consult, accrue commission, confirm |
| `order.sub_order.status_changed` | order | pharmacy, lab, billing, notification | Mirror status, mark commission earned, patient updates |
| `order.sub_order.refunded` | order | billing | Reverse / claw back settlement |
| `order.completed` / `order.partially_completed` | order | legacy-bridge | Push into existing system's order history |
| `order.cancelled` / `order.expired` / `order.payment_failed` | order | notification | Patient notices |
| `order.cart.prescription_added` | order | notification | "Your cart is ready" |
| `payment.succeeded` / `payment.failed` | payment | order, billing, wholesale | By `purpose`: order, credit_topup, subscription, wholesale |
| `payment.refunded` | payment | — | Audit / reconciliation |
| `consult.prescription.issued` | legacy-bridge | order | Prescription → matched offers → patient cart |
| `lab.report.ready` | lab | legacy-bridge | Save report to the health folio |
| `wholesale.po.submitted` / `accepted` / `shipped` / `rejected` / `cancelled` | wholesale | notification | Buyer / stockist inboxes |
| `wholesale.po.received` | wholesale | catalog, billing | Restock buyer's offers, B2B wholesale fee |
| `billing.wallet.low_balance` | billing | billing (auto top-up), notification | Alert at 20 % |
| `billing.wallet.grace_started` / `paused` / `reactivated` | billing | notification | Credit lifecycle notices |
| `billing.topup.completed` / `billing.invoice.issued` | billing | notification | Receipts |
| `billing.payout.created` / `billing.payout.paid` | billing | notification | Payout notices |
