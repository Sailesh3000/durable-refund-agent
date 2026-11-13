"""The refund agent - now running under Restate.

Same agent logic as part 1 (Ollama/qwen decides, tools execute), but the
handler is a Restate service and every non-deterministic call is wrapped
in ctx.run_typed("step name", fn, ...):

  * each LLM call -> journaled decision
  * each tool call -> journaled result
  * the whole handler re-executes from the top on crash, replays journal
    entries instead of re-running completed steps

Run the service:
    python main.py

Register with Restate (Restate in Docker, service on host):
    curl localhost:9070/deployments --json '{"uri": "http://host.docker.internal:9080"}'

Invoke it:
    curl localhost:8080/restate/call/RefundAgent/run --json '{"order_id": "ORD-4821"}'
"""

import json
import os
import sys
import urllib.request

import restate

import tools

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

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
# Durable steps: plain (async) functions wrapped by ctx.run_typed
# ---------------------------------------------------------------------------

async def call_llm(messages: list) -> dict:
    """One LLM round trip. Journal entry: the decision dict.

    Errors (Ollama down, timeout) propagate on purpose: Restate retries
    the step with backoff instead of journaling a degraded answer.
    """
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
    return {"content": msg.content}


async def run_tool(name: str, args: dict, order_id: str) -> dict:
    """One tool call. Journal entry: the tool result."""
    if name == "lookup_order":
        result = await tools.lookup_order(args.get("order_id", order_id))
        return result or {"error": f"order {order_id} not found"}
    return await tools.issue_refund(order_id, args["amount"],
                                    args.get("reason", ""))


# ---------------------------------------------------------------------------
# The durable agent
# ---------------------------------------------------------------------------

refund_agent = restate.Service("RefundAgent")


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
    while True:
        # LLM call is a durable step: journaled, never re-executed on replay
        decision = await ctx.run_typed(
            f"LLM decide (round {step + 1})", call_llm, messages=messages)
        print(f"  decision: {json.dumps(decision)[:120]}")

        if "tool_calls" not in decision:
            return {"answer": decision["content"], "order_id": order_id}

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
            result = await ctx.run_typed(
                f"tool: {call['name']}", run_tool,
                name=call["name"], args=call["args"], order_id=order_id)
            print(f"  {call['name']} -> {json.dumps(result)[:120]}")
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result)})


app = restate.app(services=[refund_agent])

if __name__ == "__main__":
    import hypercorn
    import hypercorn.asyncio
    import asyncio

    conf = hypercorn.Config()
    conf.bind = ["0.0.0.0:9080"]
    asyncio.run(hypercorn.asyncio.serve(app, conf))
