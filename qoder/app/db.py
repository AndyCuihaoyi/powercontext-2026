"""seekdb access layer: schema, documents, chunks, logs, fallback memory."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import pymysql
from pymysql.cursors import DictCursor

from . import config


class DBError(RuntimeError):
    pass


def _connect(database: str | None = None) -> pymysql.connections.Connection:
    try:
        return pymysql.connect(
            host=config.SEEKDB_HOST,
            port=config.SEEKDB_PORT,
            user=config.SEEKDB_USER,
            password=config.SEEKDB_PASSWORD,
            database=database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
            connect_timeout=5,
            read_timeout=30,
            write_timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        raise DBError(
            f"无法连接 seekdb {config.SEEKDB_HOST}:{config.SEEKDB_PORT}: {exc}"
        ) from exc


def connect() -> pymysql.connections.Connection:
    return _connect(config.SEEKDB_DATABASE)


def init_schema() -> None:
    config.ensure_dirs()
    admin = _connect(None)
    try:
        with admin.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.SEEKDB_DATABASE}` "
                "DEFAULT CHARACTER SET utf8mb4"
            )
        admin.commit()
    finally:
        admin.close()

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cf_documents (
                  id VARCHAR(64) PRIMARY KEY,
                  collection_id VARCHAR(64) NOT NULL,
                  title VARCHAR(512) NOT NULL,
                  filename VARCHAR(512) NOT NULL,
                  kind VARCHAR(16) NOT NULL,
                  sha256 CHAR(64) NOT NULL,
                  page_count INT NOT NULL,
                  chunk_count INT NOT NULL,
                  char_count INT NOT NULL,
                  stored_path VARCHAR(1024) NOT NULL,
                  created_at DATETIME NOT NULL,
                  KEY cf_doc_collection (collection_id),
                  KEY cf_doc_sha (sha256)
                ) ORGANIZATION = HEAP
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cf_chunks (
                  id BIGINT AUTO_INCREMENT PRIMARY KEY,
                  document_id VARCHAR(64) NOT NULL,
                  collection_id VARCHAR(64) NOT NULL,
                  page_no INT NOT NULL,
                  ordinal INT NOT NULL,
                  heading VARCHAR(512) NOT NULL DEFAULT '',
                  text MEDIUMTEXT NOT NULL,
                  embedding VECTOR(384) NOT NULL,
                  FULLTEXT INDEX cf_ft (text) WITH PARSER ik,
                  VECTOR INDEX cf_vec (embedding)
                    WITH (DISTANCE=l2, TYPE=hnsw, LIB=vsag, SYNC_MODE=IMMEDIATE),
                  KEY cf_chunk_doc (document_id),
                  KEY cf_chunk_collection (collection_id)
                ) ORGANIZATION = HEAP
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cf_queries (
                  id BIGINT AUTO_INCREMENT PRIMARY KEY,
                  user_id VARCHAR(64) NOT NULL,
                  session_id VARCHAR(64) NOT NULL,
                  query TEXT NOT NULL,
                  mode VARCHAR(16) NOT NULL,
                  collection_id VARCHAR(64) NOT NULL,
                  doc_filter VARCHAR(1024) NOT NULL DEFAULT '',
                  latency_ms INT NOT NULL,
                  hit_count INT NOT NULL,
                  fts_hits INT NOT NULL,
                  vector_hits INT NOT NULL,
                  answer_mode VARCHAR(16) NOT NULL DEFAULT '',
                  created_at DATETIME NOT NULL,
                  KEY cf_query_time (created_at)
                ) ORGANIZATION = HEAP
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cf_memories (
                  id BIGINT AUTO_INCREMENT PRIMARY KEY,
                  user_id VARCHAR(64) NOT NULL,
                  kind VARCHAR(32) NOT NULL,
                  text TEXT NOT NULL,
                  backend VARCHAR(24) NOT NULL,
                  pc_entry_id VARCHAR(128) NOT NULL DEFAULT '',
                  pc_request_id VARCHAR(128) NOT NULL DEFAULT '',
                  created_at DATETIME NOT NULL,
                  updated_at DATETIME NOT NULL,
                  KEY cf_mem_user (user_id, updated_at)
                ) ORGANIZATION = HEAP
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cf_progress (
                  user_id VARCHAR(64) PRIMARY KEY,
                  document_id VARCHAR(64) NOT NULL,
                  page_no INT NOT NULL,
                  updated_at DATETIME NOT NULL
                ) ORGANIZATION = HEAP
                """
            )
        conn.commit()
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ping() -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return bool(cur.fetchone())
    finally:
        conn.close()


