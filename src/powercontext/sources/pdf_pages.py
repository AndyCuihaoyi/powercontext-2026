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

"""Complete PDF page capture with page-level textual evidence retrieval."""

from __future__ import annotations

import hashlib
import io
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, unquote

from pydantic import BaseModel
from pypdf import PasswordType, PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.artifacts import MemoryCitation
    from powercontext.builtin.runtime import ScopedMemoryApplication

PDF_PAGE_SOURCE_NAME: Final = "pdf-page"
PDF_PAGE_SOURCE_VERSION: Final = "1"
PDF_PAGE_IDENTITY_PREFIX: Final = "pdf"

_SHA256_HEX_LENGTH = 64
_TOKEN = re.compile(r"[a-z]+|\d+")

_NOISE: Final = frozenset("""
a about above after again against all also am an and any are as at be because been before being below based
between both but by can could did do does doing down during each few following for from further had has have
having he her here hers herself him himself his how i if in into is it its itself just like me more most my
myself no nor not now of off on once only or other our ours ourselves out over own same she should so some such
than that the their theirs them themselves then there these they this those through to too under until up us
very was we were what when where which while who whom why will with would you your yours yourself yourselves
answer assume assuming calculate calculated compute computed define detail details give mention perspective
please provide question rely relying respond response say shall show shown state tell explain explanation
drove drive driven metric measure useful relevant fiscal fy year annual report form
done doing does did major minor key main mainly primarily significant significantly materially material
company companies business businesses amount amounts number numbers value values total percent percentage
period periods end ended name named list identify determine describe disclose disclosed include included including
report reported relate related regarding concerning increase increased increases decrease decreased decreases
much many able unable represent represents representing exclude excluding during
primary primarily
""".split())

# Generic finance vocabulary aliases, used only for offline query expansion.
_SYNONYMS: Final[dict[str, tuple[str, ...]]] = {
    "acquisition": ("merger", "divestiture", "purchase"),
    "amortization": ("amortisation", "depreciation"),
    "borrowing": ("debt", "loan"),
    "buyback": ("repurchase",),
    "capex": ("capital", "expenditure", "property", "plant", "equipment"),
    "cash": ("liquid",),
    "category": ("segment", "class", "type", "line", "product", "service"),
    "contingency": ("litigation", "legal", "claim"),
    "country": ("region", "market", "geography", "territory"),
    "customer": ("client", "buyer", "retailer", "distributor", "government", "contract", "airline", "commercial"),
    "debt": ("borrowing", "loan", "obligation"),
    "dividend": ("distribution",),
    "ebitda": ("operating", "income", "profit", "depreciation", "amortization"),
    "employee": ("headcount", "personnel"),
    "expenditure": ("spending", "capital"),
    "geographic": (
        "region",
        "market",
        "country",
        "territory",
        "international",
        "worldwide",
        "africa",
        "asia",
        "europe",
        "america",
        "pacific",
        "latin",
    ),
    "geography": (
        "region",
        "market",
        "country",
        "territory",
        "segment",
        "international",
        "worldwide",
        "africa",
        "asia",
        "europe",
        "america",
        "pacific",
        "latin",
    ),
    "growth": ("sales", "change", "organic", "increase"),
    "income": ("profit", "earnings"),
    "industry": ("sector", "business", "segment", "market", "product"),
    "inventory": ("stock",),
    "lawsuit": ("litigation", "proceeding", "claim", "battle", "settlement"),
    "lease": ("rental",),
    "legal": ("litigation", "proceeding", "lawsuit", "claim", "contingency", "settlement"),
    "liquidity": ("cash", "current", "asset", "liability"),
    "litigation": ("legal", "proceeding", "lawsuit", "claim", "settlement"),
    "margin": ("profitability", "profit"),
    "merger": ("acquisition",),
    "pension": ("retirement", "benefit"),
    "profit": ("income", "earnings"),
    "receivable": ("debtor",),
    "region": ("geography", "market", "country"),
    "repurchase": ("buyback",),
    "restructuring": ("impairment", "charge", "severance", "productivity"),
    "retain": ("retention", "keep"),
    "retention": ("retain", "keep"),
    "revenue": ("sales",),
    "risk": ("uncertainty", "exposure"),
    "sales": ("revenue",),
    "segment": ("division",),
    "tax": ("taxation", "provision"),
}

