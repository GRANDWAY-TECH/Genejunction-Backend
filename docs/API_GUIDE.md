One API for every GeneJunction app: the **GeneJunction mobile app and website** (B2C), **white-label apps** of clinic groups (B2B2C), the **partner portals and business console** (B2B), and GeneJunction's own **ops, admin and finance consoles**.

All endpoints sit behind one gateway and start with `/api/v1`.

| Environment | Base URL |
|---|---|
| Local (docker compose) | `http://localhost:8080` |
| Staging / production | set per deployment |

# Getting started

The fastest way to see the API working is the local stack:

```bash
docker compose up -d --build
make venv && make e2e        # runs every journey below against the API
```

In local and dev environments:

* the login code (OTP) is returned in the response as `debug_otp`, so no SMS is needed;
* payments go through a mock gateway — finish one with `POST /api/v1/payments/mock/{payment_id}/complete`;
* the existing GeneJunction system is simulated, with four sample doctors (`doc-1001` … `doc-1004`);
* the admin login is `admin@genejunction.com` / `Admin12345!`.

# Authentication

Send the access token on every call that needs a user:

```
Authorization: Bearer <access_token>
```

## Patients: phone number + one-time code

```bash
curl -X POST $BASE/api/v1/auth/otp/request -H 'Content-Type: application/json' \
     -d '{"phone": "0123456789"}'
# {"sent": true, "expires_in": 300, "debug_otp": "482913"}     <- debug_otp only in dev

curl -X POST $BASE/api/v1/auth/otp/verify -H 'Content-Type: application/json' \
     -d '{"phone": "0123456789", "code": "482913", "full_name": "Daniel Tan"}'
```

The first verification creates the account and links it to the patient's record in the existing
GeneJunction system, so their consultations and health folio follow them. Malaysian local numbers
(`012…`) are converted to `+6012…`. Limits: one code per 30 seconds, 5 per hour, 5 wrong attempts.

## Business users: email + password

`POST /auth/signup` creates an account and `POST /auth/login` signs in. These are for partner portals,
the business console and GeneJunction staff.

## Tokens

Both login calls return:

```json
{
  "access_token": "eyJ…", "refresh_token": "hN3…", "token_type": "Bearer", "expires_in": 900,
  "user": {"id": "…", "full_name": "Daniel Tan", "phone": "+60123456789", "email": null,
           "roles": ["patient"], "partner_ids": [], "tenant_id": "…"}
}
```

* Access tokens last **15 minutes**. Use `POST /auth/refresh` with the refresh token to get a new pair.
  Each refresh token works **once**; reusing an old one logs the user out everywhere.
* **Refresh right after** registering a partner, being invited as staff or being made a clinic-group
  admin. The new access token then carries the new `roles` and `partner_ids`.
* `POST /auth/logout` ends the session.

## Roles

| Role | Who | Can |
|---|---|---|
| `patient` | Everyone | Shop, book consultations, see own orders, reports, health folio |
| `partner_admin` / `partner_staff` | Pharmacy, lab, stockist, doctor teams | Run their partner (`partner_ids` lists which). Only admins change bank details or invite staff |
| `tenant_admin` | Clinic-group admins | Business console for their white-label brand |
| `gj_ops`, `gj_finance`, `gj_admin` | GeneJunction staff | Verify partners, manage tenants, commission, payouts |

# Brands (tenants)

Every request belongs to a brand: GeneJunction itself, or a clinic group's white-label app such as
*Sejahtera Care*. The brand decides which providers and doctors the patient sees and how orders are tagged.

* **Websites**: nothing to send. The brand comes from the domain (`genejunction.com`,
  `care.sejahtera.com.my`).
* **Mobile apps**: send the brand slug in every request: `X-Tenant: sejahtera`. The GeneJunction app
  can omit it.
* At start-up, apps call `GET /tenants/current` for the name, colours, logo and enabled features.

A login belongs to one brand. To act as a clinic-group admin, log in with that brand's `X-Tenant`.

| Brand | Channel on orders | Providers shown |
|---|---|---|
| GeneJunction | `B2C` | Every verified provider, plus clinic-group providers they chose to list |
| White label | `B2B2C` | Only the clinic group's own network |
| Stockist purchase orders | `B2B` | — |

