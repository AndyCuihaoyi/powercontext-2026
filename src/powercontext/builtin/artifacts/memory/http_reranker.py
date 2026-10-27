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

"""HTTP cross-encoder reranking for coarse Memory search candidates."""

from __future__ import annotations

import math
from collections.abc import Mapping

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import (
    MemoryRerankDecision,
    _validate_bounds,
)
from powercontext.builtin.inference import InferenceUsage

_DEFAULT_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Score candidates with an HTTP service while preserving their global ranks.

    The service may impose a smaller per-request candidate limit than the coarse pool. Every
    batch carries the candidates' original one-based ranks, and the final selection orders the
    complete pool by service score before mapping the chosen ranks back to the caller's hits.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = _DEFAULT_POLICY_ID,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")  # noqa: TRY003
        self.policy_id = policy_id
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate once and select the best ranks from the whole pool."""

        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            expected_ranks = tuple(range(start + 1, start + len(batch) + 1))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text}
                        for rank, candidate in zip(expected_ranks, batch, strict=True)
                    ],
                },
            )
            requests += 1
            response.raise_for_status()
            scores.update(_parse_batch_scores(_decode_json(response), expected_ranks))
        selected_ranks = tuple(sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected_ranks,
            usage=InferenceUsage(requests=requests),
        )


def _decode_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as error:
        raise ValueError("rerank service returned invalid JSON") from error  # noqa: TRY003


def _parse_batch_scores(payload: object, expected_ranks: tuple[int, ...]) -> dict[int, float]:
    body = _strict_object(payload, {"scores"}, context="response")
    rows = body["scores"]
    if not isinstance(rows, list):
        raise ValueError("rerank response scores must be a list")  # noqa: TRY003
    expected = set(expected_ranks)
    scores: dict[int, float] = {}
    for row in rows:
        item = _strict_object(row, {"rank", "score"}, context="score")
        rank = item["rank"]
        if type(rank) is not int or rank not in expected:
            raise ValueError("rerank response rank is outside the batch")  # noqa: TRY003
        if rank in scores:
            raise ValueError("rerank response contains a duplicate rank")  # noqa: TRY003
        scores[rank] = _finite_score(item["score"])
    if scores.keys() != expected:
        raise ValueError("rerank response does not cover every batch rank")  # noqa: TRY003
    return scores


def _strict_object(value: object, keys: set[str], *, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"rerank {context} must contain exactly {sorted(keys)}")  # noqa: TRY003
    return value


def _finite_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("rerank score must be a number")  # noqa: TRY003
    try:
        score = float(value)
    except OverflowError as error:
        raise ValueError("rerank score must be finite") from error  # noqa: TRY003
    if not math.isfinite(score):
        raise ValueError("rerank score must be finite")  # noqa: TRY003
    return score


__all__ = ["HTTPMemoryReranker"]
