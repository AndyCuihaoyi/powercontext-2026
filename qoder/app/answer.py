"""Grounded answer assembly: offline evidence extraction + optional LLM."""

from __future__ import annotations

import re
from typing import Any

import httpx

from . import config

_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=[.!?])\s+|\n+")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "what", "which", "how", "does", "did", "about", "into", "its", "their",
    "has", "have", "had", "will", "can", "could", "may", "might", "not", "but",
}


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _query_terms(query: str) -> set[str]:
    terms = {word.lower() for word in _WORD_RE.findall(query) if word.lower() not in _STOPWORDS}
    for run in _CJK_RE.findall(query):
        if len(run) <= 3:
            terms.add(run)
            continue
        for width in (2, 3):
            terms.update(run[i : i + width] for i in range(len(run) - width + 1))
    terms.discard("")
    return terms


def _sentence_key(sentence: str) -> str:
    return re.sub(r"\W+", "", sentence.lower())[:90]


def _sentence_tokens(sentence: str) -> set[str]:
    tokens = {word for word in _WORD_RE.findall(sentence.lower()) if len(word) >= 4}
    for run in _CJK_RE.findall(sentence):
        tokens.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return tokens


def _is_repeat(tokens: set[str], seen: list[set[str]], threshold: float = 0.82) -> bool:
    """10-Ks often repeat one disclosure across pages with small wording changes."""
    if not tokens:
        return False
    for existing in seen:
        union = tokens | existing
        if union and len(tokens & existing) / len(union) >= threshold:
            return True
    return False


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_RE.split(text) if part and part.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and len(part) < 24:
            merged[-1] = f"{merged[-1]} {part}".strip()
        else:
            merged.append(part)
    return merged[:60]


def _score_sentence(sentence: str, terms: set[str], hit_rank: int, heading: str) -> float:
    lowered = sentence.lower()
    stripped = sentence.strip()
    coverage = sum(1 for term in terms if term in lowered) / max(1, len(terms))
    score = coverage * 2.2 + 1.0 / (hit_rank + 1)
    if re.search(r"\d", sentence):
        score += 0.35
    if 60 <= len(sentence) <= 320:
        score += 0.3
    if heading and heading.lower() in lowered:
        score += 0.25
    # Chunks can start mid-sentence; a lowercase fragment usually continues the previous page.
    if stripped[:1].islower():
        score -= 1.3
    if re.search(r"(?i)further discussed in note|see note \d", lowered):
        score -= 0.8
    return score


def _citation_label(hit: dict[str, Any]) -> str:
    kind = hit.get("doc_kind") or ""
    if kind == "pdf":
        return f"p.{hit['page_no']}"
    if kind == "markdown":
        return f"§{hit['page_no']}"
    return f"p.{hit['page_no']}"


def _citation(index: int, hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": index,
        "hit_id": hit["id"],
        "document_id": hit["document_id"],
        "title": hit.get("doc_title", "未知资料"),
        "kind": hit.get("doc_kind", ""),
        "page_no": hit["page_no"],
        "heading": hit.get("heading", ""),
        "label": _citation_label(hit),
        "snippet": hit["text"][:900],
        "matched_by": hit.get("matched_by", ""),
        "channels": hit.get("channels", {}),
        "rrf_score": hit.get("rrf_score", 0.0),
    }


