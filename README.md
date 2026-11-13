# Durable Refund Agent — building an AI agent that never double-refunds

A customer-refund agent built twice: once as a naive Python agent, then again
on [Restate](https://restate.dev) so it survives crashes, retries flaky
payments, and pauses for human approval without losing its place.

The LLM runs locally via **Ollama** (default model: `qwen3:8b`). All order and
payment systems are mocked, so nothing here touches real money.

```
                 +---------------------------+
  curl / UI ---> |  Restate server :8080     |  durable log + retries
                 |  admin/UI       :9070     |
                 +------------+--------------+
                              | invokes handlers
        +---------------------+----------------------+
        |            |                |              |
   agent:9080  tool-failure:9081  crash-demo:9082  approval-agent:9083
   (part 2)    (part 3a)          (part 3b)        (part 4)
        |
        +--> Ollama :11434 (qwen3:8b, on the host)
```

## Prerequisites

* Docker with Compose (runs Restate + all four service containers)
* [Ollama](https://ollama.com) running on the host with a tool-capable model:
  `ollama pull qwen3:8b` (or set `OLLAMA_MODEL` to your model)
* Python 3.11+ only for Part 1 / the approval CLI (no extra packages needed)

## Quickstart

```bash
docker compose up -d --build

curl localhost:9070/deployments --json '{"uri": "http://agent:9080"}'
curl localhost:9070/deployments --json '{"uri": "http://tool-failure:9081"}'
curl localhost:9070/deployments --json '{"uri": "http://crash-demo:9082"}'
curl localhost:9070/deployments --json '{"uri": "http://approval-agent:9083"}'

curl localhost:8080/restate/call/RefundAgent/run --json '{"order_id": "ORD-4821"}'
```

UI + journals: http://localhost:9070 — expect ~75-95s per LLM-driven run
(three qwen3:8b round trips on CPU).

## Layout

| Path | Track |
|---|---|
| `part-1-basic-agent/` | Naive agent: LLM + tools in one process. Crashes lose everything; retries double-refund. |
| `part-2-restate-agent/` | Same agent as a Restate service: every LLM/tool call is a journaled durable step. |
| `part-3-failure-experiments/` | Flaky payment API (auto-retries) and kill-mid-flight crash recovery. |
| `part-4-human-approval/` | Manager approval via awakeables: suspend, approve/reject/timeout, resume. |
| `docker-compose.yml` | Restate server + all services (each in its own container). |

---

## Part 1 — the naive agent (watch it double-refund)

```bash
cd part-1-basic-agent
python main.py --order ORD-4821            # happy path
python main.py --order ORD-4821 --demo-crash --retries 2
```

The crash demo kills the workflow right after the payment provider confirmed
the refund, then re-runs it from scratch:

```
=== attempt 1 ===
[step 2] tool: issue_refund
  $$ REAL SIDE EFFECT: refunded 499.00 for ORD-4821 -> refund_ORD-4821_1
CRASHED mid-workflow: simulated process crash (OOM/deploy/kill -9)
=== attempt 2 ===
  !! WARNING: refund for ORD-4821 was ALREADY issued - this is a DOUBLE REFUND
  $$ REAL SIDE EFFECT: refunded 499.00 for ORD-4821 -> refund_ORD-4821_2
```

Everything (LLM calls, tool results, progress) lived in local memory. On
retry the agent re-runs lookup and refund — and pays the customer again.
`--llm mock` gives an instant deterministic demo; `--llm ollama` uses qwen3.

## Part 2 — the same agent on Restate

Run in Docker (recommended, fastest install — see note below):

```bash
curl localhost:8080/restate/call/RefundAgent/run --json '{"order_id": "ORD-4821"}'
```

The handler wraps every non-deterministic call in `ctx.run_typed`:

```python
decision = await ctx.run_typed("LLM decide (round 1)", call_llm, messages=messages)
result   = await ctx.run_typed("tool: issue_refund", issue_refund, order_id=..., amount=...)
```

Registry view of one completed invocation (SQL: `sys_journal`, via
`http://localhost:9070/query`):

```
index 1  Command: Run          "LLM decide (round 1)"
index 3  Command: Run          "tool: lookup_order"
index 5  Command: Run          "LLM decide (round 2)"
index 7  Command: Run          "tool: issue_refund"
index 9  Command: Run          "LLM decide (round 3)"
index 11 Command: Output       Success
```

Every LLM round trip (25-35s on CPU!) and every tool call is journaled.
Nothing is re-executed after a failure — journal entries replay instead.

> **It also runs natively:** `cd part-2-restate-agent && python main.py`, then
> register `http://host.docker.internal:9080`. Note: `restate-sdk` ships no
> Windows wheels — pip compiles the Rust core (~6 min, needs Rust), which is
> why Docker is the default here.

## Part 3 — failure experiments

See [part-3-failure-experiments/README.md](part-3-failure-experiments/README.md)
for the full script. Short version:

**Flaky payment API, automatic retries:**

```bash
curl localhost:8080/restate/call/FlakyRefundAgent/run --json '{"order_id": "ORD-4821"}'
# {"refund": {"attempts": 3, ...}} in ~8s — failures 1 and 2 were retried by Restate
```

**Kill mid-flight, resume with zero lost work:**

```bash
curl localhost:8080/restate/send/CrashDemo/process --json '{}'
sleep 7 && docker compose kill crash-demo && docker compose up -d crash-demo
docker compose logs -f crash-demo
```

```
=== handler invoked (fresh run OR resume after crash) ===
  EXECUTING step 1: prepare order
  EXECUTING step 2: issue refund          <- killed here
=== handler invoked (fresh run OR resume after crash) ===   <- resumed
  EXECUTING step 2: issue refund          <- only the incomplete step re-ran
  EXECUTING step 3: confirm with customer
=== workflow completed ===
```

## Part 4 — human approval that survives restarts

Refunds above `APPROVAL_THRESHOLD` (default 300) suspend on a durable
awakeable until a manager decides:

```bash
curl localhost:8080/restate/send/RefundApprovalAgent/run --json '{"order_id": "ORD-4821"}'
docker compose logs approval-agent        # prints the approval curl
```

```
MANAGER REVIEW REQUESTED: Refund approval needed: 499.00 for ORD-4821 ...
  curl localhost:8080/restate/awakeables/sign_.../resolve --json '"approve"'
```

Then (from repo root):

```bash
python part-4-human-approval/approval.py sign_... approve
python part-4-human-approval/approval.py sign_... reject "duplicate order"
python part-4-human-approval/approval.py sign_... reject "invalid" --hard   # terminal failure
```

* `approve` → refund issued, workflow completes
* `reject` → the reason is fed to the LLM for a polite decline
* `--hard` → raises a terminal error inside the agent (Restate `/reject` semantics)
* no answer for `APPROVAL_TIMEOUT_SECONDS` (default 3600) → escalated answer

The invocation **suspends** while waiting (visible in the UI: status
`suspended`) — no process is held. Kill anything, come back tomorrow, resolve
the awakeable: it resumes exactly where it stopped.

Try the timeout quickly:

```bash
APPROVAL_TIMEOUT_SECONDS=20 docker compose up -d --force-recreate approval-agent
```

## Production considerations

* **Idempotency** — the ingress supports `idempotency-key` headers; re-sending
  the same key returns the original result instead of re-running the agent.
  Inside handlers use `ctx.uuid()` for stable refund keys across retries.
* **Retry policies** — per-step control via `restate.RunOptions(...)`
  (attempts, backoff, max duration); terminal errors stop retries.
* **Observability** — full journals in the UI, `sys_journal` /
  `sys_invocation` SQL tables, OpenTelemetry trace export from the server.
* **Retention & limits** — `journal_retention` (default 1d), invocation
  retention, inactivity/abort timeouts, flow-control concurrency caps on
  expensive LLM calls.
* **Deploys** — Restate pins in-flight invocations to the deployment that
  started them; register a new deployment revision to roll forward safely.
* **State** — the dev compose keeps Restate's data inside the container.
  For anything real, mount a volume / use a cluster deployment.

## Reset

```bash
docker compose down                       # journals are ephemeral in this setup
docker compose up -d
```

## Troubleshooting

* **Agent answers "LLM unavailable…"** — the container cannot reach Ollama on
  the host. Check `docker compose logs agent | grep -i ollama`, and confirm
  `OLLAMA_BASE_URL` plus `host.docker.internal` (already in compose).
* **`warn: ... address already in use`** — stop anything holding 8080/9070/9080
  (an earlier standalone `restate_dev` container is a common culprit:
  `docker rm -f restate_dev`).
* **Slow first run** — qwen3:8b on CPU takes ~25-35s per LLM call; a 3-round
  refund takes ~90s. Set `OLLAMA_MODEL` to a smaller model to speed up demos.
