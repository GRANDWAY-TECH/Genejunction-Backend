# GeneJunction Marketplace — backend architecture

One engine, three front doors: **B2C** (GeneJunction app & web), **B2B** (partner portals, business
console) and **B2B2C** (white-label apps for clinic groups). Consultations, doctors, health folio and
patient records stay in the existing PHP system and are reached **only through its APIs**.

## Request path

```
Flutter app / React web / React portals
        │  HTTPS  (Host = genejunction.com | care.sejahtera.com.my, or X-Tenant: <slug> from mobile)
        ▼
CloudFront + WAF ──► API Gateway / ALB ──► (local: nginx gateway :8080)
        │  /api/v1/<resource>  →  owning service          /internal/*  → never exposed
        ▼
12 FastAPI services on ECS Fargate (each horizontally scaled, stateless)
        │
        ├── own PostgreSQL database per service (Aurora PostgreSQL in prod)
        ├── MongoDB (catalog only — DocumentDB/Atlas in prod)
        ├── Redis (ElastiCache): OTP, caches, job locks, rate limits
        ├── Kafka (MSK; Redpanda locally): domain events
        └── S3: KYC documents, prescriptions, lab reports (presigned URLs only)
```

Every service resolves the **tenant** (brand) from the host or `X-Tenant` header via the tenant
service (cached in-process 30 s + Redis 5 min). The tenant decides the **channel**: the platform tenant
is `B2C`, white-label tenants are `B2B2C`; stockist purchase orders are `B2B`.

## Services

| Service | Port (local) | Store | Owns |
|---|---|---|---|
| identity | 8101 | Postgres | Users (one global identity), phone OTP, email/password for business users, RS256 JWT + JWKS, refresh-token rotation with reuse detection, roles (platform / tenant / partner scoped) |
| tenant | 8102 | Postgres | Tenants, branding, custom domains, provider network, "list on marketplace" toggle, host → tenant resolution |
| partner | 8103 | Postgres | Pharmacy / lab / stockist / doctor / clinic-group onboarding, KYC docs (S3), Fernet-encrypted bank details, ops verification |
| catalog | 8104 | MongoDB | Master products, partner offers (retail + wholesale tiers), tenant-aware search, stock reservations, prescription → offer matching, featured listings |
| order | 8105 | Postgres | Cart, checkout saga, order split into per-partner sub-orders, state machines, refunds of rejected parts, prescriptions, partner order inbox, unified history |
| payment | 8106 | Postgres | Payment intents (FPX / card / e-wallet), gateway adapters, signed webhooks, idempotent refunds |
| billing | 8107 | Postgres | Commission rules & settlement, credit wallets / ledger / rate card / packs, payouts, subscriptions, SST invoices |
| pharmacy | 8108 | Postgres | Fulfilment: accept, pharmacist Rx verification, pack, courier dispatch, delivery |
| lab | 8109 | Postgres | Collection slots & capacity, sample pickup, report upload, AI summaries (paid in credits) |
| wholesale | 8110 | Postgres | Stockist purchase orders: tier pricing, prepaid / net-30, ship → receive → restock |
| notification | 8111 | Postgres | Branded SMS / WhatsApp / email / push, business inboxes, patient campaigns |
| legacy-bridge | 8112 | Postgres | The only door to the PHP system: doctors & slots (network-filtered), slot holds, booking after payment, prescription webhooks, health folio, patient linking, order-history sync |

Each service has Swagger UI at `http://localhost:81NN/docs`.

## Why these stores

* **PostgreSQL per service** for anything with money or state machines: ACID, row locks
  (`SELECT … FOR UPDATE`, `SKIP LOCKED`), partial indexes. Aurora gives read replicas and
  storage auto-scaling; put RDS Proxy / PgBouncer in front for connection fan-in.
* **MongoDB for the catalog**: flexible product attributes (medicines vs lab tests vs devices),
  text + geo indexes, and visibility denormalised onto every offer so search is one indexed query.
* **Kafka** for events: partitioned by aggregate id (ordering per order / partner), consumer groups
  per service, replayable. One topic per bounded context (`gj.order`, `gj.payment`, …) plus `.dlq`.
* **Redis** for hot, disposable state only.

## Reliability patterns

* **Transactional outbox** (`gj_common.events.emit`): events are written in the same DB transaction
  as the state change and relayed to Kafka by `OutboxRelay` (safe on many replicas via `SKIP LOCKED`).
