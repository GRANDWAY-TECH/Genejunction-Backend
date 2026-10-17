"""End-to-end journey through the API gateway (docker compose stack must be running).

    python scripts/e2e_journey.py [--base http://localhost:8080]

Covers the blueprint's journeys: partner onboarding (B2B), Daniel on the GeneJunction app (B2C),
consult -> prescription -> cart, pharmacy & lab fulfilment, credits, commission and payouts,
Sejahtera white label (B2B2C) and stockist wholesale.
"""

import argparse
import sys
import time
import traceback
import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx

ADMIN = ("admin@genejunction.com", "Admin12345!")
RUN = uuid.uuid4().hex[:6]


class Api:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=30)

    def call(self, method: str, path: str, token: str | None = None, tenant: str | None = None,
             expect: int | tuple = (200, 201, 204), **kw):
        headers = kw.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if tenant:
            headers["X-Tenant"] = tenant
        resp = self.http.request(method, path, headers=headers, **kw)
        expected = (expect,) if isinstance(expect, int) else expect
        if resp.status_code not in expected:
            raise AssertionError(f"{method} {path} -> {resp.status_code}: {resp.text[:600]}")
        return resp.json() if resp.content else None

    def get(self, path, token=None, **kw):
        return self.call("GET", path, token, **kw)

    def post(self, path, token=None, **kw):
        return self.call("POST", path, token, **kw)


def step(msg: str) -> None:
    print(f"  ✓ {msg}")


def wait_for(fn, what: str, timeout: float = 30):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except AssertionError as exc:
            last = exc
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}: {last}")


def business_user(api: Api, name: str, tenant: str | None = None) -> dict:
    email = f"{name.lower().replace(' ', '.')}.{RUN}@example.com"
    return api.post("/api/v1/auth/signup", tenant=tenant, json={"email": email, "password": "Passw0rd!Strong", "full_name": name})


def refresh(api: Api, tokens: dict) -> dict:
    return api.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})


def onboard_partner(api: Api, admin: str, owner: dict, ptype: str, name: str, *, owner_tenant_id: str | None = None,
                    tenant: str | None = None) -> tuple[str, dict]:
    token = owner["access_token"]
    p = api.post("/api/v1/partners", token, tenant=tenant, json={
        "type": ptype, "legal_name": f"{name} Sdn Bhd", "display_name": name, "registration_no": f"SSM-{RUN}-{name[:6]}",
        "licence_no": "LIC-1", "contact_email": f"ops.{RUN}@{name.lower().replace(' ', '')}.my", "contact_phone": "+60312345678",
        "address": {"line1": "1 Jalan Ampang", "city": "Kuala Lumpur", "state": "WP", "postcode": "50450"},
        "latitude": 3.1579, "longitude": 101.7116, "owner_tenant_id": owner_tenant_id})
    owner = refresh(api, owner)  # new token carries the partner_ids claim
    token = owner["access_token"]
    upload = api.post(f"/api/v1/partners/{p['id']}/documents", token, json={
        "doc_type": "pharmacy_licence" if ptype == "pharmacy" else "ssm_certificate",
        "file_name": "licence.pdf", "content_type": "application/pdf"})
    put = httpx.put(upload["upload_url"], content=b"%PDF-1.4 licence", headers=upload["headers"])
    assert put.status_code in (200, 201), put.text
    api.call("PUT", f"/api/v1/partners/{p['id']}/bank-account", token,
             json={"bank_name": "Maybank", "account_name": f"{name} Sdn Bhd", "account_no": "514012345678"})
    api.post(f"/api/v1/partners/{p['id']}/submit", token)
    approved = api.post(f"/api/v1/partners/{p['id']}/approve", admin, json={"note": "Licence checked"})
    assert approved["status"] == "verified"
    return p["id"], refresh(api, owner)


