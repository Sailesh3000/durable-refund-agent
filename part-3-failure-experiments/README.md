# Part 3 — Failure experiments

Two services prove what durable execution buys you. Both are deterministic
(no LLM) so the experiments are fast and reproducible.

| Service | Endpoint | What it shows |
|---|---|---|
| `tool_failure.py` | `FlakyRefundAgent/run` on :9081 | Automatic retries with exponential backoff for a failing tool |
| `crash_recovery.py` | `CrashDemo/process` on :9082 | Kill the process mid-flight, resume with zero lost work |

Start everything (from the repo root):

```bash
docker compose up -d --build
```

Register both experiment services (Restate is in Docker; services run in
their own containers on the compose network):

```bash
curl localhost:9070/deployments --json '{"uri": "http://tool-failure:9081"}'
curl localhost:9070/deployments --json '{"uri": "http://crash-demo:9082"}'
```

To run locally instead of Docker, serve the files directly
(`python tool_failure.py`) and register `http://host.docker.internal:9081`
/ `:9082`.

---

## Experiment 1 — flaky payment API

The mock `issue_refund` fails the first `FLAKY_FAIL_ATTEMPTS` times
(default 2) and then succeeds. The workflow code has **no** retry logic —
Restate retries the failed step with exponential backoff
(2s initial, doubling, capped at 6s, max 10 attempts).

```bash
curl localhost:8080/restate/call/FlakyRefundAgent/run --json '{"order_id": "ORD-4821"}'
```

> The mock keeps its failure counter in memory. Run
> `docker compose restart tool-failure` before each attempt, or a repeat call
> succeeds on the first try.

Service log (`docker compose logs tool-failure`, tracebacks trimmed — the SDK
logs one per failed attempt, which is expected):

```
13:25:47.19  workflow started for ORD-4821
13:25:47.70    [attempt 1] payment API FAILED (simulated)
13:25:50.01  workflow started for ORD-4821        <- retry after ~2s
13:25:50.31    [attempt 2] payment API FAILED (simulated)
13:25:55.01  workflow started for ORD-4821        <- retry after ~4s
13:25:55.32    [attempt 3] payment API OK -> refund issued
```

`workflow started` prints on every retry: Restate re-runs the handler from
the top and replays the journal. `lookup_order` is not called again — its
result comes from the journal — and only the failing step re-executes.

Final response:

```json
{"order_id": "ORD-4821", "refund": {"refund_id": "refund_ORD-4821", "attempts": 3}}
```

What to observe:

* The **call blocked ~6-8s** while Restate retried - the client just saw a
  slow request, never an error.
* Open http://localhost:9070 and find the invocation: the journal shows one
  `tool: issue_refund (flaky)` step with its retry history.
* Watch the invocation state flip `ready → backing-off → completed`:

```bash
curl -X POST localhost:9070/query -H 'content-type: application/json' \
  -H 'Accept: application/json' \
  -d '{"query": "SELECT id, status FROM sys_invocation WHERE target_service_name = '\''FlakyRefundAgent'\'' ORDER BY id DESC LIMIT 5"}'
```

Change the failure count for a longer retry storm:

```bash
FLAKY_FAIL_ATTEMPTS=6 docker compose up -d --force-recreate tool-failure
```

Set it higher than `max_attempts` (10) to see the workflow fail with a
terminal error after the retry budget is exhausted:

```bash
FLAKY_FAIL_ATTEMPTS=12 docker compose up -d --force-recreate tool-failure
curl localhost:8080/restate/call/FlakyRefundAgent/run --json '{"order_id": "ORD-4821"}'
```

After ~57s (waits of 2s, 4s, then 6s capped) the call returns:

```
{"code":500,"message":"ConnectionError('payment API timeout (attempt 10/12)')","source":"invocation"}
```

and `sys_invocation` shows `status: completed`, `completion_result: failure`
— Restate stops retrying. Restore the default afterwards:

```bash
docker compose up -d --force-recreate tool-failure
```

---

## Experiment 2 — kill it mid-flight

`CrashDemo/process` runs three 5-second steps. Kill the container while
step 2 is running, restart it, and watch the resume.

```bash
# fire-and-forget: returns an invocation id immediately
curl localhost:8080/restate/send/CrashDemo/process --json '{}'

sleep 7                # we are now ~2s into step 2
docker compose kill crash-demo
docker compose up -d crash-demo
docker compose logs -f crash-demo
```

Expected log across the kill:

```
=== handler invoked (fresh run OR resume after crash) ===
  EXECUTING step 1: prepare order
  EXECUTING step 2: issue refund          <- killed here
=== handler invoked (fresh run OR resume after crash) ===   <- resume
  EXECUTING step 2: issue refund          <- re-executed (was incomplete)
  EXECUTING step 3: confirm with customer
=== workflow completed ===
```

Note what is **missing** after the restart: step 1 does not print
`EXECUTING` again. Its result was replayed from the journal — the function
body never ran again. Only the incomplete step 2 was re-executed.

The journal in the UI (http://localhost:9070) shows all three steps with a
single final `Success` output, despite the process dying between them.

---

## Why this works

* Every `ctx.run_typed` call **appends a journal entry** to Restate's
  replicated log *before* the step is considered done.
* After a crash, the handler re-runs from the top; the SDK **replays**
  journal entries instead of re-executing their function bodies.
* Completed side effects (like the refund in step 2's real-world analogue)
  are never duplicated, because their journal entries already exist.
* A step that was **in flight** when the process died has no journal entry
  — so it re-executes. That is exactly the at-least-once semantics you
  want: the payment provider sees one refund, not two, not zero.
