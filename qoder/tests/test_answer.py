from __future__ import annotations

from app import answer


def _hit(
    doc_id: str,
    page: int,
    text: str,
    *,
    kind: str = "pdf",
    title: str = "3M 2022 10-K（年报）",
) -> dict:
    return {
        "id": page,
        "document_id": doc_id,
        "page_no": page,
        "heading": "Risk Factors",
        "text": text,
        "doc_title": title,
        "doc_kind": kind,
        "matched_by": "fts+vector",
        "channels": {"fts": {"rank": 1, "score": 3.2}},
        "rrf_score": 0.03,
    }


def test_extractive_answer_keeps_only_cited_pages() -> None:
    hits = [
        _hit(
            "doc_3m",
            20,
            "Raw material costs increased in 2022. Supply chain disruptions affected production. "
            "The company expects inflation to continue.",
        ),
        _hit(
            "doc_3m",
            21,
            "The company sources materials from many suppliers. Logistics constraints persisted.",
        ),
        _hit("doc_3m", 22, "Credit ratings remain important to the company."),
        _hit("doc_3m", 23, "The company operates in many countries."),
        _hit("doc_3m", 24, "Employees are located around the world."),
    ]
    result = answer.answer("supply chain risk", hits, use_llm=False)
    assert result["mode"] == "extractive"
    assert result["bullets"]
    cited = {item["citation"] for item in result["bullets"]}
    assert cited == {item["index"] for item in result["citations"]}
    assert set(cited) == set(range(1, len(cited) + 1))
    assert result["citations"][0]["document_id"] == "doc_3m"
    assert "[1]" in result["text"]
    assert len(result["citations"]) < len(hits)


def test_answer_prefers_the_top_hit_document_kind() -> None:
    hits = [
        _hit("doc_3m", 20, "Supply chain risk from raw materials and logistics."),
        _hit(
            "doc_notes",
            1,
            "记录证据时写下页码和原文句子。",
            kind="markdown",
            title="研读方法笔记",
        ),
        _hit("doc_3m", 34, "Further supply chain disruption could reduce sales."),
    ]
    result = answer.answer("supply chain risk", hits, use_llm=False)
    assert {item["document_id"] for item in result["citations"]} == {"doc_3m"}
    assert all(item["kind"] == "pdf" for item in result["citations"])


def test_no_evidence_is_explicit() -> None:
    result = answer.answer("unknown topic", [], use_llm=False)
    assert result["mode"] == "no_evidence"
    assert result["citations"] == []
    assert result["text"] == ""
    assert "No sufficiently relevant evidence" in result["lead"]


def test_answer_language_switch() -> None:
    hits = [_hit("doc_3m", 1, "Supply chain risk is discussed here.")]
    zh = answer.answer("供应链风险", hits, language="zh", use_llm=False)
    en = answer.answer("supply chain risk", hits, language="en", use_llm=False)
    assert zh["lead"].startswith("根据")
    assert en["lead"].startswith("Based on")
