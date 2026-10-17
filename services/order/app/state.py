"""Sub-order state machines. Fulfilment services drive these; the order service enforces them."""

TERMINAL = {"completed", "cancelled", "rejected", "refunded", "expired", "booking_failed"}

FLOWS: dict[str, dict[str, set[str]]] = {
    "pharmacy": {
        "pending_payment": {"placed", "cancelled", "expired"},
        "placed": {"accepted", "rejected", "cancelled"},
        "accepted": {"rx_verified", "packed", "rejected", "cancelled"},
        "rx_verified": {"packed", "cancelled"},
        "packed": {"dispatched", "ready_for_pickup"},
        "dispatched": {"delivered", "delivery_failed"},
        "delivery_failed": {"dispatched", "cancelled"},
        "ready_for_pickup": {"delivered", "cancelled"},
        "delivered": {"completed"},
    },
    "lab": {
        "pending_payment": {"placed", "cancelled", "expired"},
        "placed": {"accepted", "rejected", "cancelled"},
        "accepted": {"scheduled", "cancelled"},
        "scheduled": {"sample_collected", "scheduled", "cancelled"},
        "sample_collected": {"processing"},
        "processing": {"report_ready"},
        "report_ready": {"completed"},
    },
    "consultation": {
        "pending_payment": {"placed", "cancelled", "expired"},
        "placed": {"booked", "booking_failed", "cancelled"},
        "booked": {"completed", "cancelled", "no_show"},
        "no_show": {"completed"},
    },
}

# Cancelling here needs no partner involvement and triggers a full refund of the sub-order.
CUSTOMER_CANCELLABLE = {"pending_payment", "placed"}


def can_transition(kind: str, current: str, new: str) -> bool:
    return new in FLOWS[kind].get(current, set())
