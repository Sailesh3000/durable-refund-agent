"""The refund agent with manager approval for expensive refunds (Part 4).

Same agent as part 2 (Ollama/qwen decides, tools are durable steps), with
one new rule baked into the agent loop:

    issue_refund with amount > APPROVAL_THRESHOLD (default 300)
    -> ask a human manager, durably, and WAIT for the decision.

How the wait works (the interesting part):
  1. ctx.awakeable() creates a durable promise + a one-shot callback id
  2. we "notify" the manager (mock: log a Slack message with the id)
  3. the handler SUSPENDS - Restate releases the process; the invocation
     state lives in Restate, not in our RAM
  4. the manager resolves the awakeable over HTTP (see approval.py)
  5. the handler resumes exactly where it stopped and moves the money
     (or politely declines)

A timeout races the approval: restate.select(approval, ctx.sleep(T)).

Run:    python main.py            (serves RefundApprovalAgent on :9083)
Docker: docker compose up approval-agent
Register: curl localhost:9070/deployments --json '{"uri": "http://approval-agent:9083"}'
Invoke:   curl localhost:8080/restate/send/RefundApprovalAgent/run --json '{"order_id": "ORD-4821"}'
Approve:  python approval.py <awakeable_id> approve
"""

import asyncio
import json
import os
import sys
import urllib.request
from datetime import timedelta

import restate

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
APPROVAL_THRESHOLD = float(os.environ.get("APPROVAL_THRESHOLD", "300"))
APPROVAL_TIMEOUT = timedelta(
    seconds=int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "3600")))

REFUND_POLICY = """You are a customer refund agent for an online store.
Rules:
1. Always look up the order first (tool: lookup_order).
2. Issue a refund (tool: issue_refund) only if the order is refund_authorized.
3. Finish with a short final answer to the customer, no tool calls."""

TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "lookup_order",
        "description": "Look up an order by id",
        "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string"},
        }, "required": ["order_id"]},
    }},
    {"type": "function", "function": {
        "name": "issue_refund",
        "description": "Refund the full order amount",
        "parameters": {"type": "object", "properties": {
            "amount": {"type": "number"},
            "reason": {"type": "string"},
        }, "required": ["amount", "reason"]},
    }},
]

# ---------------------------------------------------------------------------
# Mock APIs (identical to parts 1/2, inlined so this folder is standalone)
# ---------------------------------------------------------------------------

_ORDERS = {
    "ORD-4821": {"order_id": "ORD-4821", "customer": "sarah@example.com",
                 "item": "Headphones", "amount": 499.00, "status": "delivered",
                 "refund_authorized": True},
    "ORD-1111": {"order_id": "ORD-1111", "customer": "amy@example.com",
                 "item": "Phone case", "amount": 89.00, "status": "delivered",
                 "refund_authorized": True},
    "ORD-9999": {"order_id": "ORD-9999", "customer": "bob@example.com",
                 "item": "Espresso machine", "amount": 1299.00,
                 "status": "delivered", "refund_authorized": False},
}

_ISSUED_REFUNDS: dict[str, str] = {}


async def lookup_order(order_id: str) -> dict:
    await asyncio.sleep(0.3)
    return _ORDERS.get(order_id, {"error": f"order {order_id} not found"})


async def issue_refund(order_id: str, amount: float, reason: str = "") -> dict:
    await asyncio.sleep(0.5)
    if order_id in _ISSUED_REFUNDS.values():
        print(f"  !! WARNING: refund for {order_id} was ALREADY issued - "
              f"this is a DOUBLE REFUND")
    refund_id = f"refund_{order_id}_{len(_ISSUED_REFUNDS) + 1}"
    _ISSUED_REFUNDS[refund_id] = order_id
    print(f"  $$ REAL SIDE EFFECT: refunded {amount:.2f} for {order_id} "
          f"-> {refund_id} (reason: {reason or 'n/a'})")
    return {"refund_id": refund_id, "amount": amount, "status": "succeeded"}


# ---------------------------------------------------------------------------
# Durable steps (plain functions, journaled by Restate)
# ---------------------------------------------------------------------------

def strip_think(text: str | None) -> str:
    """Remove qwen3's <think>...</think> thinking blocks from model output."""
    if not text:
        return ""
    while '<think>' in text and '</think>' in text:
        start = text.index('<think>')
        end = text.index('</think>') + len('</think>')
        text = text[:start] + text[end:]
    return text.strip()


async def call_llm(messages: list) -> dict:
    from openai import OpenAI

    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=300)
    resp = client.chat.completions.create(
        model=OLLAMA_MODEL, messages=messages, tools=TOOL_DEFS)
    msg = resp.choices[0].message
    if msg.tool_calls:
        return {"tool_calls": [
            {"name": tc.function.name,
             "args": json.loads(tc.function.arguments or "{}"),
             "id": tc.id}
            for tc in msg.tool_calls
        ]}
    return {"content": strip_think(msg.content)}