_PHRASE_ALIASES: Final[dict[tuple[str, ...], tuple[str, ...]]] = {
    ("balance", "sheet"): ("balance", "sheet", "asset", "liability"),
    ("business", "segment"): ("segment", "division"),
    ("capital", "intensive"): ("capital", "expenditure", "property", "plant", "equipment", "depreciation", "capex"),
    ("cash", "flow"): ("cash", "flow", "operating", "investing", "financing"),
    ("current", "ratio"): ("current", "asset", "liability"),
    ("customer", "base"): ("customer", "client"),
    ("effective", "tax", "rate"): ("tax", "provision", "income", "pretax"),
    ("free", "cash", "flow"): ("cash", "flow", "operating", "capital", "expenditure"),
    ("gross", "margin"): ("gross", "profit", "sales", "revenue", "cost"),
    ("income", "statement"): ("statement", "income", "operation", "profit"),
    ("operating", "margin"): ("operating", "income", "sales", "revenue"),
    ("quick", "ratio"): ("cash", "receivable", "inventory", "current", "asset", "liability"),
    ("sales", "growth"): ("sales", "growth", "change", "organic"),
    ("total", "debt"): ("debt", "borrowing", "loan", "obligation"),
}

_NORMALIZATIONS: Final = (
    (re.compile(r"\bm\s*&\s*a\b", re.IGNORECASE), " acquisition "),
    (re.compile(r"\br\s*&\s*d\b", re.IGNORECASE), " research development "),
    (re.compile(r"\bp\s*&\s*l\b", re.IGNORECASE), " income statement "),
    (re.compile(r"\byoy\b", re.IGNORECASE), " year over year "),
)

_SYNONYM_WEIGHT = 0.8
_COORDINATION_BONUS = 0.7
_COVERAGE_BONUS = 0.3
_HEADING_BONUS = 0.3
_HEADING_TOKENS = 80
_BM25_K1 = 1.2
_BM25_B = 0.75
_IDF_FLOOR = 0.5
_MAX_QUERY_TERMS = 14
_WINDOW_TOKENS = 80
_WINDOW_STRIDE = 40
_SPREAD_WEIGHT = 0.75
_MIN_STATEMENT_LIMIT = 4
_MAX_STATEMENT_PAGES = 6
_STATEMENT_DOC_RATIO = 0.9
_CACHE_LIMIT = 20_000

_TITLE_PREFIX: Final = re.compile(
    r"^Consolidated?\s+(?:statements?\s+of\s+"
    r"(?:income|operations|comprehensive\s+(?:income|loss)|cash\s+flows?|changes?\s+in\s+"
    r"(?:stockholders?|shareholders?)?\s*equity|equity|financial\s+position)"
    r"|balance\s+sheets?)\b",
    re.IGNORECASE,
)
_TITLE_PATTERNS: Final = (
    (
        "income",
        re.compile(
            r"statements?\s+of\s+(?:income|operations|earnings)|statements?\s+of\s+(?:profit|loss)",
            re.IGNORECASE,
        ),
    ),
    ("balance", re.compile(r"balance\s+sheets?|statements?\s+of\s+financial\s+position", re.IGNORECASE)),
    ("cash", re.compile(r"statements?\s+of\s+cash\s+flows?", re.IGNORECASE)),
    (
        "equity",
        re.compile(r"statements?\s+of\s+(?:changes?\s+in\s+)?(?:stockholders?|shareholders?)?\s*equity", re.IGNORECASE),
    ),
    ("comprehensive", re.compile(r"statements?\s+of\s+comprehensive\s+(?:income|loss)", re.IGNORECASE)),
)
_COMPANY_LINE: Final = re.compile(
    r"\b(company|companies|inc|corp|corporation|plc|ltd|group|subsidiaries)\b",
    re.IGNORECASE,
)
_STATEMENT_LABELS: Final = frozenset(
    ("table of contents", "index to consolidated financial statements", "index to financial statements")
)
_INCOME_WORDS: Final = frozenset(
    "income revenue sales profit margin earnings tax ebitda ebit gross cost expense operating eps dividend loss net".split()
)
_CASH_WORDS: Final = frozenset(
    "cash flow capex capital expenditure depreciation amortization investing financing".split()
)
_BALANCE_WORDS: Final = frozenset(
    """
    asset liability debt borrowing loan obligation liquidity quick current receivable inventory goodwill intangible
    property plant equipment capital intensive cash
    """.split()
)
_EQUITY_WORDS: Final = frozenset(
    "equity stockholders shareholders share buyback repurchase comprehensive".split()
)
_EXPLICIT_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "balance": ("balance", "sheet", "financial", "position"),
    "income": ("income", "operations", "profit", "loss"),
    "cash": ("cash", "flow"),
    "equity": ("equity", "stockholders", "shareholders"),
    "comprehensive": ("comprehensive",),
}

