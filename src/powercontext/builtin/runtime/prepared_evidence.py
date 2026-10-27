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

"""Pure lexical ranking of literal entry spans for query-driven evidence selection."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

_TOKEN = re.compile(r"[a-z0-9]+|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_BOUNDARY = re.compile(r"[.!?。！？;；:\n]+")
_K1 = 1.5
_B = 0.75
_MAX_PER_ENTRY = 8
_MAX_RUN_SENTENCES = 3
_MAX_RUN_GAP_CHARS = 4


@dataclass(frozen=True)
class EvidenceCandidate:
    """One scored literal span ``text[start:end]`` inside one ranked entry."""

    score: float
    entry_order: int
    start: int
    end: int

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.entry_order, self.start, self.end)


def tokenize(text: str) -> tuple[str, ...]:
    """Split into lowercase latin/digit words plus single CJK characters."""
    return tuple(_TOKEN.findall(text.lower()))


def _tight(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    position = 0
    for match in _BOUNDARY.finditer(text):
        spans.append(_tight(text, position, match.start()))
        position = match.end()
    if position < len(text):
        spans.append(_tight(text, position, len(text)))
    return [span for span in spans if span[0] < span[1]]


def score_evidence(query: str, entry_texts: Sequence[str]) -> tuple[EvidenceCandidate, ...]:
    """Rank literal sentence spans inside each entry against one query, best first.

    Sentences are the unit of selection so every span maps back to an exact ``text[start:end]``
    slice. Sentences adjacent in the source also yield merged runs up to three long because a
    cited passage usually spans consecutive sentences; run scores are the BM25 sums of members.
    """

    query_tokens = set(tokenize(query))
    if not query_tokens or not entry_texts:
        return ()

    per_entry: list[list[tuple[int, int, Counter[str]]]] = []
    lengths: list[int] = []
    document_frequency: Counter[str] = Counter()
    for text in entry_texts:
        sentences: list[tuple[int, int, Counter[str]]] = []
        for start, end in _sentence_spans(text):
            counts = Counter(tokenize(text[start:end]))
            if not counts:
                continue
            sentences.append((start, end, counts))
            document_frequency.update(counts.keys())
            lengths.append(sum(counts.values()))
        per_entry.append(sentences)
    sentence_count = len(lengths)
    if sentence_count == 0:
        return ()
    average_length = sum(lengths) / sentence_count

    idf = {
        token: math.log(1.0 + (sentence_count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
        for token in query_tokens
        if document_frequency[token] > 0
    }
    if not idf:
        return ()

    candidates: list[EvidenceCandidate] = []
    for entry_order, sentences in enumerate(per_entry):
        scores: list[float] = []
        for _, _, counts in sentences:
            length = sum(counts.values())
            norm = _K1 * (1.0 - _B + _B * length / average_length)
            total = 0.0
            for token, weight in idf.items():
                term_frequency = counts.get(token, 0)
                if term_frequency:
                    total += weight * term_frequency * (_K1 + 1.0) / (term_frequency + norm)
            scores.append(total)
        ranked = sorted(
            (index for index in range(len(sentences)) if scores[index] > 0.0),
            key=lambda index: -scores[index],
        )[
            : _MAX_PER_ENTRY
        ]
        keepers = set(ranked)
        for index in ranked:
            start, end, _ = sentences[index]
            candidates.append(EvidenceCandidate(score=scores[index], entry_order=entry_order, start=start, end=end))
        for size in range(2, _MAX_RUN_SENTENCES + 1):
            for first in range(len(sentences) - size + 1):
                window = range(first, first + size)
                if not set(window) <= keepers:
                    continue
                if any(
                    sentences[second][0] - sentences[second - 1][1] > _MAX_RUN_GAP_CHARS for second in window[1:]
                ):
                    continue
                start = sentences[first][0]
                end = sentences[first + size - 1][1]
                candidates.append(
                    EvidenceCandidate(score=sum(scores[index] for index in window), entry_order=entry_order, start=start, end=end)
                )

    return tuple(sorted(candidates, key=lambda candidate: (-candidate.score, candidate.key)))