# Conventions

| Topic | Rule |
|---|---|
| Format | JSON, UTF-8 |
| Money | Malaysian ringgit as **strings with two decimals**: `"38.00"`. Never parse into floating point |
| Credits | 1 credit = RM 1, also strings (`"0.20"` per SMS) |
| IDs | UUIDs (time-ordered). Catalog products and offers use string ids |
| Times | ISO 8601 in UTC (`2026-10-17T03:15:00Z`); consult slots carry the +08:00 offset |
| Paging | `?limit=20&offset=0` (max 100). Responses: `{"items": [...], "total", "limit", "offset"}` |
| Request id | Every response has `X-Request-ID`; quote it when reporting a problem |
| Rate limits | OTP: 10 requests/minute per IP. Everything else: 50/second per IP with bursts |

## Errors

Every error has the same body:

```json
{"error": {"code": "cart_invalid", "message": "Some items cannot be ordered",
           "details": [{"offer_id": "…", "problem": "insufficient_stock", "available": 2}]},
 "request_id": "4e9ca546ab45ee96ca7f3de700032411"}
```

Branch on `error.code`, not the message. `message` is safe to show to users.

| HTTP | Codes |
|---|---|
| 400 | `bad_request`, `otp_expired`, `otp_invalid` |
| 401 | `unauthorized`, `invalid_token`, `token_expired` (refresh and retry), `token_reused`, `invalid_credentials`, `bad_signature` |
| 402 | `insufficient_credits` — paid extras paused until a top-up |
| 403 | `forbidden`, `tenant_suspended` |
| 404 | `not_found`, `unknown_tenant`, `folio_not_linked` (retry shortly after sign-up), `no_offer` |
| 409 | `conflict`, `cart_invalid`, `out_of_stock`, `slot_unavailable`, `invalid_transition`, `not_cancellable`, `not_refundable`, `rx_unverified`, `awaiting_payment`, `report_missing`, `report_not_ready`, `email_taken`, `phone_taken`, `duplicate_partner`, `duplicate_sku`, `slug_taken`, `domain_taken` |
| 422 | `validation_failed` (field errors in `details`), `cart_empty`, `address_required`, `lab_collection_required`, `prescription_required`, `one_consultation`, `multiple_stockists`, `invalid_phone`, `refund_too_large` |
| 429 | `rate_limited` |
| 502 / 503 | `upstream_error` — a dependency (payment gateway, existing system) failed; safe to retry |

## Safe retries

`POST /orders/checkout` requires an `Idempotency-Key` header. Use a new UUID per checkout attempt and
reuse it when retrying after a timeout: you get the same order back, never a second charge. Other
writes are safe to repeat unless stated.

## File uploads

Files never pass through the API. The API returns a short-lived upload URL (licences, prescriptions,
lab reports):

```bash
# 1. ask for an upload ticket
curl -X POST $BASE/api/v1/partners/$PARTNER/documents -H "Authorization: Bearer $T" \
     -H 'Content-Type: application/json' \
     -d '{"doc_type": "pharmacy_licence", "file_name": "licence.pdf", "content_type": "application/pdf"}'
# {"upload_url": "https://…", "method": "PUT", "headers": {"Content-Type": "application/pdf"}, "document_id": "…"}

# 2. upload the file itself to upload_url
curl -X PUT "$UPLOAD_URL" -H 'Content-Type: application/pdf' --data-binary @licence.pdf
```

Downloads work the same way: endpoints return a `download_url` that is valid for 15 minutes.

## Payments

Checkout, credit top-ups, subscription invoices and prepaid purchase orders all return a `payment`:

```json
{"id": "…", "purpose": "order", "amount": "228.00", "currency": "MYR", "status": "requires_action",
 "method": "fpx", "checkout_url": "https://…", "return_url": "https://app/return", "expires_at": "…"}
```

1. Open `checkout_url` (FPX bank page, card form or e-wallet). Pass your own `return_url` when creating it.
2. When the customer comes back, re-read the order (or wallet, or invoice). The result arrives from the
   payment provider by webhook within seconds; don't treat the redirect itself as proof of payment.
