"""Task-8 boundary self-checks. Run from the worktree root with the competition venv."""

import asyncio

from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


def _result(texts):
    return EmbeddingResult(
        vectors=tuple((float(len(t)),) for t in texts),
        usage=InferenceUsage(requests=1, input_tokens=len(texts), output_tokens=0),
    )


class Base:
    profile = "profile-token"

    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        if set(texts) & self.fail_for:
            raise RuntimeError("boom")
        return _result(texts)


class Blocked(Base):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        await self.release.wait()
        return _result(texts)


def check(name, cond):
    assert cond, name
    print(f"ok - {name}")


def ctor_validation():
    for kwargs in ({"max_batch_size": 0}, {"max_batch_size": True}, {"max_batch_size": 1.5},
                   {"cache_size": -1}, {"coalesce_ms": -0.1}, {"coalesce_ms": float("nan")}):
        try:
            CoalescingEmbeddingModel(Base(), **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {kwargs}")
    check("constructor rejects invalid parameters", True)


async def scenarios():
    # 1) window coalescing: several queued dispatches merge into one full batch
    base = Base()
    model = CoalescingEmbeddingModel(base, max_batch_size=3, coalesce_ms=20, cache_size=0)
    results = await asyncio.gather(model.embed(("a",)), model.embed(("bb",)), model.embed(("ccc",)))
    check("window merges concurrent callers into one batch", base.calls == [("a", "bb", "ccc")])
    check("vectors keep per-call order", results[1].vectors == ((2.0,) ,) and results[2].vectors == ((3.0,),))
    check("cache_size=0 disables cross-call reuse", (await model.embed(("a",))) and len(base.calls) == 2)

    # 2) cache_size=0 still shares in-flight requests
    blocked = Blocked()
    model = CoalescingEmbeddingModel(blocked, coalesce_ms=0, cache_size=0)
    first = asyncio.create_task(model.embed(("slow",)))
    second = asyncio.create_task(model.embed(("slow", "slow")))
    await asyncio.sleep(0.05)
    blocked.release.set()
    r1, r2 = await asyncio.gather(first, second)
    check("in-flight sharing works with cache disabled", blocked.calls == [("slow",)])
    check("duplicate positions restored", r2.vectors == ((4.0,), (4.0,)))
    check("shared batch usage counted once per call", r1.usage.requests == 1 and r2.usage.requests == 1)
    check("token usage summed per distinct batch", r2.usage.input_tokens == 1)

    # 3) one failing batch poisons only its own texts; independent batch completes
    base = Base(fail_for={"bad"})
    model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0, cache_size=8)
    good_task = asyncio.create_task(model.embed(("good",)))
    try:
        await model.embed(("bad",))
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    check("independent batch survives sibling failure", (await good_task).vectors == ((4.0,),))
    try:
        await model.embed(("bad",))
        raise AssertionError("expected retry to fail again")
    except RuntimeError:
        pass
    check("failed text re-issued on retry", base.calls.count(("bad",)) == 2)

    # 4) cancelling every waiter keeps provider work alive and cache warm
    blocked = Blocked()
    model = CoalescingEmbeddingModel(blocked, coalesce_ms=0, cache_size=8)
    waiter = asyncio.create_task(model.embed(("kept",)))
    await asyncio.sleep(0.02)  # batch dispatched and blocked in provider
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    blocked.release.set()
    await asyncio.sleep(0.02)
    again = await model.embed(("kept",))
    check("all-waiter cancellation does not drop provider result", again.vectors == ((4.0,) ,) and len(blocked.calls) == 1)

    # 5) late caller reuses an already-dispatched request (in-flight or fresh cache)
    blocked = Blocked()
    model = CoalescingEmbeddingModel(blocked, coalesce_ms=0, cache_size=8)
    early = asyncio.create_task(model.embed(("late",)))
    await asyncio.sleep(0.02)
    blocked.release.set()
    late = asyncio.create_task(model.embed(("late",)))
    await asyncio.gather(early, late)
    check("late caller reuses in-flight request", blocked.calls == [("late",)])

    # 6) multi-batch usage aggregation and mixed cache hits
    base = Base()
    model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0, cache_size=8)
    res = await model.embed(("x", "y", "x", "z"))
    check("per-text dedup across small batches", sorted(t for c in base.calls for t in c) == ["x", "y", "z"])
    check("requests equal distinct awaited batches", res.usage.requests == 3)
    calls_before = len(base.calls)
    hit = await model.embed(("x", "x", "y", "w"))
    check("cached part adds no requests", hit.usage.requests == 1 and len(base.calls) == calls_before + 1)
    check("duplicate order restored from mixed sources", hit.vectors == ((1.0,), (1.0,), (1.0,), (1.0,)))
    empty = await model.embed(())
    check(
        "empty input returns zero usage without provider call",
        empty.usage.requests == 0 and empty.vectors == () and len(base.calls) == calls_before + 1,
    )


def event_loop_reuse():
    async def run():
        base = Base()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        return await model.embed(("loop-a",))

    check("sequential event loops reuse one instance", asyncio.run(run()) and asyncio.run(run()))


ctor_validation()
asyncio.run(scenarios())
event_loop_reuse()
print("ALL TASK-8 BOUNDARY CHECKS PASSED")
