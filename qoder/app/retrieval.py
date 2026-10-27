"""Single-statement hybrid retrieval over seekdb.

One SQL statement returns two ordered candidate channels:
  * FULLTEXT (IK parser) -> TEXT RETRIEVAL SCAN
  * VECTOR (HNSW/vsag)   -> VECTOR INDEX ADAPTIVE SCAN
The application layer fuses the channels with reciprocal rank fusion (RRF).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from . import config, db, embeddings

_LAST: dict[str, Any] = {}
_TITLE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,}")


def build_hybrid_sql(
    document_ids: list[str] | None,
    collection_id: str | None = None,
) -> tuple[str, int]:
    doc_clause = ""
    if document_ids:
        placeholders = ", ".join(["%s"] * len(document_ids))
        doc_clause = f" AND c.document_id IN ({placeholders})"
    collection_clause = " AND c.collection_id = %s" if collection_id else ""
    fts = f"""
    SELECT c.id, c.document_id, c.page_no, c.heading, c.text,
           'fts' AS channel,
           ROW_NUMBER() OVER (ORDER BY MATCH(c.text) AGAINST (%s) DESC) AS rank_in_channel,
           MATCH(c.text) AGAINST (%s) AS channel_score
    FROM cf_chunks c
    WHERE 1 = 1{collection_clause}{doc_clause}
      AND MATCH(c.text) AGAINST (%s)
    ORDER BY MATCH(c.text) AGAINST (%s) DESC
    LIMIT %s
    """
    vec = f"""
    SELECT c.id, c.document_id, c.page_no, c.heading, c.text,
           'vector' AS channel,
           ROW_NUMBER() OVER (ORDER BY l2_distance(c.embedding, %s) ASC) AS rank_in_channel,
           l2_distance(c.embedding, %s) AS channel_score
    FROM cf_chunks c
    WHERE 1 = 1{collection_clause}{doc_clause}
    ORDER BY l2_distance(c.embedding, %s) APPROXIMATE
    LIMIT %s
    """
    sql = (
        "SELECT id, document_id, page_no, heading, text, channel, rank_in_channel, channel_score "
        f"FROM ({fts}) fts UNION ALL "
        "SELECT id, document_id, page_no, heading, text, channel, rank_in_channel, channel_score "
        f"FROM ({vec}) vec"
    )
    doc_params = len(document_ids or [])
    collection_params = 1 if collection_id else 0
    # fts: rank query, score query, [collection], [docs], where query, order query, limit
    # vec: rank vector, score vector, [collection], [docs], order vector, limit
    placeholders = 9 + 2 * collection_params + 2 * doc_params
    return sql, placeholders


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"


def _title_boost(query: str, title: str) -> float:
    """Boost documents whose title names an entity mentioned in the query."""
    query_tokens = {token.lower() for token in _TITLE_TOKEN_RE.findall(query) if len(token) >= 2}
    lowered = (title or "").lower()
    return 1.35 if any(token in lowered for token in query_tokens) else 1.0


def _fuse(
    rows: list[dict[str, Any]],
    top_k: int,
    query: str = "",
    documents: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        chunk_id = int(row["id"])
        channel = str(row["channel"])
        rank = int(row["rank_in_channel"])
        score = float(row["channel_score"])
        hit = grouped.get(chunk_id)
        if hit is None:
            hit = {
                "id": chunk_id,
                "document_id": row["document_id"],
                "page_no": int(row["page_no"]),
                "heading": row["heading"],
                "text": row["text"],
                "channels": {},
                "rrf_score": 0.0,
            }
            grouped[chunk_id] = hit
        hit["channels"][channel] = {"rank": rank, "score": round(score, 6)}
        # rank^1.5 suppresses weak agreement: two mediocre channels must not
        # outrank a strong single-channel hit (e.g. rank 20 + 24 vs rank 1).
        hit["rrf_score"] += 1.0 / (config.RRF_K + rank ** 1.5)

    documents = documents or {}
    for item in grouped.values():
        title = documents.get(item["document_id"], {}).get("title", "")
        item["title_boost"] = _title_boost(query, title)

    fused = sorted(
        grouped.values(),
        key=lambda item: (
            -(item["rrf_score"] * item["title_boost"]),
            min(meta["rank"] for meta in item["channels"].values()),
            item["id"],
        ),
    )
    for item in fused:
        item["rrf_score"] = round(item["rrf_score"] * item["title_boost"], 6)
        item["matched_by"] = "+".join(sorted(item["channels"]))
    return fused[:top_k]


def _documents_for(document_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not document_ids:
        return {}
    ids = sorted(document_ids)
    placeholders = ", ".join(["%s"] * len(ids))
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, title, kind, filename, page_count FROM cf_documents "
                f"WHERE id IN ({placeholders})",
                ids,
            )
            return {row["id"]: row for row in cur.fetchall()}
    finally:
        conn.close()


def _attach_documents(
    hits: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]] | None = None,
) -> None:
    if not hits:
        return
    docs = documents if documents is not None else _documents_for({hit["document_id"] for hit in hits})
    for hit in hits:
        doc = docs.get(hit["document_id"], {})
        hit["doc_title"] = doc.get("title", "未知资料")
        hit["doc_kind"] = doc.get("kind", "")
        hit["filename"] = doc.get("filename", "")
        hit["doc_page_count"] = int(doc.get("page_count", 0) or 0)


def search(
    query: str,
    *,
    collection_id: str | None = None,
    document_ids: list[str] | None = None,
    top_k: int = config.TOP_K_DEFAULT,
    user_id: str = "local",
    session_id: str = "local",
    mode: str = "search",
) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        raise ValueError("请输入问题或关键词")
    if len(query) > config.MAX_QUERY_CHARS:
        raise ValueError(f"问题过长（上限 {config.MAX_QUERY_CHARS} 字）")
    collection = (collection_id or "").strip()[:64]
    document_ids = [item for item in (document_ids or []) if item][:50]
    top_k = max(1, min(int(top_k), 30))

    started = time.perf_counter()
    provider = embeddings.get_provider()
    query_vector = provider.embed_query(query)
    embed_ms = int((time.perf_counter() - started) * 1000)
    vector_literal = _vector_literal(query_vector)

    sql, expected_params = build_hybrid_sql(document_ids, collection or None)
    collection_params = [collection] if collection else []
    params: list[Any] = [
        query,
        query,
        *collection_params,
        *document_ids,
        query,
        query,
        config.PER_CHANNEL_LIMIT,
        vector_literal,
        vector_literal,
        *collection_params,
        *document_ids,
        vector_literal,
        config.PER_CHANNEL_LIMIT,
    ]
    if len(params) != expected_params:
        raise RuntimeError(f"hybrid SQL 参数数量不一致: {len(params)} != {expected_params}")

    query_started = time.perf_counter()
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = list(cur.fetchall())
    finally:
        conn.close()
    query_ms = int((time.perf_counter() - query_started) * 1000)

    documents = _documents_for({row["document_id"] for row in rows})
    hits = _fuse(rows, top_k, query, documents)
    _attach_documents(hits, documents)

    fts_hits = len({row["id"] for row in rows if row["channel"] == "fts"})
    vector_hits = len({row["id"] for row in rows if row["channel"] == "vector"})
    total_ms = int((time.perf_counter() - started) * 1000)

    trace = {
        "query": query,
        "collection_id": collection or "*",
        "document_ids": document_ids,
        "top_k": top_k,
        "per_channel_limit": config.PER_CHANNEL_LIMIT,
        "embedding": {
            "backend": provider.backend,
            "model": provider.model,
            "dim": provider.dim,
        },
        "embed_ms": embed_ms,
        "query_ms": query_ms,
        "total_ms": total_ms,
        "fts_hits": fts_hits,
        "vector_hits": vector_hits,
        "fused_hits": len(hits),
        "sql": sql.strip(),
        "channels": {
            "fulltext": "MATCH(...) AGAINST(...) + ik parser",
            "vector": "l2_distance(...) ORDER BY ... APPROXIMATE + hnsw/vsag",
            "fusion": f"RRF k={config.RRF_K}, rank^1.5, title boost 1.35",
        },
    }

    _LAST.clear()
    _LAST.update(
        {
            "trace": trace,
            "sql": sql,
            "params": params,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _persist_last(trace, hits)

    try:
        db.log_query(
            user_id=user_id,
            session_id=session_id,
            query=query,
            mode=mode,
            collection_id=collection or "*",
            doc_filter=",".join(document_ids),
            latency_ms=total_ms,
            hit_count=len(hits),
            fts_hits=fts_hits,
            vector_hits=vector_hits,
        )
    except Exception:  # noqa: BLE001 - logging must not break retrieval
        pass

    return {"hits": hits, "trace": trace}


def _persist_last(trace: dict[str, Any], hits: list[dict[str, Any]]) -> None:
    try:
        config.ensure_dirs()
        payload = {
            "trace": trace,
            "results": [
                {
                    "id": hit["id"],
                    "document_id": hit["document_id"],
                    "doc_title": hit["doc_title"],
                    "page_no": hit["page_no"],
                    "heading": hit["heading"],
                    "matched_by": hit["matched_by"],
                    "channels": hit["channels"],
                    "rrf_score": hit["rrf_score"],
                    "snippet": hit["text"][:240],
                }
                for hit in hits
            ],
        }
        (config.RUNTIME_DIR / "last_retrieval.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _explain_rows(sql: str, params: list[Any]) -> list[Any]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            statement = cur.mogrify("EXPLAIN " + sql, params)
            if isinstance(statement, str):
                statement = statement.encode("utf-8")
            # seekdb may cut long literals in the plan at a fixed byte boundary,
            # splitting a multi-byte character. Read as latin-1 (byte-preserving)
            # and repair the UTF-8 below instead of failing to decode.
            conn.encoding = "latin1"
            cur.execute(statement)
            return list(cur.fetchall())
    finally:
        conn.close()


def _repair_plan_text(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    try:
        return text.encode("latin1").decode("utf-8", "replace")
    except UnicodeEncodeError:
        return text


def explain_last() -> dict[str, Any]:
    if not _LAST:
        raise ValueError("当前进程还没有检索记录，请先执行一次检索")
    sql = _LAST["sql"]
    params = _LAST["params"]
    rows = _explain_rows(sql, params)
    lines = []
    for row in rows:
        values = row.values() if isinstance(row, dict) else row
        lines.extend(_repair_plan_text(value) for value in values if value is not None)
    plan = "\n".join(line for line in lines if line and not set(line) <= {"-", "=", "+"})
    trace = dict(_LAST["trace"])
    trace["plan"] = plan
    _persist_plan(plan)
    return {"sql": sql.strip(), "plan": plan, "trace": trace}


def _persist_plan(plan: str) -> None:
    try:
        path = config.RUNTIME_DIR / "last_retrieval.json"
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        payload.setdefault("trace", {})["plan"] = plan
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def last_evidence() -> dict[str, Any]:
    if _LAST:
        trace = dict(_LAST["trace"])
        return {"trace": trace, "created_at": _LAST["created_at"], "sql": _LAST["sql"].strip()}
    path = config.RUNTIME_DIR / "last_retrieval.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {"trace": payload.get("trace", {}), "results": payload.get("results", [])}
        except Exception:  # noqa: BLE001
            pass
    return {}


def usage_stats() -> dict[str, Any]:
    return db.usage_stats()
