"""Mock order/payment APIs for the refund agent.

These simulate the external systems an e-commerce refund agent would talk to:
an order service (read-only) and a payment processor (money-moving!).

No real APIs are called. Latency is simulated with sleeps so the code behaves
like the real world (and so failures land mid-workflow, not instantly).
"""

import time

# ---------------------------------------------------------------------------
# Order API (simulated)
# ---------------------------------------------------------------------------

ORDERS = {
    "ORD-4821": {
        "order_id": "ORD-4821",
        "customer": "sarah@example.com",
        "item": "Headphones",
        "amount": 499.00,
        "status": "delivered",
        "refund_authorized": True,
    },
    "ORD-9999": {
        "order_id": "ORD-9999",
        "customer": "bob@example.com",
        "item": "Espresso machine",
        "amount": 1299.00,
        "status": "delivered",
        "refund_authorized": False,  # not eligible -> agent should reject
    },
}


def lookup_order(order_id: str) -> dict | None:
    """Fetch order details from the (mock) order service."""
    time.sleep(0.3)  # simulate network latency
    return ORDERS.get(order_id)


# ---------------------------------------------------------------------------
# Payment API (simulated) - this one MOVES MONEY, so bugs here hurt
# ---------------------------------------------------------------------------

# Refunds already issued: refund_id -> order_id. In a real system this
# state lives in the payment processor, not in your agent's memory.
ISSUED_REFUNDS: dict[str, str] = {}

_attempt_counter: dict[str, int] = {}


def issue_refund(order_id: str, amount: float, reason: str = "") -> dict:
    """Issue a refund at the (mock) payment processor.

    Deliberately made flaky for experiments: set REFUND_FAIL_ATTEMPTS to N
    and this call raises for the first N attempts before succeeding.
    """
    import os

    attempt = _attempt_counter.get(order_id, 0) + 1
    _attempt_counter[order_id] = attempt

    # --- experiment hook: simulate a flaky payment API ---------------------
    fail_until = int(os.environ.get("REFUND_FAIL_ATTEMPTS", "0"))
    if attempt <= fail_until:
        raise ConnectionError(
            f"Payment API timeout (attempt {attempt}/{fail_until}): "
            "connection reset while charging"
        )
    # -----------------------------------------------------------------------

    time.sleep(0.5)  # simulate payment network latency

    # Double-refund guard: only for the demo. The real processor would
    # happily charge you twice and you would find out from a customer tweet.
    if order_id in ISSUED_REFUNDS.values():
        print(f"  !! WARNING: refund for {order_id} was ALREADY issued - "
              f"this is a DOUBLE REFUND")

    refund_id = f"refund_{order_id}_{len(ISSUED_REFUNDS) + 1}"
    ISSUED_REFUNDS[refund_id] = order_id
    print(f"  $$ REAL SIDE EFFECT: refunded {amount:.2f} for {order_id} "
          f"-> {refund_id} (reason: {reason or 'n/a'})")
    return {"refund_id": refund_id, "amount": amount, "status": "succeeded"}