def run(base: str) -> None:
    api = Api(base)
    print(f"GeneJunction E2E against {base} (run {RUN})")

    # ---------------------------------------------------------------- 1. ops + partners (B2B)
    admin = api.post("/api/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})["access_token"]
    step("GJ admin logged in")

    pharm_owner = business_user(api, "Farma Care")
    pharmacy_id, pharm_owner = onboard_partner(api, admin, pharm_owner, "pharmacy", "FarmaCare")
    lab_owner = business_user(api, "Pathlab KL")
    lab_id, lab_owner = onboard_partner(api, admin, lab_owner, "lab", "Pathlab KL")
    stock_owner = business_user(api, "Medi Stockist")
    stockist_id, stock_owner = onboard_partner(api, admin, stock_owner, "stockist", "MediSupply")
    pt, lt, st = pharm_owner["access_token"], lab_owner["access_token"], stock_owner["access_token"]
    assert pharmacy_id in pharm_owner["user"]["partner_ids"]
    step("pharmacy, lab and stockist registered, documents uploaded, verified by ops")

    sku = lambda s: f"{s}-{RUN}"  # noqa: E731
    vitd = api.post("/api/v1/catalog/products", admin, json={
        "sku": sku("VITD1000"), "type": "medicine", "name": f"Vitamin D3 1000IU {RUN}", "category": "Vitamins",
        "brand": "Blackmores", "requires_prescription": False})
    amlo = api.post("/api/v1/catalog/products", admin, json={
        "sku": sku("AMLO5"), "type": "medicine", "name": f"Amlodipine 5mg {RUN}", "generic_name": "amlodipine",
        "category": "Cardiovascular", "requires_prescription": True})
    fbc = api.post("/api/v1/catalog/products", admin, json={
        "sku": sku("FBC"), "type": "lab_test", "name": f"Full Blood Count {RUN}", "category": "Blood tests",
        "lab": {"sample_type": "blood", "turnaround_hours": 24, "home_collection": True}})
    off_vitd = api.post(f"/api/v1/catalog/partners/{pharmacy_id}/offers", pt,
                        json={"product_id": vitd["id"], "price": "38.00", "mrp": "45.00", "stock": 100})
    off_amlo = api.post(f"/api/v1/catalog/partners/{pharmacy_id}/offers", pt,
                        json={"product_id": amlo["id"], "price": "24.50", "stock": 50})
    off_fbc = api.post(f"/api/v1/catalog/partners/{lab_id}/offers", lt, json={"product_id": fbc["id"], "price": "45.00"})
    off_whs = api.post(f"/api/v1/catalog/partners/{stockist_id}/offers", st, json={
        "product_id": vitd["id"], "channel": "wholesale", "price": "26.00", "stock": 5000, "min_order_qty": 10,
        "wholesale_tiers": [{"min_qty": 40, "price": "24.00"}]})
    step("products added by ops; offers (retail, lab, wholesale) added by partners")

    wait_for(lambda: api.get("/api/v1/catalog/search", params={"q": RUN})["total"] >= 3, "offers visible on marketplace")
    step("search on the GeneJunction marketplace shows the new offers")

    # ---------------------------------------------------------------- 2. Daniel, B2C patient
    phone = f"+6012{int(RUN, 16) % 10_000_000:07d}"
    otp = api.post("/api/v1/auth/otp/request", json={"phone": phone})["debug_otp"]
    daniel = api.post("/api/v1/auth/otp/verify", json={"phone": phone, "code": otp, "full_name": "Daniel Tan"})
    dt = daniel["access_token"]
    step("Daniel signed up with phone OTP")
    wait_for(lambda: api.get("/api/v1/folio", dt, expect=(200, 404)) == [], "health folio linked to existing system")
    step("account linked to a patient record in the existing system (health folio reachable)")

    doctors = api.get("/api/v1/consult/doctors", params={"specialty": "Cardiology"})
    ravi = doctors[0]
    tomorrow = date.today() + timedelta(days=1)
    slot = api.get(f"/api/v1/consult/doctors/{ravi['id']}/slots", params={"day": tomorrow.isoformat()})[0]
    api.post("/api/v1/cart/consultation", dt, json={"doctor_id": ravi["id"], "slot_id": slot["slot_id"]})
    api.post("/api/v1/cart/items", dt, json={"offer_id": off_vitd["id"], "qty": 1})
    cart = api.post("/api/v1/cart/items", dt, json={"offer_id": off_fbc["id"], "qty": 1})
    assert len(cart["items"]) == 3 and not cart["problems"]
    step(f"cart: consult with {ravi['name']}, Vitamin D (pharmacy), Full Blood Count (lab)")

    address = {"recipient": "Daniel Tan", "phone": phone, "line1": "12 Jalan Bukit Bintang", "city": "Kuala Lumpur",
               "state": "WP", "postcode": "55100"}
    key = str(uuid.uuid4())
    checkout = api.post("/api/v1/orders/checkout", dt, headers={"Idempotency-Key": key}, json={
        "payment_method": "fpx", "delivery_address": address,
        "lab_collection": {"type": "home_collection", "date": tomorrow.isoformat(), "slot": "08:00-10:00"}})
    order = checkout["order"]
    again = api.post("/api/v1/orders/checkout", dt, headers={"Idempotency-Key": key}, json={
        "payment_method": "fpx", "delivery_address": address,
        "lab_collection": {"type": "home_collection", "date": tomorrow.isoformat(), "slot": "08:00-10:00"}})
    assert again["order"]["id"] == order["id"], "checkout must be idempotent"
    # 120 consult + 38 vit D + 5 delivery (under RM50) + 45 FBC + 20 home collection
    assert Decimal(order["total"]) == Decimal("228.00"), order["total"]
    step(f"one checkout, 3 sub-orders, total RM {order['total']} (idempotent retry returned the same order)")

    api.post(f"/api/v1/payments/mock/{checkout['payment']['id']}/complete", params={"outcome": "succeeded"})
    paid = wait_for(lambda: (o := api.get(f"/api/v1/orders/{order['id']}", dt))["status"] == "paid" and o, "order paid")
    subs = {s["kind"]: s for s in paid["sub_orders"]}
    step("paid once by FPX (mock gateway webhook) -> order paid")

    consult = wait_for(lambda: (s := {x["kind"]: x for x in api.get(f"/api/v1/orders/{order['id']}", dt)["sub_orders"]}
                                ["consultation"])["status"] == "booked" and s, "consultation booked")
    step("marketplace booked the consult in the existing system after payment")

    # ---------------------------------------------------------------- 3. pharmacy fulfils
    f = wait_for(lambda: api.get(f"/api/v1/pharmacy/{pharmacy_id}/fulfilments", pt)["items"], "pharmacy inbox")[0]
    assert f["channel"] == "B2C"
    for action in ("accept", "pack", "dispatch", "delivered"):
        f = api.post(f"/api/v1/pharmacy/fulfilments/{f['id']}/{action}", pt)
    assert f["status"] == "completed" and f["tracking_no"]
    step("pharmacy: accept -> pack -> courier dispatch -> delivered (tracking issued)")

    # ---------------------------------------------------------------- 4. lab fulfils, with AI summary paid in credits
    topup = api.post(f"/api/v1/billing/wallets/partner/{lab_id}/topups", lt, json={"pack_code": "growth"})
    assert Decimal(topup["total"]) == Decimal("5400.00")  # RM5,000 + 8% SST
    api.post(f"/api/v1/payments/mock/{topup['payment']['id']}/complete")
    wallet = wait_for(lambda: (w := api.get(f"/api/v1/billing/wallets/partner/{lab_id}", lt))["balance"] == "5250.00" and w,
                      "credits added")
    step("lab bought Growth pack: RM 5,400 incl. SST -> 5,250 credits (+5% bonus)")

    b = wait_for(lambda: api.get(f"/api/v1/lab/{lab_id}/bookings", lt)["items"], "lab booking")[0]
    api.post(f"/api/v1/lab/bookings/{b['id']}/accept", lt)
    api.post(f"/api/v1/lab/bookings/{b['id']}/schedule", lt, json={
        "date": tomorrow.isoformat(), "slot": "08:00-10:00", "phlebotomist_name": "Siti", "phlebotomist_phone": "+60123334444"})
    api.post(f"/api/v1/lab/bookings/{b['id']}/sample-collected", lt)
    api.post(f"/api/v1/lab/bookings/{b['id']}/processing", lt)
    up = api.post(f"/api/v1/lab/bookings/{b['id']}/report-upload", lt, json={})
    assert httpx.put(up["upload_url"], content=b"%PDF-1.4 report", headers=up["headers"]).status_code in (200, 201)
    b = api.post(f"/api/v1/lab/bookings/{b['id']}/report-ready", lt, json={"ai_summary": True})
    assert b["status"] == "completed" and b["ai_summary_status"] == "done"
    w = api.get(f"/api/v1/billing/wallets/partner/{lab_id}", lt)
    assert w["balance"] == "5240.00", w
    step("lab: schedule -> collect -> process -> report uploaded; AI summary charged 10 credits (5,240 left)")
    report = api.get(f"/api/v1/lab/my-bookings/{b['id']}/report", dt)
    assert report["download_url"]
    wait_for(lambda: any(r["type"] == "lab_report" for r in api.get("/api/v1/folio", dt)), "report in health folio")
    step("Daniel downloads the report; it is also saved to his health folio in the existing system")

    # ---------------------------------------------------------------- 5. doctor's prescription -> cart
    api.post(f"/api/v1/consult/dev/bookings/{consult['external_ref']}/prescribe", json={
        "items": [{"kind": "medicine", "sku": amlo["sku"], "name": "Amlodipine 5mg", "qty": 2}]})
    cart = wait_for(lambda: (c := api.get("/api/v1/cart", dt))["items"] and c, "prescription in cart")
    rx_item = cart["items"][0]
    assert rx_item["source"] == "prescription" and rx_item["offer_id"] == off_amlo["id"] and rx_item["qty"] == 2
    step("doctor issued e-prescription -> Amlodipine x2 placed in Daniel's cart automatically")
    final = wait_for(lambda: (o := api.get(f"/api/v1/orders/{order['id']}", dt))["status"] == "completed" and o,
                     "order completed")
    step("consult, pharmacy and lab parts all completed -> order completed")

    # ---------------------------------------------------------------- 6. commission + payouts
    settle = wait_for(lambda: (s := api.get(f"/api/v1/billing/partners/{pharmacy_id}/settlements", pt))["items"]
                      and s["items"][0]["status"] == "earned" and s, "pharmacy settlement earned")
    e = settle["items"][0]
    # B2C pharmacy: 10% of RM38 = 3.80; partner keeps 38 + 5 delivery - 3.80 = 39.20
    assert (e["gj_commission"], e["partner_net"]) == ("3.80", "39.20"), e
    step("commission: GJ 10% = RM 3.80, FarmaCare net RM 39.20")
    payouts = api.post("/api/v1/billing/payouts/run", admin)
    mine = next(p for p in payouts if p["payee_id"] == pharmacy_id)
    api.post(f"/api/v1/billing/payouts/{mine['id']}/approve", admin)
    paid_out = api.post(f"/api/v1/billing/payouts/{mine['id']}/mark-paid", admin, json={"bank_reference": f"IBG{RUN}"})
    assert paid_out["status"] == "paid" and paid_out["bank"]["account_last4"] == "5678"
    step(f"weekly payout RM {paid_out['amount']} approved and paid to FarmaCare")

    # ---------------------------------------------------------------- 7. Sejahtera white label (B2B2C)
    sj_owner = business_user(api, "Sejahtera Admin")
    slug = f"sejahtera-{RUN}"
    tenant = api.post("/api/v1/tenants", admin, json={
        "slug": slug, "name": "Sejahtera Care", "plan_code": "white_label_standard",
        "admin_email": sj_owner["user"]["email"], "branding": {"primary_color": "#9d174d"}})
    api.post(f"/api/v1/tenants/{tenant['id']}/activate", admin)
    sj_owner = api.post("/api/v1/auth/login", tenant=slug, json={"email": sj_owner["user"]["email"], "password": "Passw0rd!Strong"})
    sjt = sj_owner["access_token"]
    assert "tenant_admin" in sj_owner["user"]["roles"]
    brand = api.get("/api/v1/tenants/current", tenant=slug)
    assert brand["channel"] == "B2B2C" and brand["branding"]["primary_color"] == "#9d174d"
    step("Sejahtera onboarded on white label: own brand, tenant admin, B2B2C channel")

    sj_pharm_id, sj_owner = onboard_partner(api, admin, sj_owner, "pharmacy", "Sejahtera Pharmacy",
                                            owner_tenant_id=tenant["id"], tenant=slug)
    sjt = sj_owner["access_token"]
    sj_offer = api.post(f"/api/v1/catalog/partners/{sj_pharm_id}/offers", sjt,
                        json={"product_id": vitd["id"], "price": "36.00", "stock": 30})
    api.post(f"/api/v1/tenants/{tenant['id']}/network", sjt, json={
        "member_type": "doctor", "legacy_doctor_id": "doc-1002", "display_name": "Dr. Lim Wei Ling"})
    wait_for(lambda: any(i["best_offer"]["id"] == sj_offer["id"]
                         for i in api.get("/api/v1/catalog/search", tenant=slug, params={"q": RUN})["items"]),
             "Sejahtera pharmacy visible in Sejahtera app")
    sj_search = api.get("/api/v1/catalog/search", tenant=slug, params={"q": RUN})
    assert {i["best_offer"]["partner_id"] for i in sj_search["items"]} == {sj_pharm_id}
    gj_search = api.get("/api/v1/catalog/search", params={"q": RUN})
    assert sj_pharm_id not in {i["best_offer"]["partner_id"] for i in gj_search["items"]}
    wait_for(lambda: [d["id"] for d in api.get("/api/v1/consult/doctors", tenant=slug)] == ["doc-1002"]
             and "doc-1002" not in [d["id"] for d in api.get("/api/v1/consult/doctors")], "doctor network applied")
    step("Sejahtera app shows only its own pharmacy and Dr. Lim; both stay private on the GJ marketplace")

    members = api.get(f"/api/v1/tenants/{tenant['id']}/network", sjt)["items"]
    pharm_member = next(m for m in members if m["partner_id"] == sj_pharm_id)
    api.call("PATCH", f"/api/v1/tenants/{tenant['id']}/network/{pharm_member['id']}", sjt,
             json={"listed_on_marketplace": True})
    wait_for(lambda: sj_pharm_id in {o["partner_id"] for o in api.get(f"/api/v1/catalog/products/{vitd['id']}")["offers"]},
             "listed on marketplace")
    step("toggle 'list on marketplace' -> Sejahtera Pharmacy now also appears on GeneJunction")

    aphone = f"+6013{int(RUN, 16) % 10_000_000:07d}"
    code = api.post("/api/v1/auth/otp/request", tenant=slug, json={"phone": aphone})["debug_otp"]
    aisyah = api.post("/api/v1/auth/otp/verify", tenant=slug, json={"phone": aphone, "code": code, "full_name": "Aisyah"})
    at = aisyah["access_token"]
    api.post("/api/v1/cart/items", at, tenant=slug, json={"offer_id": sj_offer["id"], "qty": 2})
    co = api.post("/api/v1/orders/checkout", at, tenant=slug, headers={"Idempotency-Key": str(uuid.uuid4())},
                  json={"payment_method": "card", "delivery_address": {**address, "recipient": "Aisyah", "phone": aphone}})
    assert co["order"]["channel"] == "B2B2C"
    api.post(f"/api/v1/payments/mock/{co['payment']['id']}/complete")
    sj_entry = wait_for(lambda: api.get(f"/api/v1/billing/partners/{sj_pharm_id}/settlements", sjt)["items"], "B2B2C settlement")[0]
    # white_label_standard plan fee 2% of RM72 = 1.44
    assert sj_entry["channel"] == "B2B2C" and sj_entry["gj_commission"] == "1.44", sj_entry
    step("Aisyah orders in the Sejahtera app: tagged B2B2C, GJ takes the plan's 2% fee (RM 1.44)")
    invoices = api.get("/api/v1/billing/invoices", sjt, params={"customer_type": "tenant", "customer_id": tenant["id"]})
    assert invoices["items"] and invoices["items"][0]["kind"] == "subscription"
    step(f"subscription + setup invoice issued: {invoices['items'][0]['number']} RM {invoices['items'][0]['total']} (incl. SST)")

    # ---------------------------------------------------------------- 8. stockist wholesale (B2B)
    po = api.post("/api/v1/wholesale/purchase-orders", pt, json={
        "buyer_partner_id": pharmacy_id, "items": [{"offer_id": off_whs["id"], "qty": 40}],
        "payment_terms": "prepaid", "delivery_address": {"line1": "FarmaCare, Jalan Ampang"}})
    assert po["purchase_order"]["total"] == "960.00"  # 40 boxes at the RM24 tier price
    api.post(f"/api/v1/payments/mock/{po['payment']['id']}/complete")
    po_id = po["purchase_order"]["id"]
    wait_for(lambda: api.get(f"/api/v1/wholesale/purchase-orders/{po_id}", st)["payment_status"] == "paid", "PO paid")
    api.post(f"/api/v1/wholesale/purchase-orders/{po_id}/accept", st)
    api.post(f"/api/v1/wholesale/purchase-orders/{po_id}/ship", st, json={"courier": "J&T", "tracking_no": f"JT{RUN}"})
    api.post(f"/api/v1/wholesale/purchase-orders/{po_id}/receive", pt)
    wait_for(lambda: next(o for o in api.get(f"/api/v1/catalog/partners/{pharmacy_id}/offers", pt)["items"]
                          if o["id"] == off_vitd["id"])["stock"] == 139, "pharmacy restocked")
    whs = wait_for(lambda: api.get(f"/api/v1/billing/partners/{stockist_id}/settlements", st)["items"], "wholesale fee")[0]
    assert whs["channel"] == "B2B" and whs["gj_commission"] == "28.80"
    step("FarmaCare restocked 40 boxes from MediSupply (RM 960 tier price); stock 99 -> 139; GJ wholesale fee 3% = RM 28.80")

    # ---------------------------------------------------------------- 9. notifications & history
    inbox = api.get("/api/v1/notifications/me", dt)
    titles = {n["title"] for n in inbox["items"]}
    assert {"Order confirmed", "Your cart is ready", "Your lab report is ready"} <= titles, titles
    step(f"Daniel's inbox has {inbox['total']} notifications (order confirmed, cart ready, report ready, ...)")
    history = api.get("/api/v1/orders/history", dt)
    assert history[0]["number"] == final["number"]
    step("unified order history available")
    print(f"\nALL CHECKS PASSED ({RUN})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8080")
    try:
        run(parser.parse_args().base)
    except AssertionError as exc:
        traceback.print_exc()
        print(f"\nFAILED: {exc}")
        sys.exit(1)