_PAGE_CACHE: dict[str, tuple[str, ...]] = {}


class PDFDocumentInput(BaseModel):
    """One complete offline PDF document."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of a parsed PDF document."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A durable page-level Source describing one physical PDF page."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the stable page identity derived from document revision and page number."""

    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must not be empty or whitespace only")
    if (
        not isinstance(document_sha256, str)
        or len(document_sha256) != _SHA256_HEX_LENGTH
        or document_sha256 != document_sha256.lower()
    ):
        raise ValueError("document_sha256 must be a lowercase SHA-256 hexadecimal string")
    try:
        int(document_sha256, 16)
    except ValueError as error:
        raise ValueError("document_sha256 must be a lowercase SHA-256 hexadecimal string") from error
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    readable = f"{PDF_PAGE_IDENTITY_PREFIX}:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(readable) <= MAX_SOURCE_ID_LENGTH:
        return readable
    document_digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
    return f"{PDF_PAGE_IDENTITY_PREFIX}:h:{document_digest}:{document_sha256}:{page_number}"


def _page_identity(source_id: str) -> tuple[str | None, str, int] | None:
    """Recover document id, revision digest, and physical page from a page source id."""

    if not source_id.startswith(f"{PDF_PAGE_IDENTITY_PREFIX}:"):
        return None
    parts = source_id.split(":")
    if len(parts) == 4:
        document_id, digest, number = parts[1], parts[2], parts[3]
        if len(digest) != _SHA256_HEX_LENGTH or not number.isdigit() or int(number) < 1:
            return None
        decoded = unquote(document_id)
        if not decoded.strip():
            return None
        return decoded, digest, int(number)
    if len(parts) == 5 and parts[1] == "h":
        document_digest, digest, number = parts[2], parts[3], parts[4]
        if len(document_digest) != _SHA256_HEX_LENGTH or len(digest) != _SHA256_HEX_LENGTH:
            return None
        if not number.isdigit() or int(number) < 1:
            return None
        return None, digest, int(number)
    return None


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one PDF document without returning partial results."""

    if not isinstance(value, PDFDocumentInput):
        raise ValueError("value must be a PDFDocumentInput")
    document_id = value.document_id
    if not document_id.strip():
        raise ValueError("document_id must not be empty or whitespace only")
    content = bytes(value.content)
    if not content:
        raise ValueError("content must be a non-empty PDF document")
    if b"%PDF-" not in content[:1024]:
        raise ValueError("content is not a PDF document")
    document_sha256 = hashlib.sha256(content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted and reader.decrypt("") is PasswordType.NOT_DECRYPTED:
            raise ValueError("PDF requires a user password to be read")
        texts: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            texts.append("" if text is None or not text.strip() else text)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("content is not a complete readable PDF document") from error
    return tuple(
        PDFPageInput(
            document_id=document_id,
            document_sha256=document_sha256,
            page_number=number,
            text=text,
        )
        for number, text in enumerate(texts, start=1)
    )


class _PDFPageProjection:
    """Expose one physical page as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


