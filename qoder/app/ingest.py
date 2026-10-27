"""Document parsing, chunking and ingestion."""

from __future__ import annotations

import hashlib
import io
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pypdf
import pypdfium2 as pdfium

from . import config, db, embeddings

_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_GENERIC_TITLES = {
    "united states",
    "securities and exchange commission",
    "form 10-k",
    "table of contents",
}


@dataclass
class Section:
    page_no: int
    heading: str
    text: str


@dataclass
class Chunk:
    page_no: int
    ordinal: int
    heading: str
    text: str


def _clean_title(value: str | None) -> str:
    if not value:
        return ""
    title = _WS_RE.sub(" ", str(value)).strip(" \t\n\r-")
    if len(title) < 3 or title.lower() in _GENERIC_TITLES:
        return ""
    if title.lower().startswith("microsoft word"):
        return ""
    return title[:180]


def _stem_title(filename: str) -> str:
    stem = Path(filename).stem.replace("_", " ").replace("-", " ").strip()
    return stem[:180] or "未命名资料"


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    # Rejoin words hyphenated across PDF line breaks.
    text = re.sub(r"(?<=[A-Za-z])-\n(?=[a-z])", "", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def parse_pdf(data: bytes, filename: str) -> tuple[str, list[Section], int]:
    reader = pypdf.PdfReader(io.BytesIO(data))
    meta_title = ""
    try:
        meta_title = _clean_title(reader.metadata.title if reader.metadata else "")
    except Exception:  # noqa: BLE001 - malformed metadata should not block parsing
        meta_title = ""

    sections: list[Section] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            raw = ""
        text = _normalize_text(raw)
        if text:
            sections.append(Section(page_no=index, heading="", text=text))

    title = meta_title
    if not title:
        first_line = ""
        if sections:
            first_line = sections[0].text.split("\n", 1)[0].strip()
        first_line = _clean_title(first_line)
        if first_line and len(first_line) <= 120:
            title = first_line
    if not title:
        title = _stem_title(filename)
    return title, sections, len(reader.pages)


def parse_markdown(text: str, filename: str) -> tuple[str, list[Section], int]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    sections: list[Section] = []
    heading = ""
    buffer: list[str] = []
    title = ""

    def flush() -> None:
        nonlocal buffer
        body = _normalize_text("\n".join(buffer))
        if body:
            sections.append(
                Section(page_no=len(sections) + 1, heading=heading, text=body)
            )
        buffer = []

    for line in text.split("\n"):
        match = _HEADING_RE.match(line)
        if match:
            flush()
            heading = match.group(2).strip()
            if not title:
                title = heading
            buffer.append(heading)
        else:
            buffer.append(line)
    flush()

    if not sections:
        body = _normalize_text(text)
        if body:
            sections = [Section(page_no=1, heading="", text=body)]
    if not title:
        title = _stem_title(filename)
    return title, sections, max(1, len(sections))


def parse_text(text: str, filename: str) -> tuple[str, list[Section], int]:
    body = _normalize_text(text)
    sections = [Section(page_no=1, heading="", text=body)] if body else []
    return _stem_title(filename), sections, 1


def parse_document(data: bytes, filename: str, kind: str) -> tuple[str, list[Section], int]:
    if kind == "pdf":
        return parse_pdf(data, filename)
    if kind == "markdown":
        return parse_markdown(data.decode("utf-8", errors="replace"), filename)
    if kind == "text":
        return parse_text(data.decode("utf-8", errors="replace"), filename)
    raise ValueError(f"不支持的文件类型: {kind}")


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+", text)
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(current) + len(sentence) + 1 <= limit:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                parts.append(current)
            if len(sentence) > limit:
                for start in range(0, len(sentence), limit):
                    parts.append(sentence[start : start + limit])
                current = ""
            else:
                current = sentence
    if current:
        parts.append(current)
    return parts


def chunk_sections(
    sections: Iterable[Section],
    target: int = 900,
    overlap: int = 120,
    min_chars: int = 40,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    ordinal = 0
    for section in sections:
        paragraphs = [part.strip() for part in section.text.split("\n\n") if part.strip()]
        buffer = ""
        for paragraph in paragraphs:
            for piece in _split_long(paragraph, int(target * 1.6)):
                candidate = f"{buffer}\n\n{piece}".strip() if buffer else piece
                if len(candidate) <= target:
                    buffer = candidate
                    continue
                if buffer:
                    text = buffer.strip()
                    if len(text) >= min_chars:
                        chunks.append(Chunk(section.page_no, ordinal, section.heading, text))
                        ordinal += 1
                    tail = text[-overlap:] if overlap else ""
                    cut = tail.find(" ")
                    if 0 <= cut < len(tail) // 2:
                        tail = tail[cut + 1 :]
                    buffer = f"{tail}\n\n{piece}".strip() if tail else piece
                else:
                    buffer = piece
        if buffer.strip():
            text = buffer.strip()
            if len(text) >= min_chars:
                chunks.append(Chunk(section.page_no, ordinal, section.heading, text))
                ordinal += 1
    return chunks


def _embedding_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"


def _serialize_chunks(document_id: str, collection_id: str, chunks: list[Chunk], vectors: list[list[float]]) -> list[dict]:
    rows = []
    for chunk, vector in zip(chunks, vectors):
        rows.append(
            {
                "document_id": document_id,
                "collection_id": collection_id,
                "page_no": chunk.page_no,
                "ordinal": chunk.ordinal,
                "heading": chunk.heading[:500],
                "text": chunk.text,
                "embedding": _embedding_literal(vector),
            }
        )
    return rows


def ingest_bytes(
    data: bytes,
    filename: str,
    *,
    collection_id: str = config.DEFAULT_COLLECTION,
    title: str | None = None,
) -> dict:
    started = time.perf_counter()
    suffix = Path(filename).suffix.lower()
    if suffix not in config.ALLOWED_EXTENSIONS:
        raise ValueError("仅支持 PDF、Markdown（.md/.markdown）和纯文本（.txt）文件")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise ValueError(f"文件超过 {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB 限制")
    if not data:
        raise ValueError("文件为空")

    sha256 = hashlib.sha256(data).hexdigest()
    existing = db.find_document_by_sha(sha256)
    if existing:
        return {"duplicate": True, "document": existing, "elapsed_ms": 0}

    kind = config.ALLOWED_EXTENSIONS[suffix]
    parsed_title, sections, page_count = parse_document(data, filename, kind)
    if not sections:
        raise ValueError("未能提取到文本（扫描版 PDF 需要先做 OCR）")
    chunks = chunk_sections(sections)
    if not chunks:
        raise ValueError("文本过短，未生成可检索片段")

    provider = embeddings.get_provider()
    embed_started = time.perf_counter()
    vectors = provider.embed_documents(
        [f"{chunk.heading}\n{chunk.text}" if chunk.heading else chunk.text for chunk in chunks]
    )
    embed_ms = int((time.perf_counter() - embed_started) * 1000)

    document_id = "doc_" + sha256[:20]
    config.ensure_dirs()
    stored_path = config.UPLOAD_DIR / f"{document_id}{suffix}"
    stored_path.write_bytes(data)

    document = {
        "id": document_id,
        "collection_id": collection_id[:64],
        "title": (title or parsed_title)[:500],
        "filename": filename[:500],
        "kind": kind,
        "sha256": sha256,
        "page_count": page_count,
        "chunk_count": len(chunks),
        "char_count": sum(len(section.text) for section in sections),
        "stored_path": str(stored_path.relative_to(config.RUNTIME_DIR)),
        "created_at": db._now(),
    }
    try:
        db.insert_document_with_chunks(
            document,
            _serialize_chunks(document_id, collection_id[:64], chunks, vectors),
        )
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "duplicate": False,
        "document": document,
        "elapsed_ms": elapsed_ms,
        "embed_ms": embed_ms,
        "embedding": {
            "backend": provider.backend,
            "model": provider.model,
            "dim": provider.dim,
        },
    }


def seed_samples(collection_id: str = config.DEMO_COLLECTION) -> dict:
    started = time.perf_counter()
    files = sorted(
        path for path in config.SAMPLES_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in config.ALLOWED_EXTENSIONS
    )
    titles = {
        "3M_2022_10K.pdf": "3M 2022 10-K（年报）",
        "BOEING_2022_10K.pdf": "Boeing 2022 10-K（年报）",
    }
    results = []
    for path in files:
        try:
            outcome = ingest_bytes(
                path.read_bytes(),
                path.name,
                collection_id=collection_id,
                title=titles.get(path.name),
            )
            results.append(
                {
                    "filename": path.name,
                    "duplicate": outcome["duplicate"],
                    "title": outcome["document"]["title"],
                    "chunks": outcome["document"]["chunk_count"],
                    "pages": outcome["document"]["page_count"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - report per file and continue
            results.append({"filename": path.name, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "collection_id": collection_id,
        "files": results,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def page_text(document_id: str, page_no: int) -> dict:
    document = db.get_document(document_id)
    if not document:
        raise ValueError("文档不存在")
    if page_no < 1 or page_no > int(document["page_count"]):
        raise ValueError("页码超出范围")
    rows = db.page_chunks(document_id, page_no)
    text = "\n\n".join(row["text"] for row in rows)
    headings = [row["heading"] for row in rows if row["heading"]]
    return {
        "document_id": document_id,
        "title": document["title"],
        "kind": document["kind"],
        "page_no": page_no,
        "page_count": int(document["page_count"]),
        "text": text,
        "headings": headings,
        "has_image": document["kind"] == "pdf",
    }


def page_image(document_id: str, page_no: int) -> bytes:
    document = db.get_document(document_id)
    if not document:
        raise ValueError("文档不存在")
    if document["kind"] != "pdf":
        raise ValueError("仅 PDF 支持页图预览")
    if page_no < 1 or page_no > int(document["page_count"]):
        raise ValueError("页码超出范围")

    config.ensure_dirs()
    cache_path = config.PAGE_CACHE_DIR / f"{document_id}_{page_no}.png"
    if cache_path.exists():
        return cache_path.read_bytes()

    pdf_path = config.RUNTIME_DIR / document["stored_path"]
    if not pdf_path.exists():
        raise ValueError("原始文件缺失，无法渲染页图")
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_no - 1]
        bitmap = page.render(scale=1.4)
        image = bitmap.to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        payload = buffer.getvalue()
    finally:
        pdf.close()
    cache_path.write_bytes(payload)
    return payload
