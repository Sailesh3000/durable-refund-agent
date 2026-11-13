"""A refund agent - without Restate.

Flow:
    customer request -> LLM decides tool calls -> lookup_order
                     -> issue_refund -> final answer to the customer

The LLM runs locally on Ollama (default: qwen3:8b, see OLLAMA_MODEL).
If Ollama is not reachable, a deterministic mock LLM takes over so the
demo stays fully reproducible offline.

Run the happy path:
    python main.py --order ORD-4821

Run the FAILURE DEMO (crash after the refund went out, then retry):
    python main.py --order ORD-4821 --demo-crash --retries 2
"""

import argparse
import json
import sys
import urllib.request

import tools

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# LLM configuration (Ollama serves an OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------

import os

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


def strip_think(text: str | None) -> str | None:
    """Remove qwen3's <think> blocks from model output."""
    if not text:
        return text
    while "<think>" in text and "</think>" in text:
        start, end = text.index("<think>"), text.index("</think>") + len("</think>")
        text = text[:start] + text[end:]
    return text.strip()


# ---------------------------------------------------------------------------
# Two LLM backends behind one interface:
#   decide(messages) ->
#       {"content": "final answer"} |
#       {"tool_calls": [{"name", "args", "id"}]}
# ---------------------------------------------------------------------------

def decide_ollama(messages):
    from openai import OpenAI  # imported lazily: only needed in this mode

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


def decide_mock(messages):
    """Deterministic stand-in that follows the same policy."""
    last = messages[-1]
    content = last.get("content", "")
    if last["role"] == "user":
        return {"tool_calls": [{"name": "lookup_order",
                                "args": {}, "id": "call_1"}]}
    if "error" in content:
        return {"content": "I could not find that order."}
    if "refund_id" not in content:
        order = json.loads(content)
        if order["refund_authorized"]:
            return {"tool_calls": [{"name": "issue_refund",
                                    "args": {"amount": order["amount"],
                                             "reason": "customer requested refund"},
                                    "id": "call_2"}]}
        return {"content": "This order is not eligible for a refund."}
    refund = json.loads(content)
    return {"content": (f"Your refund of {refund['amount']:.2f} was issued. "
                        f"Refund id: {refund['refund_id']}")}


def decide(messages, llm):
    if llm == "ollama":
        try:
            return decide_ollama(messages)
        except Exception as e:  # Ollama down -> fall back to mock
            print(f"  (Ollama unreachable: {e.__class__.__name__} - using mock)")
            llm = "mock"
    return decide_mock(messages)


# ---------------------------------------------------------------------------
# The agent loop. NOTE: everything here lives in local memory.
# If the process dies, all of this is gone - that is the whole point.
# ---------------------------------------------------------------------------

def execute_tool(name, args, order_id) -> dict:
    if name == "lookup_order":
        return tools.lookup_order(args.get("order_id", order_id)) \
            or {"error": f"order {order_id} not found"}
    return tools.issue_refund(order_id, args["amount"], args.get("reason", ""))


def run_refund_workflow(order_id: str, query: str, llm: str,
                        crash_before_confirm: bool) -> str:
    """One attempt of the refund workflow, inside ONE process."""
    messages = [
        {"role": "system", "content": REFUND_POLICY},
        {"role": "user", "content": f"Customer: {query}"},
    ]
    step = 0
    while True:
        decision = decide(messages, llm)

        if "tool_calls" not in decision:
            return decision["content"]

        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"],
                             "arguments": json.dumps(c["args"])},
            } for c in decision["tool_calls"]],
        })
        for call in decision["tool_calls"]:
            step += 1
            print(f"[step {step}] tool: {call['name']}({call['args']})")
            result = execute_tool(call["name"], call["args"], order_id)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result)})

            if call["name"] == "issue_refund" and crash_before_confirm:
                # The refund went out to the payment provider... and NOW we die.
                # The customer will love the second refund. We won't.
                raise RuntimeError("simulated process crash (OOM/deploy/kill -9)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", default="ORD-4821")
    ap.add_argument("--llm", choices=["auto", "ollama", "mock"], default="auto")
    ap.add_argument("--demo-crash", action="store_true",
                    help="crash right after the refund was issued")
    ap.add_argument("--retries", type=int, default=0,
                    help="how many times to re-run after a crash")
    args = ap.parse_args()

    query = f"I want a refund for order {args.order}, it arrived damaged."
    if args.llm == "auto":
        args.llm = "ollama" if _ollama_reachable() else "mock"

    for attempt in range(1, args.retries + 2):
        print(f"\n=== attempt {attempt} (llm={args.llm}) ===")
        try:
            final = run_refund_workflow(args.order, query, args.llm,
                                        args.demo_crash)
            print(f"\nAgent final answer: {final}")
            return
        except RuntimeError as e:
            print(f"\nCRASHED mid-workflow: {e}")
            if attempt > args.retries:
                print("Out of retries - workflow never completed.")
                break

    print("\nPayments issued during this run:", list(tools.ISSUED_REFUNDS))
    sys.exit(1)


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(
            OLLAMA_BASE_URL.replace("/v1", "") + "/api/tags", timeout=2)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    main()
