# Contract with the existing GeneJunction (PHP) system

The marketplace never reads the PHP database. `legacy-bridge` calls these REST endpoints
(`LEGACY_MODE=http`, `LEGACY_BASE_URL`, header `X-Api-Key: $LEGACY_API_KEY`). Shapes below are what
`HttpLegacyClient` expects; agree them with the PHP team and adjust the adapter if they differ.

## Calls from the marketplace to PHP

| Method & path | Request | Response |
|---|---|---|
| `GET /api/doctors?specialty=&q=` | — | `[{id, name, specialty, clinic, fee}]` |
| `GET /api/doctors/{id}` | — | `{id, name, specialty, clinic, fee}` |
| `GET /api/doctors/{id}/slots?date=YYYY-MM-DD` | — | `[{slot_id, start, end}]` (ISO 8601 with offset) |
| `POST /api/patients/find-or-create` | `{phone, name, email}` | `{patient_id}` |
| `POST /api/bookings` | `{doctor_id, slot_id, patient_id, external_ref, paid_amount}` | `{booking_id, video_url}` |
| `GET /api/patients/{id}/folio` | — | `[{id, type, title, date, ...}]` |
| `POST /api/patients/{id}/folio` | `{type: "lab_report", title, date, source, document_url, ai_summary, marketplace_order}` | 2xx |
| `POST /api/patients/{id}/orders` | `{external_order_id, total, status, description}` | 2xx |

`external_ref` is the marketplace sub-order id; PHP should treat a repeated booking with the same
`external_ref` as the same booking (idempotency).

## Webhooks from PHP to the marketplace

`POST https://<api>/api/v1/legacy/webhooks/<name>` with header
`X-Signature: hex(HMAC-SHA256(LEGACY_WEBHOOK_SECRET, raw body))`.

| Name | Body | Effect |
|---|---|---|
| `prescription-issued` | `{booking_id, consult_id, doctor_name, items: [{kind: "medicine"\|"lab_test", sku?, name, qty}], document_url, issued_at}` | Consult marked completed; medicines and tests matched to offers in the patient's network and put in their cart |
| `consult-status` | `{booking_id, status: "completed"\|"no_show"\|"cancelled"}` | Consultation sub-order updated (cancel → automatic refund) |
| `order-created` | `{order_id, patient_id, description, total, status, ordered_at, details}` | Copied into the patient's unified order history |

Doctors for white-label networks are set up as consultants in PHP first, then added to the tenant's
network in the business console with their PHP doctor id (`legacy_doctor_id`).
