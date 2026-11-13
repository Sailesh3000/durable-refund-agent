"""EXPERIMENT 2: crash in the middle, resume without redoing work.

Three slow steps (5s each). Kill the service mid-flight, restart it,
and watch Restate:
  * replay completed steps from the journal (no re-execution, no
    "EXECUTING" log line), and
  * re-execute only the step that was in flight when the process died.

Run (locally):
    python crash_recovery.py          # serves CrashDemo on :9082

Trigger (fire-and-forget so the kill does not abort the HTTP call):
    curl localhost:8080/restate/send/CrashDemo/process --json '{}'
    sleep 7                            # now inside step 2
    docker compose kill crash-demo && docker compose up -d crash-demo
    docker compose logs -f crash-demo  # watch the resume
"""

import asyncio

import restate

service = restate.Service("CrashDemo")


async def slow_step(name: str, seconds: float) -> str:
    """Actually executing a step. After a crash + restart, completed
    steps are REPLAYED from the journal - this print never fires again."""
    print(f"  EXECUTING {name}")
    await asyncio.sleep(seconds)
    return f"{name} done"


@service.handler()
async def process(ctx: restate.Context, req: dict) -> dict:
    # The handler always re-runs "from the top" after a crash.
    # Completed steps skip their function body entirely: their result is
    # replayed from the journal Restate persisted.
    print("=== handler invoked (fresh run OR resume after crash) ===")

    step1 = await ctx.run_typed("step 1: prepare order", slow_step,
                                name="step 1: prepare order", seconds=5)
    step2 = await ctx.run_typed("step 2: issue refund", slow_step,
                                name="step 2: issue refund", seconds=5)
    step3 = await ctx.run_typed("step 3: confirm with customer", slow_step,
                                name="step 3: confirm with customer", seconds=5)

    print("=== workflow completed ===")
    return {"steps": [step1, step2, step3]}


app = restate.app(services=[service])

if __name__ == "__main__":
    import hypercorn
    import hypercorn.asyncio
    import asyncio

    conf = hypercorn.Config()
    conf.bind = ["0.0.0.0:9082"]
    asyncio.run(hypercorn.asyncio.serve(app, conf))