3. Unpaid orders expire after 30 minutes, which releases the stock and the consult slot.

# Patient journeys

## Find and buy

1. `GET /catalog/search?q=vitamin d` — one row per product with `best_offer`, `min_price` and
   `offer_count`. Filter with `type` (`medicine`, `lab_test`, `wellness`, `device`), `category`,
   `requires_prescription`, and `lat`/`lng`/`radius_km` for nearby providers.
2. `GET /catalog/products/{id}` — every offer for that product in this app, cheapest first.
3. `POST /cart/items {"offer_id", "qty"}`.

## Book a consultation

1. `GET /consult/doctors?specialty=Cardiology`
2. `GET /consult/doctors/{id}/slots?day=2026-10-18`
3. `POST /cart/consultation {"doctor_id", "slot_id"}`. The consultation is paid together with the cart
   and booked in the existing system after payment. `GET /consult/bookings` returns the `video_url`.

After the consultation, the doctor's e-prescription is matched to medicines and tests from providers in
the patient's app and **added to their cart automatically** (`source: "prescription"`). The patient gets
a "Your cart is ready" notification. `GET /prescriptions` lists the prescriptions.

## Check out once

```bash
curl -X POST $BASE/api/v1/orders/checkout -H "Authorization: Bearer $T" \
  -H "Idempotency-Key: $(uuidgen)" -H 'Content-Type: application/json' -d '{
    "payment_method": "fpx",
    "return_url": "https://genejunction.com/orders/return",
    "delivery_address": {"recipient": "Daniel Tan", "phone": "+60123456789", "line1": "12 Jalan Bukit Bintang",
                         "city": "Kuala Lumpur", "state": "WP", "postcode": "55100"},
    "lab_collection": {"type": "home_collection", "date": "2026-10-18", "slot": "08:00-10:00"}
  }'
```

The order is split into **sub-orders**, one per provider. Fees: delivery is RM 5 under RM 50; home
sample collection is RM 20. Lab collection slots come from `GET /lab/{lab_partner_id}/slots?date=`.

## Track

* `GET /orders/{id}` shows the order and each sub-order's status, `tracking` and `schedule`.
* `GET /orders/{id}/timeline` lists every status change.
* `GET /orders/history` merges marketplace orders with orders from the existing system.
* `POST /orders/{id}/cancel` works until a provider accepts. Paid amounts are refunded automatically.
* If a provider rejects its part, that part alone is cancelled and refunded.

| Order `status` | Meaning |
|---|---|
| `pending_payment` | Waiting for payment (30 min) |
| `paid` | Paid; providers are working on it |
| `completed` / `partially_completed` | All parts done / done except cancelled parts |
| `cancelled`, `expired`, `payment_failed` | Closed; nothing charged or everything refunded |

| Sub-order kind | Status flow |
|---|---|
| `pharmacy` | `placed → accepted → rx_verified* → packed → dispatched → delivered → completed` (or `ready_for_pickup → delivered`). *Only for prescription medicine |
| `lab` | `placed → accepted → scheduled → sample_collected → processing → report_ready → completed` |
| `consultation` | `placed → booked → completed` (or `booking_failed`, `no_show`) |

Any part can also end `rejected` or `cancelled`, and both are refunded.

## Reports, health folio, notifications

* `GET /lab/my-bookings` and `GET /lab/my-bookings/{id}/report` return the PDF `download_url` and
  optional `ai_summary`.
* `GET /folio` returns records, prescriptions and lab reports from the existing GeneJunction system. These
  are the same in every brand's app.
* `GET /notifications/me` is the in-app inbox. Register push tokens with `POST /notifications/devices`.

# Partner journeys (B2B)

## Onboard

1. `POST /auth/signup`, then `POST /partners` with `type` = `pharmacy` | `lab` | `stockist` | `doctor` |
   `clinic_group`. Refresh the token afterwards.
2. Upload documents (`POST /partners/{id}/documents`) and set the payout bank account
   (`PUT /partners/{id}/bank-account`; only the last 4 digits are ever shown).
3. `POST /partners/{id}/submit`. GeneJunction ops approves or rejects, and the partner gets a notification.
4. Invite colleagues with `POST /partners/{id}/staff`.