def _focus_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the answer on one evidence type when the shelf mixes notes and PDFs."""
    if len(hits) < 2:
        return hits
    top_kind = hits[0].get("doc_kind") or ""
    same_kind = [hit for hit in hits if (hit.get("doc_kind") or "") == top_kind]
    return same_kind if len(same_kind) >= 2 else hits


def _extractive_answer(
    query: str,
    hits: list[dict[str, Any]],
    language: str,
) -> dict[str, Any]:
    hits = _focus_hits(hits)
    if not hits:
        return {
            "mode": "no_evidence",
            "lead": "没有找到足够相关的证据。" if _has_cjk(query) else "No sufficiently relevant evidence was found.",
            "bullets": [],
            "note": "可以换一个关键词，或先导入更多资料。" if _has_cjk(query) else
                    "Try different keywords, or import more documents first.",
            "text": "",
            "citations": [],
        }

    terms = _query_terms(query)
    bullets: list[dict[str, Any]] = []
    used: set[str] = set()
    used_tokens: list[set[str]] = []
    for rank, hit in enumerate(hits[:6]):
        candidates = _sentences(hit["text"])
        if not candidates:
            continue
        scored = sorted(
            (
                (_score_sentence(sentence, terms, rank, hit.get("heading", "")), sentence)
                for sentence in candidates
                if _sentence_key(sentence) not in used
            ),
            key=lambda item: -item[0],
        )
        picked = 0
        for _, sentence in scored:
            if picked >= (2 if rank == 0 else 1):
                break
            key = _sentence_key(sentence)
            if key in used:
                continue
            tokens = _sentence_tokens(sentence)
            if _is_repeat(tokens, used_tokens):
                continue
            used.add(key)
            used_tokens.append(tokens)
            snippet = sentence[:360].strip()
            if len(sentence) > 360:
                snippet += "…"
            bullets.append({"text": snippet, "citation": rank + 1})
            picked += 1
        if len(bullets) >= 4:
            break

    if not bullets:
        snippet = hits[0]["text"][:360].strip()
        if len(hits[0]["text"]) > 360:
            snippet += "…"
        bullets.append({"text": snippet, "citation": 1})

    cited = sorted({item["citation"] for item in bullets})
    renumber = {old: new for new, old in enumerate(cited, start=1)}
    for item in bullets:
        item["citation"] = renumber[item["citation"]]
    citations = [_citation(renumber[rank], hits[rank - 1]) for rank in cited]

    zh = language == "zh" or (language == "auto" and _has_cjk(query))
    lead = "根据已导入资料中的检索证据：" if zh else "Based on the retrieved evidence:"
    note = ""
    top_snippets = " ".join(hit["text"][:400] for hit in hits[:3])
    if _has_cjk(query) and top_snippets and not _has_cjk(top_snippets):
        note = "检索到的主要是英文原文，以下保留原句并附页码，避免转述失真。"
    joined = "\n".join(f"- {item['text']} [{item['citation']}]" for item in bullets)
    text = f"{lead}\n{joined}" + (f"\n\n{note}" if note else "")
    return {
        "mode": "extractive",
        "lead": lead,
        "bullets": bullets,
        "note": note,
        "text": text,
        "citations": citations,
    }


def _llm_answer(
    query: str,
    hits: list[dict[str, Any]],
    memory_texts: list[str],
    language: str,
) -> dict[str, Any] | None:
    if not config.llm_available():
        return None
    hits = _focus_hits(hits)
    evidence_lines = []
    for index, hit in enumerate(hits[:6], start=1):
        label = _citation_label(hit)
        heading = f" · {hit['heading']}" if hit.get("heading") else ""
        evidence_lines.append(
            f"[{index}] {hit.get('doc_title','未知资料')} {label}{heading}（通道: {hit.get('matched_by','')}）\n"
            f"{hit['text'][:1600]}"
        )
    preferred = "中文" if language == "zh" else "英文"
    memory_block = "\n".join(f"- {text}" for text in memory_texts[:8]) or "（无）"
    system = (
        "你是 CiteFlow 研读助手。只依据用户提供的检索证据回答，不得补充外部事实。"
        "每个结论后用 [n] 标注对应证据编号；证据不足时明确说明。"
        f"回答语言：{preferred}。保持简洁，先给结论，再给要点。"
    )
    user = (
        f"问题：{query}\n\n"
        f"用户长期偏好与进度（来自 PowerContext，仅作参考）：\n{memory_block}\n\n"
        f"检索证据：\n" + "\n\n".join(evidence_lines)
    )
    try:
        response = httpx.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
            json={
                "model": config.LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                "max_tokens": 900,
            },
            timeout=config.LLM_TIMEOUT,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
    except Exception:  # noqa: BLE001 - fall back to extractive mode
        return None
    if not content:
        return None
    citations = [_citation(index + 1, hit) for index, hit in enumerate(hits[:6])]
    return {
        "mode": "llm",
        "lead": "",
        "bullets": [],
        "note": "" if re.search(r"\[\d+\]", content) else "回答未包含引用标记，证据见右侧列表。",
        "text": content,
        "citations": citations,
        "model": config.LLM_MODEL,
    }


def answer(
    query: str,
    hits: list[dict[str, Any]],
    *,
    memory_texts: list[str] | None = None,
    language: str = "auto",
    use_llm: bool = True,
) -> dict[str, Any]:
    memory_texts = memory_texts or []
    result = None
    if use_llm and hits:
        result = _llm_answer(query, hits, memory_texts, language)
    if result is None:
        result = _extractive_answer(query, hits, language)
        if use_llm and config.llm_available():
            result["note"] = (result.get("note", "") + " ").strip()
    result["applied_memories"] = memory_texts
    result["language"] = language
    return result