@dataclass(frozen=True, slots=True)
class _PDFPageSourceDefinition:
    """Bind the PDF page adapter to one durable version and its projections."""

    name: str = PDF_PAGE_SOURCE_NAME
    version: str = PDF_PAGE_SOURCE_VERSION
    input_class: type[PDFPageInput] = PDFPageInput
    source_class: type[PDFPageSource] = PDFPageSource
    projections: tuple[_PDFPageProjection, ...] = (_PDFPageProjection(),)

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            definition_version=self.version,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


PDF_PAGE_SOURCE_DEFINITION: _PDFPageSourceDefinition = _PDFPageSourceDefinition()


@dataclass(frozen=True, slots=True)
class _PageText:
    citation: MemoryCitation
    document_id: str
    page_number: int
    source_id: str
    text: str
    stems: tuple[str, ...]

    @property
    def location(self) -> tuple[str, int]:
        return (self.document_id, self.page_number)


@dataclass(frozen=True, slots=True)
class _QueryPlan:
    term_weights: dict[str, float]
    concepts: tuple[tuple[str, ...], ...]


async def search_pdf_pages(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Return exact citations for up to ``limit`` distinct pages relevant to ``question``.

    The scoped Memory is the only source of citations. Candidate selection is an offline
    lexical ranking over the current Memory entries, which keeps retrieval independent of
    the backend's exact-token FTS analysis while never fabricating a citation.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not isinstance(question, str) or not question.strip():
        return ()
    page = await memory.list()
    pages: list[_PageText] = []
    seen_sources: set[str] = set()
    seen_locations: set[tuple[str, int]] = set()
    for record in page.entries:
        references = tuple(
            reference for reference in record.entry.sources if reference.source_type == PDF_PAGE_SOURCE_NAME
        )
        if len(references) != 1:
            continue
        source_id = references[0].source_id
        if source_id in seen_sources:
            continue
        seen_sources.add(source_id)
        identity = _page_identity(source_id)
        if identity is None:
            continue
        document_id, _digest, page_number = identity
        entry_text = record.entry.text
        if document_id is None:
            document_id = entry_text.split("\n", 1)[0]
            if not document_id.strip():
                continue
        prefix = f"{document_id}\n"
        text = "" if entry_text == document_id else entry_text.removeprefix(prefix)
        if (document_id, page_number) in seen_locations:
            continue
        seen_locations.add((document_id, page_number))
        stems = _PAGE_CACHE.get(source_id)
        if stems is None:
            stems = _stems(text)
            if len(_PAGE_CACHE) >= _CACHE_LIMIT:
                _PAGE_CACHE.clear()
            _PAGE_CACHE[source_id] = stems
        pages.append(
            _PageText(
                citation=record.citation,
                document_id=document_id,
                page_number=page_number,
                source_id=source_id,
                text=text,
                stems=stems,
            )
        )
    if not pages:
        return ()
    targets = _target_documents(question, {candidate.document_id for candidate in pages})
    if targets:
        pages = [candidate for candidate in pages if candidate.document_id in targets]
        if not pages:
            return ()
    document_counts: Counter[str] = Counter()
    for candidate in pages:
        document_counts.update(set(candidate.stems))
    average_length = sum(len(candidate.stems) for candidate in pages) / len(pages)
    doc_tokens: set[str] = set()
    doc_keys: set[str] = set()
    for document_id in {candidate.document_id for candidate in pages}:
        doc_tokens.update(_stems(document_id))
        doc_keys.add(_document_key(document_id))
    plan = _query_plan(question, document_counts, len(pages), frozenset(doc_tokens), frozenset(doc_keys))
    if not plan.term_weights:
        return ()
    scores = {
        candidate.location: _page_score(candidate, plan, document_counts, len(pages), average_length)
        for candidate in pages
    }
    scores = _spread_scores(pages, scores)
    ranked = sorted(
        ((scores[candidate.location], candidate) for candidate in pages if scores[candidate.location] > 0),
        key=lambda item: (-item[0], item[1].document_id, item[1].page_number, item[1].source_id),
    )
    selected = _select_pages(ranked, pages, scores, question, doc_tokens, doc_keys, limit)
    return tuple(candidate.citation for candidate in selected)


def _select_pages(
    ranked: list[tuple[float, _PageText]],
    pages: list[_PageText],
    scores: dict[tuple[str, int], float],
    question: str,
    doc_tokens: set[str],
    doc_keys: set[str],
    limit: int,
) -> list[_PageText]:
    selected = [candidate for _score, candidate in ranked[:limit]]
    if limit < _MIN_STATEMENT_LIMIT:
        return selected
    needed = _needed_statement_types(question, frozenset(doc_tokens), frozenset(doc_keys))
    if not needed:
        return selected
    best_by_document: dict[str, float] = {}
    for candidate in pages:
        best_by_document[candidate.document_id] = max(
            best_by_document.get(candidate.document_id, 0.0), scores[candidate.location]
        )
    threshold = max(best_by_document.values()) * _STATEMENT_DOC_RATIO
    by_location = {candidate.location: candidate for candidate in pages}
    guaranteed: list[_PageText] = []
    for document_id, best in best_by_document.items():
        if best < threshold:
            continue
        statements = _statement_map(pages, document_id)
        matches = [
            number
            for number, kinds in sorted(statements.items())
            if not kinds.isdisjoint(needed) and (document_id, number) in by_location
        ]
        for number in matches[:_MAX_STATEMENT_PAGES]:
            guaranteed.append(by_location[(document_id, number)])
    if not guaranteed:
        return selected
    guaranteed_locations = {candidate.location for candidate in guaranteed}
    chosen = {candidate.location for candidate in selected}
    for candidate in guaranteed:
        if candidate.location in chosen:
            continue
        if len(selected) < limit:
            selected.append(candidate)
            chosen.add(candidate.location)
            continue
        for index in range(len(selected) - 1, -1, -1):
            if selected[index].location not in guaranteed_locations:
                chosen.discard(selected[index].location)
                selected[index] = candidate
                chosen.add(candidate.location)
                break
        else:
            break
    return selected


def _spread_scores(
    pages: list[_PageText],
    scores: dict[tuple[str, int], float],
) -> dict[tuple[str, int], float]:
    by_document: dict[str, dict[int, tuple[float, tuple[str, int]]]] = {}
    for candidate in pages:
        by_document.setdefault(candidate.document_id, {})[candidate.page_number] = (
            scores[candidate.location],
            candidate.location,
        )
    lifted = dict(scores)
    for document_scores in by_document.values():
        for number, (score, _location) in document_scores.items():
            if score <= 0:
                continue
            for neighbour in (number - 1, number + 1):
                hit = document_scores.get(neighbour)
                if hit is not None and hit[0] > 0:
                    lifted[hit[1]] = max(lifted[hit[1]], score * _SPREAD_WEIGHT)
    return lifted


def _page_score(
    candidate: _PageText,
    plan: _QueryPlan,
    document_counts: Counter[str],
    document_count: int,
    average_length: float,
) -> float:
    stems = candidate.stems
    if not stems:
        return 0.0
    average_window = min(average_length, _WINDOW_TOKENS)
    starts = list(range(0, max(len(stems) - _WINDOW_TOKENS, 0) + 1, _WINDOW_STRIDE))
    last = max(len(stems) - _WINDOW_TOKENS, 0)
    if not starts or starts[-1] != last:
        starts.append(last)
    heading = set(stems[:_HEADING_TOKENS])
    best = 0.0
    for start in starts:
        chunk = stems[start : start + _WINDOW_TOKENS]
        counts = Counter(chunk)
        score = _window_score(counts, len(chunk), plan, document_counts, document_count, average_window)
        if start == 0:
            score += _HEADING_BONUS * sum(
                plan.term_weights[term] * _idf(term, document_counts, document_count)
                for term in heading
                if term in plan.term_weights
            )
        best = max(best, score)
    whole = Counter(stems)
    best += _COVERAGE_BONUS * sum(1 for concept in plan.concepts if any(whole.get(term) for term in concept))
    return best


def _window_score(
    counts: Counter[str],
    length: int,
    plan: _QueryPlan,
    document_counts: Counter[str],
    document_count: int,
    average_window: float,
) -> float:
    normalization = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * length / max(average_window, 1.0))
    score = 0.0
    for term, weight in plan.term_weights.items():
        frequency = counts.get(term, 0)
        if frequency:
            inverse = _idf(term, document_counts, document_count)
            score += weight * inverse * frequency * (_BM25_K1 + 1.0) / (frequency + normalization)
    score += _COORDINATION_BONUS * sum(1 for concept in plan.concepts if any(counts.get(term) for term in concept))
    return score