* **Idempotent consumers**: every handler runs in one transaction with a `processed_events` insert, so
  redelivery is harmless. Failing events retry with backoff, then go to `<topic>.dlq`.
* **Checkout saga** (order service): create order → reserve stock (catalog, all-or-nothing) → hold
  consult slot (bridge) → open payment. Any failure compensates (release stock / holds, cancel order).
  `Idempotency-Key` header makes retries return the same order.
* **Payments**: idempotency keys, webhook dedupe table, refunds idempotent on `reference`.
  Late payment on an expired order is auto-refunded.
* **Money**: `Decimal` everywhere, `NUMERIC(14,2)` in Postgres, `Decimal128` in Mongo, serialised as
  strings in JSON and events. Never floats.
* **Time-ordered UUIDv7 keys** keep B-tree inserts append-only at high write volume.
* **Single-runner jobs** (`PeriodicJob`): Redis lock so a job runs on one replica (payouts, credit
  expiry, grace → pause, subscription renewals, reservation / payment / order expiry).

## Money flows

| Stream | Where | Rule |
|---|---|---|
| Commission (B2C) | billing `settlement` | Most specific rule wins: partner > tenant > partner type > default. Seeds: 10 % default, 15 % lab, 20 % doctor |
| Per-order fee (B2B2C) | billing | The white-label plan's `transaction_fee_pct` (2 % standard) unless a partner-specific rule exists; the clinic group's own commission on network members is kept separately and paid out to it |
| Wholesale fee (B2B) | billing | 3 % default on received purchase orders |
| Subscriptions & setup | billing `invoicing` | Started on `tenant.created`, renewed monthly, SST invoice |
| Credits | billing `credits` | 1 credit = RM 1, FIFO lots expiring after 12 months, rate card + per-agreement overrides, low-balance alert at 20 % of last top-up, 7-day grace then paid extras pause, refunds for failed actions, optional auto top-up |

Settlement entries are `accrued` on payment, `earned` when the sub-order completes, `reversed` on
refund (or clawed back with a negative entry if already paid out). Weekly payouts batch earned
entries per partner (net) and per clinic group (its commission share); finance approves and marks paid.

## Security

* JWT RS256 with JWKS; claims carry `tenant_id`, `roles`, `partner_ids`, `partner_admin_ids`.
  Swap to **Amazon Cognito** by pointing `JWKS_URL` / `JWT_ISSUER` at the user pool — `cognito:groups`
  map to roles. Identity then becomes a thin facade over Cognito custom-auth (OTP).
* Refresh tokens are opaque, hashed, rotated, with family revocation on reuse.
* `/internal/*` is service-to-service only (shared key locally; mTLS / service mesh in prod) and is
  blocked at the gateway.
* Bank account numbers are Fernet-encrypted (KMS-managed key in prod); only last-4 is ever returned
  to clients. Documents are private in S3 and served by short-lived presigned URLs.
* OTP: hashed in Redis, 5-minute TTL, attempt limit, per-phone hourly cap and resend cool-down,
  plus gateway rate limiting.

## Scaling to production (AWS, ap-southeast-1)

* ECS Fargate service per microservice; split each into `api` and `worker` tasks
  (`RUN_CONSUMERS` / `RUN_OUTBOX_RELAY` / `RUN_JOBS` flags) and autoscale api on CPU / p95 latency and
  workers on consumer lag.
* Run `alembic upgrade head` as a one-off task per deploy and set `RUN_MIGRATIONS=false` on services.
* Aurora PostgreSQL (one cluster per critical domain: order, payment, billing; shared cluster with
  separate databases for the rest), MSK (3 brokers, RF 3), ElastiCache Redis (cluster mode),
  DocumentDB or Atlas for the catalog, S3 with SSE-KMS.
* Observability: JSON logs with request id, Prometheus `/metrics` on every service, `/health` and
  `/ready` for load-balancer and ECS health checks. Add OpenTelemetry traces next.
* Catalog search can move to OpenSearch when needed; the offer document is already search-shaped.

## Extending

* **Payment gateway**: implement `PaymentProvider` in `services/payment/app/providers.py`.
* **Courier**: implement `Courier` in `services/pharmacy/app/courier.py`.
* **SMS / email / push**: replace `ConsoleChannel` in `services/notification/app/channels.py`.
* **AI report summaries**: implement `ReportSummarizer` in `services/lab/app/summarizer.py`.
* **Existing PHP system**: `HttpLegacyClient` in `services/legacy_bridge/app/legacy.py` — see
  [LEGACY_CONTRACT.md](LEGACY_CONTRACT.md).
