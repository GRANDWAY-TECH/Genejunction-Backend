# GeneJunction Marketplace — backend

FastAPI microservices for the GeneJunction marketplace: one engine serving **B2C** (GeneJunction app &
web), **B2B** (partner portals, business console) and **B2B2C** (white-label apps for clinic groups),
integrated with the existing GeneJunction (PHP) system for consultations and the health folio.

**API reference:** `http://localhost:8080/docs/` (Redoc, with a Swagger "try it" console) — built from
[docs/api/openapi.json](docs/api/openapi.json); guide: [docs/API_GUIDE.md](docs/API_GUIDE.md).

Architecture, data stores and scaling: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
Events: [docs/EVENTS.md](docs/EVENTS.md) · PHP integration contract: [docs/LEGACY_CONTRACT.md](docs/LEGACY_CONTRACT.md)

## Quick start

```bash
docker compose up -d --build          # Postgres, MongoDB, Redis, Redpanda (Kafka), S3 mock, gateway, 12 services
make venv && make e2e                 # full journey through the gateway: B2C, B2B2C, B2B
make test                             # unit + DB-backed tests
```

* API gateway: `http://localhost:8080` (`/api/v1/...`)
* Swagger per service: `http://localhost:8101/docs` … `http://localhost:8112/docs`
* Dev admin: `admin@genejunction.com` / `Admin12345!` (`POST /api/v1/auth/login`)
* Phone OTP is echoed as `debug_otp` in dev; the payment gateway is a mock
  (`POST /api/v1/payments/mock/{payment_id}/complete`); the PHP system is mocked by the bridge
  (`LEGACY_MODE=mock`).
* White-label requests: send `X-Tenant: <slug>` (mobile builds) or call from the tenant's domain.
* Host ports: Postgres `55432`, MongoDB `27017`, Redis `6379`, Kafka `19092`, S3 `9000`.

## Services

```
libs/gj_common/        shared platform code: app factory, auth (JWT/JWKS, Cognito-ready), tenancy,
                       outbox + Kafka consumers, errors, logging, pagination, money, S3, jobs
services/
  identity/            phone OTP + password login, JWT/JWKS, roles, refresh rotation
  tenant/              brands, domains, branding, provider networks, marketplace listing toggle
  partner/             onboarding & KYC for pharmacies, labs, stockists, doctors, clinic groups
  catalog/             (MongoDB) products, offers, search, stock reservations, Rx matching
  order/               cart, checkout saga, sub-orders per partner, partner inbox, prescriptions
  payment/             payment intents, gateway adapters, webhooks, refunds
  billing/             commission & settlement, credits, payouts, subscriptions, SST invoices
  pharmacy/            fulfilment: Rx check, pack, courier, delivery
  lab/                 slots, sample collection, reports, AI summaries (credits)
  wholesale/           stockist purchase orders
  notification/        SMS / WhatsApp / email / push, inboxes, campaigns
  legacy_bridge/       consult & health-folio bridge to the existing PHP system
gateway/nginx.conf     local stand-in for CloudFront/WAF/API Gateway
scripts/e2e_journey.py end-to-end journey test
```

## Main API surface (via the gateway)

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/otp/request`, `/auth/otp/verify`, `/auth/signup`, `/auth/login`, `/auth/refresh`, `/auth/logout`, `GET/PATCH /users/me` |
| Brand | `GET /tenants/current` (theme for app/web), tenant admin: `/tenants/{id}`, `/branding`, `/domains`, `/network`, `/admins` |
| Partners | `POST /partners`, `/partners/{id}/documents`, `/bank-account`, `/submit`, ops: `/approve`, `/reject`, `/suspend` |
| Catalog | `GET /catalog/search`, `/catalog/products/{id}`, partner: `/catalog/partners/{id}/offers`, `/catalog/wholesale/search` |
| Consult & folio | `GET /consult/doctors`, `/consult/doctors/{id}/slots`, `/consult/bookings`, `GET /folio` |
| Shopping | `/cart`, `/cart/items`, `/cart/consultation`, `POST /orders/checkout` (`Idempotency-Key`), `/orders`, `/orders/{id}/cancel`, `/orders/history`, `/prescriptions` |
| Partner ops | `/partner-orders`, `/pharmacy/fulfilments/{id}/{accept,verify-prescription,pack,dispatch,delivered}`, `/lab/bookings/{id}/{accept,schedule,sample-collected,processing,report-upload,report-ready}`, `/wholesale/purchase-orders` |
| Billing | `/billing/wallets/{owner_type}/{owner_id}` (+ `/ledger`, `/topups`, `/auto-topup`), `/billing/rate-card`, `/billing/credit-packs`, `/billing/partners/{id}/settlements`, `/billing/invoices`, finance: `/billing/payouts`, `/billing/commission-rules`, `/billing/reports/revenue` |
| Notifications | `/notifications/me`, `/notifications/business`, `/notifications/devices`, `/notifications/campaigns` |

All paths are prefixed with `/api/v1`. Errors share one shape:
`{"error": {"code", "message", "details"}, "request_id"}`.

## Working on a service

```bash
# after changing models
make migration s=order m="add delivery window"
docker compose up -d --build order
make docs        # after changing any endpoint: regenerate the API reference
```

Each service is a standard FastAPI app (`app/main.py`) built with `gj_common.app_factory`; the same
`Dockerfile` builds every service via `--build-arg SERVICE=<name>`.