def insert_document_with_chunks(document: dict[str, Any], chunks: Iterable[dict[str, Any]]) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cf_documents
                  (id, collection_id, title, filename, kind, sha256, page_count,
                   chunk_count, char_count, stored_path, created_at)
                VALUES
                  (%(id)s, %(collection_id)s, %(title)s, %(filename)s, %(kind)s, %(sha256)s,
                   %(page_count)s, %(chunk_count)s, %(char_count)s, %(stored_path)s, %(created_at)s)
                """,
                document,
            )
            cur.executemany(
                """
                INSERT INTO cf_chunks
                  (document_id, collection_id, page_no, ordinal, heading, text, embedding)
                VALUES
                  (%(document_id)s, %(collection_id)s, %(page_no)s, %(ordinal)s,
                   %(heading)s, %(text)s, %(embedding)s)
                """,
                list(chunks),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def find_document_by_sha(sha256: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cf_documents WHERE sha256 = %s ORDER BY created_at ASC LIMIT 1",
                (sha256,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def list_documents(collection_id: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            if collection_id:
                cur.execute(
                    "SELECT * FROM cf_documents WHERE collection_id = %s "
                    "ORDER BY created_at DESC",
                    (collection_id,),
                )
            else:
                cur.execute("SELECT * FROM cf_documents ORDER BY created_at DESC")
            return list(cur.fetchall())
    finally:
        conn.close()


def get_document(document_id: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM cf_documents WHERE id = %s", (document_id,))
            return cur.fetchone()
    finally:
        conn.close()


def delete_document(document_id: str) -> bool:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cf_chunks WHERE document_id = %s", (document_id,))
            cur.execute("DELETE FROM cf_progress WHERE document_id = %s", (document_id,))
            cur.execute("DELETE FROM cf_documents WHERE id = %s", (document_id,))
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def page_chunks(document_id: str, page_no: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, page_no, ordinal, heading, text FROM cf_chunks "
                "WHERE document_id = %s AND page_no = %s ORDER BY ordinal ASC",
                (document_id, page_no),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def log_query(
    *,
    user_id: str,
    session_id: str,
    query: str,
    mode: str,
    collection_id: str,
    doc_filter: str,
    latency_ms: int,
    hit_count: int,
    fts_hits: int,
    vector_hits: int,
    answer_mode: str = "",
) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cf_queries
                  (user_id, session_id, query, mode, collection_id, doc_filter,
                   latency_ms, hit_count, fts_hits, vector_hits, answer_mode, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    session_id,
                    query[:4000],
                    mode,
                    collection_id,
                    doc_filter[:1000],
                    latency_ms,
                    hit_count,
                    fts_hits,
                    vector_hits,
                    answer_mode,
                    _now(),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def recent_queries(limit: int = 12) -> list[dict[str, Any]]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT query, mode, latency_ms, hit_count, fts_hits, vector_hits, "
                "answer_mode, created_at FROM cf_queries ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def server_version() -> str:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS version")
            row = cur.fetchone()
            return str(row["version"]) if row else ""
    finally:
        conn.close()


def list_collections() -> list[dict[str, Any]]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT collection_id, COUNT(*) AS documents, COALESCE(SUM(page_count), 0) AS pages,
                       COALESCE(SUM(char_count), 0) AS chars, COALESCE(SUM(chunk_count), 0) AS chunks
                FROM cf_documents
                GROUP BY collection_id
                ORDER BY collection_id
                """
            )
            return [
                {
                    "collection_id": row["collection_id"],
                    "documents": int(row["documents"]),
                    "pages": int(row["pages"]),
                    "chars": int(row["chars"]),
                    "chunks": int(row["chunks"]),
                }
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


def set_last_answer_mode(user_id: str, session_id: str, answer_mode: str) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM cf_queries
                WHERE user_id = %s AND session_id = %s
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, session_id),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE cf_queries SET answer_mode = %s WHERE id = %s",
                    (answer_mode[:16], row["id"]),
                )
        conn.commit()
    finally:
        conn.close()