def _idf(term: str, document_counts: Counter[str], document_count: int) -> float:
    frequency = document_counts.get(term, 0)
    return math.log(1.0 + (document_count - frequency + 0.5) / (frequency + 0.5))


def _query_plan(
    question: str,
    document_counts: Counter[str],
    document_count: int,
    doc_tokens: frozenset[str],
    doc_keys: frozenset[str],
) -> _QueryPlan:
    stems = _question_stems(question, doc_tokens, doc_keys)
    ordered = list(dict.fromkeys(stems))
    phrase_concepts: list[tuple[str, ...]] = []
    for size in (3, 2):
        for index in range(len(stems) - size + 1):
            aliases = _PHRASE_STEM_ALIASES.get(tuple(stems[index : index + size]))
            if not aliases:
                continue
            alternatives = tuple(
                sorted(
                    {
                        alias
                        for alias in aliases
                        if alias and _idf(alias, document_counts, document_count) >= _IDF_FLOOR
                    }
                )
            )
            if alternatives:
                phrase_concepts.append(alternatives)
    kept = [term for term in ordered if _idf(term, document_counts, document_count) >= _IDF_FLOOR]
    if len(kept) > _MAX_QUERY_TERMS:
        kept = sorted(
            kept, key=lambda term: (-_idf(term, document_counts, document_count), ordered.index(term))
        )[:_MAX_QUERY_TERMS]
    term_weights: dict[str, float] = {}
    concepts: list[tuple[str, ...]] = []
    for term in kept:
        alternatives = {term}
        alternatives.update(_SYNONYM_STEMS.get(term, ()))
        alternatives = {
            alternative
            for alternative in alternatives
            if alternative and _idf(alternative, document_counts, document_count) >= _IDF_FLOOR
        }
        for alternative in alternatives:
            term_weights[alternative] = max(
                term_weights.get(alternative, 0.0),
                1.0 if alternative == term else _SYNONYM_WEIGHT,
            )
        concepts.append(tuple(sorted(alternatives)))
    for alternatives in phrase_concepts:
        for alternative in alternatives:
            term_weights[alternative] = max(term_weights.get(alternative, 0.0), _SYNONYM_WEIGHT)
        concepts.append(alternatives)
    return _QueryPlan(term_weights=term_weights, concepts=tuple(concepts))


