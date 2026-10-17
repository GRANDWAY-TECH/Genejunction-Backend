import uuid
from decimal import Decimal

from app.api import _unit_price
from app.visibility import compute_visibility, tenant_filter, visible_to
from gj_common.tenancy import TenantContext

SJ = str(uuid.uuid4())
PLATFORM = TenantContext(id=uuid.uuid4(), slug="genejunction", name="GeneJunction", type="platform")
SEJAHTERA = TenantContext(id=uuid.UUID(SJ), slug="sejahtera", name="Sejahtera Care", type="white_label")


def test_independent_verified_partner_is_on_marketplace_only():
    vis = compute_visibility({"status": "verified", "owner_tenant_id": None})
    assert vis == {"verified": True, "marketplace": True, "tenants": []}


def test_unverified_partner_is_hidden_everywhere():
    vis = compute_visibility({"status": "under_review", "owner_tenant_id": None,
                              "tenants": {SJ: {"listed": True, "status": "active"}}})
    assert vis == {"verified": False, "marketplace": False, "tenants": []}


def test_clinic_group_provider_is_private_until_listed():
    private = compute_visibility({"status": "verified", "owner_tenant_id": SJ,
                                  "tenants": {SJ: {"listed": False, "status": "active"}}})
    assert private["tenants"] == [SJ] and not private["marketplace"]
    listed = compute_visibility({"status": "verified", "owner_tenant_id": SJ,
                                 "tenants": {SJ: {"listed": True, "status": "active"}}})
    assert listed["marketplace"]


def test_inactive_membership_removes_tenant_visibility():
    vis = compute_visibility({"status": "verified", "owner_tenant_id": SJ,
                              "tenants": {SJ: {"listed": True, "status": "inactive"}}})
    assert vis["tenants"] == [] and not vis["marketplace"]


def test_filters_per_brand():
    assert tenant_filter(PLATFORM) == {"visibility.marketplace": True}
    assert tenant_filter(SEJAHTERA) == {"visibility.tenants": SJ}
    offer = {"visibility": {"verified": True, "marketplace": False, "tenants": [SJ]}}
    assert visible_to(offer, SEJAHTERA) and not visible_to(offer, PLATFORM)


def test_wholesale_tier_pricing():
    offer = {"price": Decimal("26.00"), "wholesale_tiers": [{"min_qty": 40, "price": Decimal("24.00")},
                                                            {"min_qty": 200, "price": Decimal("22.50")}]}
    assert _unit_price(offer, 10) == Decimal("26.00")
    assert _unit_price(offer, 40) == Decimal("24.00")
    assert _unit_price(offer, 500) == Decimal("22.50")