async def notify_manager(order_id: str, amount: float, reason: str,
                         approval_id: str) -> str:
    """Mock: this is where you'd post to Slack / Jira / email.
    The printed curl is all the 'manager' has to do."""
    message = (f"Refund approval needed: {amount:.2f} for {order_id} "
               f"(reason: {reason}). Approve or reject within "
               f"{APPROVAL_TIMEOUT.total_seconds():.0f}s.")
    print("\n" + "-" * 70)
    print("MANAGER REVIEW REQUESTED:", message)
    print("Approve:")
    print(f"  curl localhost:8080/restate/awakeables/{approval_id}/resolve "
          f"--json '\"approve\"'")
    print("Graceful reject (agent tells the customer):")
    print(f"  python approval.py {approval_id} reject \"reason\"")
    print("Hard reject (terminal failure inside the agent):")
    print(f"  python approval.py {approval_id} reject \"reason\" --hard")
    print("-" * 70 + "\n")
    return message


async def request_manager_approval(ctx: restate.Context, order_id: str,
                                   amount: float, reason: str) -> tuple[bool, str]:
    """Suspend until a human decides (or the timeout fires)."""
    approval_id, approval_promise = ctx.awakeable(type_hint=str)

    await ctx.run_typed("Request manager approval", notify_manager,
                        order_id=order_id, amount=amount, reason=reason,
                        approval_id=approval_id)

    match await restate.select(
        approval=approval_promise,
        timeout=ctx.sleep(APPROVAL_TIMEOUT),
    ):
        case ["approval", decision]:
            decision = (decision or "").strip().lower()
            if decision == "approve":
                return True, "approved"
            return False, decision.replace("reject:", "").strip() or "rejected"
        case _:
            return False, "approval timed out - escalated for manual review"


# ---------------------------------------------------------------------------
# The durable agent with an approval gate
# ---------------------------------------------------------------------------

refund_agent = restate.Service("RefundApprovalAgent")


@refund_agent.handler()
async def run(ctx: restate.Context, req: dict) -> dict:
    order_id = req["order_id"]
    query = req.get("query", f"I want a refund for order {order_id}.")
    print(f"Agent started for {order_id}")

    messages = [
        {"role": "system", "content": REFUND_POLICY},
        {"role": "user", "content": f"Customer: {query}"},
    ]
    step = 0
    nudges = 0
    looked_up = False
    while True:
        decision = await ctx.run_typed(
            f"LLM decide (round {step + 1})", call_llm, messages=messages)

        if "tool_calls" not in decision:
            # Small local models sometimes answer without using tools.
            # Nudge (bounded) if the order was never looked up.
            if not looked_up and nudges < 2:
                nudges += 1
                print(f"  LLM skipped tools - nudging ({nudges}/2)")
                messages.append({"role": "assistant",
                                 "content": decision.get("content") or ""})
                messages.append({"role": "system",
                                 "content": "You must call the lookup_order "
                                            "tool before answering."})
                continue
            answer = decision.get("content") or (
                "Your refund request has been received and is being processed.")
            return {"answer": answer, "order_id": order_id}

        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": c["id"], "type": "function",
                "function": {"name": c["name"],
                             "arguments": json.dumps(c["args"])},
            } for c in decision["tool_calls"]],
        })

        for call in decision["tool_calls"]:
            step += 1

            # ---- the approval gate ------------------------------------
            if (call["name"] == "issue_refund"
                    and call["args"].get("amount", 0) > APPROVAL_THRESHOLD):
                print(f"  {call['args']['amount']:.2f} > {APPROVAL_THRESHOLD:.0f} "
                      f"-> manager approval required")
                approved, reason = await request_manager_approval(
                    ctx, order_id, call["args"]["amount"],
                    call["args"].get("reason", ""))
                if not approved:
                    messages.append({
                        "role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps(
                            {"error": f"refund declined: {reason}"})})
                    continue
                print("  approved -> issuing refund")

            if call["name"] == "lookup_order":
                looked_up = True
                result = await ctx.run_typed(
                    f"tool: {call['name']}", lookup_order,
                    order_id=call["args"].get("order_id", order_id))
            else:
                result = await ctx.run_typed(
                    f"tool: {call['name']}", issue_refund,
                    order_id=order_id, amount=call["args"]["amount"],
                    reason=call["args"].get("reason", ""))

            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result)})


app = restate.app(services=[refund_agent])

if __name__ == "__main__":
    import hypercorn
    import hypercorn.asyncio
    import asyncio

    conf = hypercorn.Config()
    conf.bind = ["0.0.0.0:9083"]
    asyncio.run(hypercorn.asyncio.serve(app, conf))
