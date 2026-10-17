from app.state import CUSTOMER_CANCELLABLE, FLOWS, TERMINAL, can_transition


def test_pharmacy_happy_path():
    path = ["pending_payment", "placed", "accepted", "rx_verified", "packed", "dispatched", "delivered", "completed"]
    assert all(can_transition("pharmacy", a, b) for a, b in zip(path, path[1:]))


def test_lab_happy_path():
    path = ["pending_payment", "placed", "accepted", "scheduled", "sample_collected", "processing", "report_ready", "completed"]
    assert all(can_transition("lab", a, b) for a, b in zip(path, path[1:]))


def test_consultation_path_and_failure():
    assert can_transition("consultation", "placed", "booked")
    assert can_transition("consultation", "placed", "booking_failed")
    assert can_transition("consultation", "booked", "completed")


def test_no_skipping_or_going_backwards():
    assert not can_transition("pharmacy", "placed", "dispatched")
    assert not can_transition("pharmacy", "dispatched", "packed")
    assert not can_transition("lab", "processing", "completed")


def test_terminal_states_have_no_exits():
    for flow in FLOWS.values():
        assert not TERMINAL & set(flow)


def test_customer_can_only_cancel_before_partner_acts():
    assert CUSTOMER_CANCELLABLE == {"pending_payment", "placed"}
