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
_CONCLUSION = re.compile(
    r"\b(conclusions?|in (summary|conclusion|short)|we (show|demonstrate|find|report)|"
    r"these (results|data|findings|observations)|our (results|findings|data|study)|"
    r"taken together|collectively|overall|therefore|thus|hence|"
    r"(suggests?|demonstrates?|shows?|indicates?|reveals?|confirms?) that)\b"
)
_TREND = re.compile(
    r"\b(associat|correlat|significan|consistent with|supports?|supported|in contrast|"
    r"compared|predict|underli|mechanis|efficac|therapeut|dysregulat|implicat)\b"
)
_TREND_BONUS = 12.0
_K1 = 1.5
_B = 0.0
_CONCLUSION_BONUS = 24
_MICRO_SENTENCE_CHARS = 28
_MAX_PER_ENTRY = 10
_MAX_RUN_SENTENCES = 2
_MAX_RUN_GAP_CHARS = 4
_EXPANSION_TERMS = 12
_EXPANSION_SENTENCES = 3
_EXPANSION_WEIGHT = 0.5
_STOPWORDS = frozenset(
    """a an and are as at be been being but by can could did do does for from had has have
    how i in into is it its may might must of on or our shall should so than that the their
    them then there these they this those to was we were what when where which while who will
    with would you your""".split()
)


@dataclass(frozen=True)
class EvidenceCandidate:
    """One scored literal span ``text[start:end]`` inside one ranked entry.

    ``members`` carries the per-sentence ``(``start``, ``end``, ``score``)`` breakdown so
    budget-driven cuts inside the span can land on its least valuable sentence edge.
    """

    score: float
    entry_order: int
    start: int
    end: int
    members: tuple[tuple[int, int, float], ...] = ()

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.entry_order, self.start, self.end)


def tokenize(text: str) -> tuple[str, ...]:
    """Split into lowercase latin/digit words plus single CJK characters."""
    return tuple(_TOKEN.findall(text.lower()))


def _stem(token: str) -> str:
    """Crude suffix folding so plural/verb forms match across claim and evidence text."""
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _query_tokens(text: str) -> frozenset[str]:
    return frozenset(_stem(token) for token in tokenize(text) if token not in _STOPWORDS)


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentence spans that include their trailing terminator punctuation.

    Evidence annotations are whole sentences, so a selected span must reach through the
    final ``.``/``!``/``?`` (but not the separator whitespace) to cover them exactly.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    for match in _BOUNDARY.finditer(text):
        end = match.start()
        for index in range(match.end() - 1, match.start() - 1, -1):
            if not text[index].isspace():
                end = index + 1
                break
        if position < end:
            spans.append((position, end))
        position = match.end()
    if position < len(text):
        spans.append((position, len(text)))
    spans = [span for span in spans if span[0] < span[1]]
    # Decimal statistics ("7.5%", "P = .04") shred annotated sentences into micro-spans no
    # window could cover, so short fragments fold into their predecessor.
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and end - start < _MICRO_SENTENCE_CHARS:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    if len(merged) >= 2 and merged[0][1] - merged[0][0] < _MICRO_SENTENCE_CHARS:
        merged[1] = (merged[0][0], merged[1][1])
        merged.pop(0)
    return merged


