"""EXPERIMENT 1: automatic retries for flaky tools.

The payment API fails for the first FLAKY_FAIL_ATTEMPTS attempts
(default: 2). Restate retries the failed step automatically with
exponential backoff - the workflow itself just writes straight-line code.

Run (locally):
    python tool_failure.py            # serves FlakyRefundAgent on :9081

Run (in Docker): see docker-compose.yml in the repo root.

Trigger the experiment:
    curl localhost:8080/restate/call/FlakyRefundAgent/run --json '{"order_id": "ORD-4821"}'

Watch retries in the UI (http://localhost:9070) or via SQL:
    curl -X POST localhost:9070/query -H 'content-type: application/json' \
      -H 'Accept: application/json' \
      -d '{"query": "SELECT id, status FROM sys_invocation ORDER BY id DESC LIMIT 5"}'
"""

import asyncio
import os
from datetime import timedelta

import restate

service = restate.Service("FlakyRefundAgent")

# ---------------------------------------------------------------------------
# Mock APIs (self-contained, deterministic, no LLM needed)
# ---------------------------------------------------------------------------

_ORDERS = {
    "ORD-4821": {"order_id": "ORD-4821", "item": "Headphones",
                 "amount": 499.00, "refund_authorized": True},
}

_attempt_counter: dict[str, int] = {}


async def lookup_order(order_id: str) -> dict:
    await asyncio.sleep(0.2)
    return _ORDERS.get(order_id, {"error": "not found"})


async def flaky_issue_refund(order_id: str, amount: float) -> dict:
    """Fails FLAKY_FAIL_ATTEMPTS times, then succeeds."""
    attempt = _attempt_counter.get(order_id, 0) + 1
    _attempt_counter[order_id] = attempt

    fail_until = int(os.environ.get("FLAKY_FAIL_ATTEMPTS", "2"))
    await asyncio.sleep(0.3)

    if attempt <= fail_until:
        print(f"  [attempt {attempt}] payment API FAILED (simulated)")
        raise ConnectionError(f"payment API timeout (attempt {attempt}/{fail_until})")

    print(f"  [attempt {attempt}] payment API OK -> refund issued")
    return {"refund_id": f"refund_{order_id}", "amount": amount, "attempts": attempt}


# ---------------------------------------------------------------------------
# The workflow: look up, refund, return. No retry code anywhere.
# ---------------------------------------------------------------------------

@service.handler()
async def run(ctx: restate.Context, req: dict) -> dict:
    order_id = req.get("order_id", "ORD-4821")
    print(f"workflow started for {order_id}")

    order = await ctx.run_typed("tool: lookup_order", lookup_order,
                                order_id=order_id)

    # Custom retry policy on this step: Restate handles the backoff loop,
    # the workflow code is oblivious to failures.
    refund = await ctx.run_typed(
        "tool: issue_refund (flaky)",
        flaky_issue_refund,
        restate.RunOptions(
            initial_retry_interval=timedelta(seconds=2),
            retry_interval_factor=2.0,
            max_retry_interval=timedelta(seconds=6),
            max_attempts=10,
        ),
        order_id=order_id,
        amount=order["amount"],
    )

    return {"order_id": order_id, "refund": refund}


app = restate.app(services=[service])

if __name__ == "__main__":
    import hypercorn
    import hypercorn.asyncio
    import asyncio

    conf = hypercorn.Config()
    conf.bind = ["0.0.0.0:9081"]
    asyncio.run(hypercorn.asyncio.serve(app, conf))