def _question_stems(
    question: str,
    doc_tokens: frozenset[str],
    doc_keys: frozenset[str],
) -> list[str]:
    stems: list[str] = []
    for token in _tokens(_normalize(question)):
        if token in _NOISE or token.isdigit() or len(token) == 1:
            continue
        stem = _stem(token)
        if stem in _NOISE_STEMS or stem in doc_tokens:
            continue
        if len(stem) > 2 and any(stem in key for key in doc_keys):
            continue
        stems.append(stem)
    return stems


def _needed_statement_types(
    question: str,
    doc_tokens: frozenset[str],
    doc_keys: frozenset[str],
) -> frozenset[str]:
    raw = set(_question_stems(question, doc_tokens, doc_keys))
    needed: set[str] = set()
    if raw & _BALANCE_EXPLICIT:
        needed.add("balance")
    if raw & _INCOME_EXPLICIT:
        needed.add("income")
    if {"cash", "flow"} <= raw:
        needed.add("cash")
    if raw & _EQUITY_EXPLICIT:
        needed.add("equity")
    if raw & _COMPREHENSIVE_EXPLICIT:
        needed.add("comprehensive")
    if raw & _INCOME_TERMS:
        needed.add("income")
    if raw & _CASH_TERMS:
        needed.add("cash")
    if raw & _BALANCE_TERMS:
        needed.add("balance")
    if raw & _EQUITY_TERMS:
        needed.update(("equity", "comprehensive"))
    if _CAPITAL_STEM in raw and _INTENSIVE_STEM in raw:
        needed.update(("income", "balance", "cash"))
    if _QUICK_BALANCE_STEM in raw or _CURRENT_BALANCE_STEM in raw:
        needed.add("balance")
    return frozenset(needed)