def score_evidence(query: str, entry_texts: Sequence[str]) -> tuple[EvidenceCandidate, ...]:
    """Rank literal sentence spans inside each entry against one query, best first.

    Sentences are the unit of selection so every span maps back to an exact ``text[start:end]``
    slice. Beyond direct claim matches, the ranking runs one pseudo-relevance pass: the best
    matched sentences contribute extra terms (bridging claim/evidence vocabulary), and
    sentence extensions plus zero-score fill keep the byte budget useful when matches are
    sparse. Every span maps to literal text, and runs never cross a sentence boundary.
    """

    claim_tokens = _query_tokens(query)
    if not entry_texts:
        return ()
    per_entry: list[list[tuple[int, int, Counter[str]]]] = []
    entry_texts_list: list[str] = []
    lengths: list[int] = []
    document_frequency: Counter[str] = Counter()
    for text in entry_texts:
        entry_texts_list.append(text)
        sentences: list[tuple[int, int, Counter[str]]] = []
        for start, end in _sentence_spans(text):
            counts = Counter(_stem(token) for token in tokenize(text[start:end]) if token not in _STOPWORDS)
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
    # Discourse-marker prior: abstract conclusions carry the annotated finding far more
    # often than their claim-token overlap suggests, so they get a flat BM25 bonus.
    bonuses = [
        [_CONCLUSION_BONUS if _CONCLUSION.search(entry_texts_list[entry_order][start:end])
         else _TREND_BONUS if _TREND.search(entry_texts_list[entry_order][start:end]) else 0.0
         for start, end, _ in sentences]
        for entry_order, sentences in enumerate(per_entry)
    ]

    def idf_of(token: str) -> float:
        return math.log(1.0 + (sentence_count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))

    def bm25(weights: dict[str, float], counts: Counter[str], length: int) -> float:
        norm = _K1 * (1.0 - _B + _B * length / average_length)
        total = 0.0
        for token, weight in weights.items():
            if document_frequency[token] == 0:
                continue
            term_frequency = counts.get(token, 0)
            if term_frequency:
                total += weight * idf_of(token) * term_frequency * (_K1 + 1.0) / (term_frequency + norm)
        return total

    direct = {token: 1.0 for token in claim_tokens if document_frequency[token] > 0}
    if not direct:
        return ()
    scores = [
        [bm25(direct, counts, sum(counts.values())) + bonus for (_, _, counts), bonus in zip(sentences, bonuses[entry_order])]
        for entry_order, sentences in enumerate(per_entry)
    ]

    best: list[tuple[float, int, int]] = []
    for entry_order, entry_scores in enumerate(scores):
        for index, score in enumerate(entry_scores):
            if score > 0.0:
                best.append((score, entry_order, index))
    best.sort(reverse=True)
    if best:
        expansion: Counter[str] = Counter()
        for _, entry_order, index in best[:_EXPANSION_SENTENCES]:
            expansion.update(per_entry[entry_order][index][2])
        weights = dict(direct)
        for token in expansion.most_common(_EXPANSION_TERMS + len(direct)):
            if token in weights or document_frequency[token] == 0:
                continue
            if len(weights) >= len(direct) + _EXPANSION_TERMS:
                break
            weights[token] = _EXPANSION_WEIGHT
        if len(weights) > len(direct):
            scores = [
                [bm25(weights, counts, sum(counts.values())) + bonus for (_, _, counts), bonus in zip(sentences, bonuses[entry_order])]
                for entry_order, sentences in enumerate(per_entry)
            ]

    entry_direct_max = [max((bm25(direct, counts, sum(counts.values())) for _, _, counts in sentences), default=0.0) for sentences in per_entry]
    candidates: list[EvidenceCandidate] = []
    seen: set[tuple[int, int, int]] = set()

    def add(score: float, entry_order: int, start: int, end: int, members: tuple[tuple[int, int, float], ...] = ()) -> None:
        span = (entry_order, start, end)
        if span not in seen:
            seen.add(span)
            candidates.append(EvidenceCandidate(score=score, entry_order=entry_order, start=start, end=end, members=members))

    for entry_order, sentences in enumerate(per_entry):
        entry_scores = scores[entry_order]
        for index in range(len(sentences)):
            start, end, _ = sentences[index]
            add(entry_scores[index], entry_order, start, end, ((start, end, entry_scores[index]),))
        keepers = {index for index, score in enumerate(entry_scores) if score > 0.0}
        ranked = sorted(keepers, key=lambda index: (-entry_scores[index], index))[:_MAX_PER_ENTRY]
        for index in ranked:
            for size in range(2, _MAX_RUN_SENTENCES + 1):
                for first in range(max(0, index - size + 1), min(index + 1, len(sentences) - size + 1)):
                    window = range(first, first + size)
                    if index not in window:
                        continue
                    if any(
                        sentences[second][0] - sentences[second - 1][1] > _MAX_RUN_GAP_CHARS for second in window[1:]
                    ):
                        continue
                    add(
                        sum(entry_scores[member] for member in window),
                        entry_order,
                        sentences[first][0],
                        sentences[first + size - 1][1],
                        tuple(
                            (sentences[member][0], sentences[member][1], entry_scores[member])
                            for member in window
                        ),
                    )
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -entry_direct_max[candidate.entry_order],
                -candidate.score,
                candidate.start - candidate.end,
                candidate.start,
                candidate.entry_order,
            ),
        )
    )
