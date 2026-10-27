from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app import config, db, embeddings, ingest, retrieval

pytestmark = pytest.mark.skipif(
    os.environ.get("CITEFLOW_TEST_SEEKDB") != "1",
    reason="set CITEFLOW_TEST_SEEKDB=1 to run tests against a live seekdb",
)


def test_ingest_search_and_explain_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        db.init_schema()
        db.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"seekdb unavailable: {exc}")

    provider = embeddings.HashEmbedding()
    monkeypatch.setattr(embeddings, "get_provider", lambda: provider)
    monkeypatch.setattr(db, "log_query", lambda **_: None)

    collection = "test-citeflow-" + uuid.uuid4().hex[:8]
    marker = uuid.uuid4().hex
    text = (
        f"CiteFlow integration probe {marker}. "
        "The supply chain risk section discusses raw material shortages, "
        "supplier concentration and logistics disruption. "
        "Management expects these risks to affect operating margin."
    )
    outcome = ingest.ingest_bytes(
        text.encode("utf-8"),
        f"probe-{marker}.txt",
        collection_id=collection,
    )
    document = outcome["document"]
    stored = Path(config.RUNTIME_DIR) / document["stored_path"]
    try:
        assert outcome["duplicate"] is False
        result = retrieval.search(
            "supply chain risk", collection_id=collection, top_k=5, mode="test"
        )
        assert result["hits"]
        assert result["trace"]["fts_hits"] > 0
        assert result["trace"]["vector_hits"] > 0
        top = result["hits"][0]
        assert top["text"].startswith("CiteFlow integration probe")
        assert top["doc_title"].startswith("probe ")

        explain = retrieval.explain_last()
        # With a one-row collection the optimizer may use TABLE RANGE SCAN for
        # the fulltext side; the full 1,300-chunk dataset exercises the
        # TEXT RETRIEVAL SCAN path checked by scripts/verify_demo.py.
        assert "MATCH(c.text) AGAINST" in explain["plan"]
        assert "VECTOR INDEX" in explain["plan"]
    finally:
        db.delete_document(document["id"])
        stored.unlink(missing_ok=True)
