from __future__ import annotations

from app import config, retrieval


def test_hybrid_sql_placeholders_match_params() -> None:
    cases = [
        (None, None, 9),
        (None, "books", 11),
        (["doc_a"], None, 11),
        (["doc_a", "doc_b"], "books", 15),
    ]
    for document_ids, collection_id, expected in cases:
        sql, count = retrieval.build_hybrid_sql(document_ids, collection_id)
        assert count == expected
        assert sql.count("%s") == expected
        assert "MATCH(c.text) AGAINST" in sql
        assert "l2_distance(c.embedding" in sql
        assert "APPROXIMATE" in sql
        if collection_id:
            assert "c.collection_id = %s" in sql
        if document_ids:
            assert "c.document_id IN" in sql


def test_fuse_merges_channels_and_boosts_title() -> None:
    rows = [
        {
            "id": 1,
            "document_id": "doc_3m",
            "page_no": 20,
            "heading": "",
            "text": "3M supply chain",
            "channel": "vector",
            "rank_in_channel": 1,
            "channel_score": 0.12,
        },
        {
            "id": 1,
            "document_id": "doc_3m",
            "page_no": 20,
            "heading": "",
            "text": "3M supply chain",
            "channel": "fts",
            "rank_in_channel": 1,
            "channel_score": 3.5,
        },
        {
            "id": 2,
            "document_id": "doc_other",
            "page_no": 5,
            "heading": "",
            "text": "unrelated",
            "channel": "vector",
            "rank_in_channel": 2,
            "channel_score": 0.2,
        },
    ]
    documents = {
        "doc_3m": {"id": "doc_3m", "title": "3M 2022 10-K", "kind": "pdf", "page_count": 100},
        "doc_other": {"id": "doc_other", "title": "Other notes", "kind": "text", "page_count": 3},
    }
    hits = retrieval._fuse(rows, top_k=5, query="3M supply risk", documents=documents)
    assert [hit["id"] for hit in hits] == [1, 2]
    assert hits[0]["matched_by"] == "fts+vector"
    assert hits[0]["title_boost"] == 1.35
    assert hits[0]["rrf_score"] == round((1 / (config.RRF_K + 1) * 2) * 1.35, 6)
    assert hits[1]["title_boost"] == 1.0


def test_title_boost_requires_a_query_token() -> None:
    assert retrieval._title_boost("3M risk", "3M 2022 10-K") == 1.35
    assert retrieval._title_boost("Chinese notes", "3M 2022 10-K") == 1.0
    assert retrieval._title_boost("", "3M 2022 10-K") == 1.0