def _statement_map(pages: list[_PageText], document_id: str) -> dict[int, frozenset[str]]:
    found: dict[int, frozenset[str]] = {}
    for candidate in sorted(pages, key=lambda item: item.page_number):
        if candidate.document_id != document_id:
            continue
        lines = [line.strip() for line in candidate.text.splitlines() if line.strip()][:10]
        for index, line in enumerate(lines):
            if not line.startswith(("Consolidated", "CONSOLIDATED")) or len(line) > 120 or "," in line:
                continue
            if any(character.isdigit() for character in line) or not _TITLE_PREFIX.match(line):
                continue
            before = lines[index - 1] if index else ""
            if index and before.casefold() not in _STATEMENT_LABELS and not _COMPANY_LINE.search(before):
                continue
            kinds = frozenset(
                kind for kind, pattern in _TITLE_PATTERNS if pattern.search(line)
            ) or frozenset(("other",))
            found[candidate.page_number] = kinds
            break
    return found


def _document_key(document_id: str) -> str:
    return "".join(character for character in document_id.casefold() if character.isalnum())


def _target_documents(question: str, document_ids: set[str]) -> frozenset[str]:
    """Narrow a multi-document scope when the question names one of its documents."""

    key = _document_key(question)
    if not key:
        return frozenset()
    tokens = set(_tokens(_normalize(question)))
    targets: set[str] = set()
    for document_id in document_ids:
        match = re.match(r"[0-9A-Za-z]+", document_id)
        normalized = _document_key(match.group(0)) if match else ""
        if len(normalized) < 2 or normalized.isdigit():
            continue
        if normalized in key:
            targets.add(document_id)
            continue
        if len(normalized) >= 3 and any(
            len(token) >= 3 and (normalized.startswith(token) or token.startswith(normalized))
            for token in tokens
        ):
            targets.add(document_id)
    return frozenset(targets)


def _normalize(value: str) -> str:
    for pattern, replacement in _NORMALIZATIONS:
        value = pattern.sub(replacement, value)
    return value


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(unicodedata.normalize("NFC", value).casefold()))


def _stems(value: str) -> tuple[str, ...]:
    return tuple(_stem(token) for token in _tokens(value))


def _term_set(words: frozenset[str] | tuple[str, ...]) -> frozenset[str]:
    return frozenset(_stem(word) for word in words)