## Sell

* Products come from a shared master list: `GET /catalog/products?q=`. Propose a missing one with
  `POST /catalog/products?partner_id=`; ops approves it.
* `POST /catalog/partners/{id}/offers` sets the price, MRP and stock. Stockists use `channel: "wholesale"`
  with quantity tiers.
* `POST /catalog/partners/{id}/offers/stock` bulk-updates stock levels.

## Fulfil

* Orders from every app arrive in one inbox, tagged by `channel`: `GET /partner-orders?partner_id=`.
* Pharmacy actions: `/pharmacy/fulfilments/{id}/accept | verify-prescription | pack | dispatch |
  ready-for-pickup | delivered | reject`. `dispatch` books the courier and returns tracking.
* Lab actions: `/lab/bookings/{id}/accept | schedule | sample-collected | processing | report-upload |
  report-ready | reject`.

## Restock from a stockist

1. `GET /catalog/wholesale/search`
2. `POST /wholesale/purchase-orders` — prepaid or `net30`. All items must come from one stockist, and tier
   prices apply automatically.
3. The stockist accepts, then ships. The buyer confirms with `/receive`, and their retail stock increases
   automatically.

## Get paid

* `GET /billing/partners/{id}/settlements` shows each sale's gross amount, GeneJunction commission and the
  partner's net amount.
* `GET /billing/payees/partner/{id}/payouts` lists weekly payouts.

## Credits for paid extras

Businesses prepay credits (1 credit = RM 1, valid 12 months) for extras on the rate card
(`GET /billing/rate-card`): AI lab report summaries, SMS/WhatsApp alerts, featured listings, patient
campaigns and extra admin users.

* Buy credits: `GET /billing/credit-packs`, then `POST /billing/wallets/{owner_type}/{owner_id}/topups`.
  Starter, Growth (+5%) and Enterprise (+10%) packs; SST is added and invoiced.
* Balance and history: `GET /billing/wallets/...` and `.../ledger`.
* At 20% of the last top-up a low-balance alert is sent. Optional auto top-up: `PUT .../auto-topup`.
* At zero a **7-day grace** starts, then paid extras pause (`402 insufficient_credits`). Orders,
  consultations and payments are never affected.
* Feature a listing in search: `POST /billing/partners/{id}/featured {"weeks": 1}` (300 credits a week).

# Clinic groups (white label)

GeneJunction ops creates the tenant (`POST /tenants`) with the plan and the admin's email. The admin then
logs in with `X-Tenant: <slug>` and uses the business console:

* **Brand & app**: `PUT /tenants/{id}/branding` sets the name, logo and colours. `POST /tenants/{id}/domains`
  adds a custom domain, which goes live after DNS verification by ops.
* **My network**: `POST /tenants/{id}/network` adds a pharmacy or lab partner, or a doctor by their id in
  the existing system. Providers the clinic group registers itself (`owner_tenant_id`) join automatically.
* **List on marketplace**: `PATCH /tenants/{id}/network/{member_id} {"listed_on_marketplace": true}` also
  shows that provider in the GeneJunction app.
* **Commission**: `commission_pct` on a network member is the clinic group's share of that provider's
  orders, paid out to the clinic group.
* **Earnings and invoices**: `GET /billing/tenants/{id}/earnings` and `GET /billing/invoices?customer_type=tenant`.
* **Campaigns**: `POST /notifications/campaigns` sends a push + SMS campaign to the brand's patients for
  200 credits.

# GeneJunction consoles

| Console | Endpoints |
|---|---|
| Ops | `GET /partners?status=under_review`, `/partners/{id}/approve|reject|suspend`, `/catalog/products` approval (`PATCH status`), `GET /admin/orders`, `POST /tenants`, domain verification |
| Finance | `/billing/commission-rules`, `/billing/rate-overrides`, `/billing/payouts/run`, `/approve`, `/mark-paid`, `/billing/wallets/.../adjust`, `/billing/reports/revenue`, `GET /payments` |
| Admin | `GET /users`, `POST /users/{id}/roles` |

Integration with the existing PHP system and webhooks from it are described in `docs/LEGACY_CONTRACT.md`.