def counts() -> dict[str, int]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM cf_documents")
            documents = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM cf_chunks")
            chunks = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM cf_queries")
            queries = int(cur.fetchone()["n"])
            cur.execute("SELECT COALESCE(SUM(page_count), 0) AS n FROM cf_documents")
            pages = int(cur.fetchone()["n"])
            cur.execute("SELECT COALESCE(SUM(char_count), 0) AS n FROM cf_documents")
            chars = int(cur.fetchone()["n"])
            return {
                "documents": documents,
                "chunks": chunks,
                "pages": pages,
                "chars": chars,
                "queries": queries,
            }
    finally:
        conn.close()


def usage_stats() -> dict[str, Any]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT latency_ms, fts_hits, vector_hits, mode, answer_mode, created_at "
                "FROM cf_queries ORDER BY id DESC LIMIT 500"
            )
            rows = list(cur.fetchall())
            cur.execute(
                "SELECT DATE(created_at) AS day, COUNT(*) AS n FROM cf_queries "
                "GROUP BY DATE(created_at) ORDER BY day DESC LIMIT 7"
            )
            daily = list(cur.fetchall())
    finally:
        conn.close()

    latencies = sorted(int(row["latency_ms"]) for row in rows)

    def percentile(values: list[int], ratio: float) -> float:
        if not values:
            return 0.0
        index = min(len(values) - 1, max(0, int(round((len(values) - 1) * ratio))))
        return float(values[index])

    total = counts()
    return {
        "totals": total,
        "sample_size": len(rows),
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "avg": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        },
        "answer_modes": _count_by(rows, "answer_mode"),
        "daily": [{"day": str(row["day"]), "count": int(row["n"])} for row in daily],
    }


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        value = row.get(key) or "search"
        result[value] = result.get(value, 0) + 1
    return result


def mirror_memory(
    *,
    user_id: str,
    kind: str,
    text: str,
    backend: str,
    pc_entry_id: str = "",
    pc_request_id: str = "",
) -> None:
    now = _now()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cf_memories
                  (user_id, kind, text, backend, pc_entry_id, pc_request_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, kind, text, backend, pc_entry_id, pc_request_id, now, now),
            )
        conn.commit()
    finally:
        conn.close()


def list_memories(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, text, backend, pc_entry_id, pc_request_id, created_at "
                "FROM cf_memories WHERE user_id = %s ORDER BY id DESC LIMIT %s",
                (user_id, limit),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def search_memories_fallback(user_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            like = f"%{query.strip()[:80]}%"
            cur.execute(
                """
                SELECT id, kind, text, backend, created_at FROM cf_memories
                WHERE user_id = %s
                  AND (kind = 'preference' OR text LIKE %s)
                ORDER BY updated_at DESC LIMIT %s
                """,
                (user_id, like, limit),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def set_progress(user_id: str, document_id: str, page_no: int) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cf_progress (user_id, document_id, page_no, updated_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE document_id = VALUES(document_id),
                  page_no = VALUES(page_no), updated_at = VALUES(updated_at)
                """,
                (user_id, document_id, page_no, _now()),
            )
        conn.commit()
    finally:
        conn.close()


def get_progress(user_id: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.user_id, p.document_id, p.page_no, p.updated_at,
                       d.title, d.kind, d.page_count
                FROM cf_progress p
                LEFT JOIN cf_documents d ON d.id = p.document_id
                WHERE p.user_id = %s
                """,
                (user_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()
