import uuid
from datetime import datetime, timezone
from decimal import Decimal

from gj_common.auth import Principal, claims_to_principal
from gj_common.db import uuid7
from gj_common.events import to_jsonable, topic_for
from gj_common.money import money, pct_of


def test_uuid7_is_v7_and_time_ordered():
    ids = [uuid7() for _ in range(1000)]
    assert all(i.version == 7 for i in ids)
    assert len(set(ids)) == 1000
    stamps = [i.int >> 80 for i in ids]
    assert stamps == sorted(stamps)


def test_money_rounds_half_up_and_keeps_two_places():
    assert money("1.005") == Decimal("1.01")
    assert money(3) == Decimal("3.00")
    assert pct_of(Decimal("38.00"), Decimal("10")) == Decimal("3.80")
    assert pct_of(Decimal("72.00"), Decimal("2.00")) == Decimal("1.44")


def test_event_payloads_keep_money_exact():
    payload = to_jsonable({"amount": Decimal("0.20"), "id": uuid.UUID(int=1),
                           "at": datetime(2026, 1, 1, tzinfo=timezone.utc), "nested": [Decimal("1.10")]})
    assert payload["amount"] == "0.20"
    assert payload["nested"] == ["1.10"]
    assert payload["at"].startswith("2026-01-01")


def test_topic_per_bounded_context():
    assert topic_for("order.sub_order.status_changed") == "gj.order"
    assert topic_for("billing.wallet.low_balance") == "gj.billing"


def test_claims_mapping_supports_local_and_cognito_tokens():
    uid, pid = uuid.uuid4(), uuid.uuid4()
    local = claims_to_principal({"sub": str(uid), "roles": ["patient", "partner_admin"],
                                 "partner_ids": [str(pid)], "partner_admin_ids": [str(pid)]})
    assert local.can_act_for_partner(pid) and pid in local.partner_admin_ids
    cognito = claims_to_principal({"sub": str(uid), "cognito:groups": ["gj_ops"], "custom:tenant_id": str(uid)})
    assert cognito.is_staff and cognito.tenant_id == uid


def test_partner_access_is_scoped():
    p = Principal(user_id=uuid.uuid4(), partner_ids=[uuid.uuid4()])
    assert not p.can_act_for_partner(uuid.uuid4())
