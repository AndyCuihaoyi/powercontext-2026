# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cross-call request coalescing and bounded vector cache for embedding models."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
    from powercontext.builtin.inference.models import EmbeddingVector
    from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass
class _BatchJob:
    """One shared embedding request covering a bounded set of distinct texts."""

    texts: list[str]
    future: asyncio.Future[_BatchJob]
    dispatched: bool = False
    vectors: dict[str, EmbeddingVector] | None = None
    usage: InferenceUsage | None = None
    error: BaseException | None = None
    timer: asyncio.TimerHandle | None = None


class CoalescingEmbeddingModel:
    """Wrap an embedding model so concurrent calls share bounded provider work.

    A text requested by several calls inside the coalescing window, or while its request is
    still executing, is embedded once. Successful vectors are reused from a bounded LRU
    cache while each call still receives vectors in its own original order and duplicate
    positions. A call's usage aggregates every distinct batch it waited on; purely cached
    texts add no requests. Cancelling one call never detaches the shared request from the
    others, and a failed batch is retried instead of cached.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")
        if (
            isinstance(coalesce_ms, bool)
            or not isinstance(coalesce_ms, (int, float))
            or not math.isfinite(coalesce_ms)
            or coalesce_ms < 0
        ):
            raise ValueError("coalesce_ms must be a finite non-negative number")
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_ms = float(coalesce_ms)
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._lock = asyncio.Lock()
        self._jobs: dict[str, _BatchJob] = {}
        self._open_job: _BatchJob | None = None

    @property
    def profile(self) -> EmbeddingProfile:
        """Expose the wrapped model's embedding profile unchanged."""
        return self._model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed texts through shared batches and the vector cache, preserving input order."""
        ordered = tuple(texts)
        if not ordered:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        cached: dict[str, EmbeddingVector] = {}
        jobs: dict[str, _BatchJob] = {}
        async with self._lock:
            for text in dict.fromkeys(ordered):
                vector = self._cache.get(text)
                if vector is not None:
                    self._cache.move_to_end(text)
                    cached[text] = vector
                    continue
                job = self._jobs.get(text)
                if job is None:
                    job = self._attach(text)
                jobs[text] = job

        awaited: list[_BatchJob] = []
        seen: set[int] = set()
        for job in jobs.values():
            if id(job) in seen:
                continue
            seen.add(id(job))
            # Shield the shared future: Task.cancel() otherwise cancels the awaited future
            # itself, which would break the other waiters of this batch.
            awaited.append(await asyncio.shield(job.future))

        vectors_by_text = dict(cached)
        error: BaseException | None = None
        for job in awaited:
            if job.error is not None:
                if error is None:
                    error = job.error
            elif job.vectors is not None:
                vectors_by_text.update(job.vectors)
        if error is not None:
            raise error
        return EmbeddingResult(
            vectors=tuple(vectors_by_text[text] for text in ordered),
            usage=self._aggregate_usage(awaited),
        )

    def _attach(self, text: str) -> _BatchJob:
        job = self._open_job
        if job is not None and not job.dispatched and len(job.texts) < self._max_batch_size:
            job.texts.append(text)
            self._jobs[text] = job
            if len(job.texts) >= self._max_batch_size:
                self._dispatch_job(job)
            return job
        return self._start_job(text)

    def _start_job(self, text: str) -> _BatchJob:
        loop = asyncio.get_running_loop()
        job = _BatchJob(texts=[text], future=loop.create_future())
        self._jobs[text] = job
        self._open_job = job
        if self._coalesce_ms > 0:
            job.timer = loop.call_later(self._coalesce_ms / 1000.0, self._dispatch_job, job)
        else:
            loop.call_soon(self._dispatch_job, job)
        return job

    def _dispatch_job(self, job: _BatchJob) -> None:
        if job.dispatched:
            return
        job.dispatched = True
        if self._open_job is job:
            self._open_job = None
        if job.timer is not None:
            job.timer.cancel()
            job.timer = None
        task = asyncio.get_running_loop().create_task(self._model.embed(tuple(job.texts)))
        task.add_done_callback(lambda finished: self._finish_job(job, finished))

    def _finish_job(self, job: _BatchJob, task: asyncio.Task[EmbeddingResult]) -> None:
        try:
            result = task.result()
        except BaseException as exc:
            job.error = exc
        else:
            if len(result.vectors) != len(job.texts):
                job.error = ValueError("embedding provider returned an unexpected vector count")
            else:
                job.vectors = dict(zip(job.texts, result.vectors, strict=True))
                job.usage = result.usage
        for text in job.texts:
            if self._jobs.get(text) is job:
                del self._jobs[text]
        if job.vectors is not None and self._cache_size > 0:
            for text, vector in job.vectors.items():
                self._cache[text] = vector
                self._cache.move_to_end(text)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        if not job.future.done():
            job.future.set_result(job)

    def _aggregate_usage(self, jobs: list[_BatchJob]) -> InferenceUsage:
        requests = 0
        input_tokens: int | None = None
        output_tokens: int | None = None
        for job in jobs:
            usage = job.usage
            if usage is None:
                continue
            requests += usage.requests
            if usage.input_tokens is not None:
                input_tokens = (input_tokens or 0) + usage.input_tokens
            if usage.output_tokens is not None:
                output_tokens = (output_tokens or 0) + usage.output_tokens
        return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)