_VOWELS: Final = frozenset("aeiou")
_STEP2: Final = (
    ("ational", "ate"),
    ("tional", "tion"),
    ("enci", "ence"),
    ("anci", "ance"),
    ("izer", "ize"),
    ("bli", "ble"),
    ("alli", "al"),
    ("entli", "ent"),
    ("eli", "e"),
    ("ousli", "ous"),
    ("ization", "ize"),
    ("ation", "ate"),
    ("ator", "ate"),
    ("alism", "al"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("aliti", "al"),
    ("iviti", "ive"),
    ("biliti", "ble"),
    ("logi", "log"),
)
_STEP3: Final = (
    ("icate", "ic"),
    ("ative", ""),
    ("alize", "al"),
    ("iciti", "ic"),
    ("ical", "ic"),
    ("ful", ""),
    ("ness", ""),
)
_STEP4: Final = (
    "ement",
    "ance",
    "ence",
    "able",
    "ible",
    "ment",
    "ant",
    "ent",
    "ion",
    "ism",
    "ate",
    "iti",
    "ous",
    "ive",
    "ize",
    "al",
    "er",
    "ic",
    "ou",
)


def _stem(token: str) -> str:
    if token.isdigit() or len(token) <= 3:
        return token
    word = token
    if word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("ies"):
        word = word[:-2]
    elif word.endswith("ss"):
        pass
    elif word.endswith("s"):
        word = word[:-1]
    changed = False
    if word.endswith("eed"):
        if _measure(word[:-3]) > 0:
            word = word[:-1]
    elif word.endswith("ed") and _contains_vowel(word[:-2]):
        word = word[:-2]
        changed = True
    elif word.endswith("ing") and _contains_vowel(word[:-3]):
        word = word[:-3]
        changed = True
    if changed:
        if word.endswith(("at", "bl", "iz")):
            word = f"{word}e"
        elif _ends_double_consonant(word) and word[-1] not in "lsz":
            word = word[:-1]
        elif _measure(word) == 1 and _ends_cvc(word):
            word = f"{word}e"
    if word.endswith("y") and _contains_vowel(word[:-1]):
        word = f"{word[:-1]}i"
    for suffix, replacement in _STEP2:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if _measure(stem) > 0:
                word = f"{stem}{replacement}"
            break
    for suffix, replacement in _STEP3:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if _measure(stem) > 0:
                word = f"{stem}{replacement}"
            break
    for suffix in _STEP4:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if suffix == "ion" and not stem.endswith(("s", "t")):
                break
            if _measure(stem) > 1:
                word = stem
            break
    if word.endswith("e"):
        stem = word[:-1]
        measure = _measure(stem)
        if measure > 1 or (measure == 1 and not _ends_cvc(stem)):
            word = stem
    if word.endswith("ll") and _measure(word) > 1:
        word = word[:-1]
    if len(word) > 4 and word.endswith("i"):
        word = word[:-1]
    return word


def _is_vowel(word: str, index: int) -> bool:
    character = word[index]
    if character in _VOWELS:
        return True
    if character == "y":
        return index > 0 and not _is_vowel(word, index - 1)
    return False


def _contains_vowel(word: str) -> bool:
    return any(_is_vowel(word, index) for index in range(len(word)))


def _measure(word: str) -> int:
    count = 0
    previous_vowel = False
    for index in range(len(word)):
        vowel = _is_vowel(word, index)
        if previous_vowel and not vowel:
            count += 1
        previous_vowel = vowel
    return count


def _ends_double_consonant(word: str) -> bool:
    return len(word) >= 2 and word[-1] == word[-2] and not _is_vowel(word, len(word) - 1)


def _ends_cvc(word: str) -> bool:
    return (
        len(word) >= 3
        and not _is_vowel(word, len(word) - 3)
        and _is_vowel(word, len(word) - 2)
        and not _is_vowel(word, len(word) - 1)
        and word[-1] not in "wxy"
    )


_NOISE_STEMS: Final = frozenset(_stem(word) for word in _NOISE)
_SYNONYM_STEMS: Final = {
    _stem(key): tuple(_stem(value) for value in values) for key, values in _SYNONYMS.items()
}
_PHRASE_STEM_ALIASES: Final = {
    tuple(_stem(word) for word in phrase): tuple(_stem(alias) for alias in aliases)
    for phrase, aliases in _PHRASE_ALIASES.items()
}
_INCOME_TERMS: Final = _term_set(_INCOME_WORDS)
_CASH_TERMS: Final = _term_set(_CASH_WORDS)
_BALANCE_TERMS: Final = _term_set(_BALANCE_WORDS)
_EQUITY_TERMS: Final = _term_set(_EQUITY_WORDS)
_BALANCE_EXPLICIT: Final = _term_set(_EXPLICIT_WORDS["balance"])
_INCOME_EXPLICIT: Final = _term_set(_EXPLICIT_WORDS["income"])
_EQUITY_EXPLICIT: Final = _term_set(_EXPLICIT_WORDS["equity"])
_COMPREHENSIVE_EXPLICIT: Final = _term_set(_EXPLICIT_WORDS["comprehensive"])
_CAPITAL_STEM: Final = _stem("capital")
_INTENSIVE_STEM: Final = _stem("intensive")
_QUICK_BALANCE_STEM: Final = _stem("quick")
_CURRENT_BALANCE_STEM: Final = _stem("current")


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDF_PAGE_SOURCE_VERSION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
