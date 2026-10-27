"""PowerContext memory layer with a seekdb mirror and offline fallback.

PowerContext stores long-lived reading preferences, insights and progress in a
scope. Every write is mirrored into seekdb (`cf_memories`) so the UI can show
what was remembered even when the PowerContext service is stopped.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config, db

_KINDS = {"preference", "insight", "progress"}
_LOCK = threading.Lock()
_STATUS_CACHE: dict[str, Any] = {"at": 0.0, "value": None}


class MemoryUnavailable(RuntimeError):
    pass


def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> tuple[dict[str, Any], str]:
    url = f"{config.POWERCONTEXT_URL}{path}"
    try:
        response = httpx.request(
            method,
            url,
            json=payload,
            timeout=timeout or config.POWERCONTEXT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        raise MemoryUnavailable(f"PowerContext 不可用: {exc}") from exc
    if response.status_code >= 400:
        detail = response.text[:400]
        raise MemoryUnavailable(f"PowerContext {response.status_code}: {detail}")
    request_id = response.headers.get("x-powercontext-request-id", "")
    try:
        return response.json(), request_id
    except ValueError as exc:
        raise MemoryUnavailable("PowerContext 返回非 JSON 响应") from exc


def status(*, refresh: bool = False) -> dict[str, Any]:
    if config.POWERCONTEXT_MODE == "off":
        return {
            "mode": "off",
            "available": False,
            "url": config.POWERCONTEXT_URL,
            "detail": "已通过 CITEFLOW_POWERCONTEXT=off 关闭",
            "scope_id": "",
        }
    now = time.time()
    cached = _STATUS_CACHE["value"]
    if not refresh and cached and now - _STATUS_CACHE["at"] < 5:
        return cached

    started = time.perf_counter()
    result: dict[str, Any]
    try:
        health, _ = _request("GET", "/health/ready", timeout=min(2.0, config.POWERCONTEXT_TIMEOUT))
        checks = health.get("checks", {})
        result = {
            "mode": "powercontext",
            "available": health.get("status") == "ready",
            "url": config.POWERCONTEXT_URL,
            "database": checks.get("database", ""),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "detail": f"scope 记忆在线（数据库 {checks.get('database', 'unknown')}）",
            "scope_id": _scope_id_from_file() or "",
        }
    except MemoryUnavailable as exc:
        result = {
            "mode": "seekdb-fallback",
            "available": False,
            "url": config.POWERCONTEXT_URL,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "detail": f"已降级到 seekdb 镜像记忆（{exc}）",
            "scope_id": "",
        }
    _STATUS_CACHE["at"] = now
    _STATUS_CACHE["value"] = result
    return result


def _scope_id_from_file() -> str | None:
    path = config.POWERCONTEXT_SCOPE_FILE
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scope_id = str(payload.get("scope_id") or "").strip()
    return scope_id or None


def _persist_scope(scope_id: str, title: str) -> None:
    config.ensure_dirs()
    config.POWERCONTEXT_SCOPE_FILE.write_text(
        json.dumps(
            {
                "scope_id": scope_id,
                "title": title,
                "url": config.POWERCONTEXT_URL,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def ensure_scope() -> str:
    with _LOCK:
        existing = _scope_id_from_file()
        if existing:
            return existing
        scopes, _ = _request("GET", "/v1/scopes", timeout=config.POWERCONTEXT_TIMEOUT)
        for item in scopes.get("items", []):
            if item.get("title") == config.POWERCONTEXT_SCOPE_TITLE:
                scope_id = str(item["scope_id"])
                _persist_scope(scope_id, config.POWERCONTEXT_SCOPE_TITLE)
                return scope_id
        created, _ = _request(
            "POST",
            "/v1/scopes",
            {
                "title": config.POWERCONTEXT_SCOPE_TITLE,
                "summary": config.POWERCONTEXT_SCOPE_SUMMARY,
                "idempotency_key": config.POWERCONTEXT_SCOPE_KEY,
            },
            timeout=config.POWERCONTEXT_TIMEOUT,
        )
        scope_id = str(created["scope_id"])
        _persist_scope(scope_id, config.POWERCONTEXT_SCOPE_TITLE)
        return scope_id


def _normalize_kind(kind: str) -> str:
    kind = (kind or "").strip().lower()
    return kind if kind in _KINDS else "insight"


def _kind_tag(kind: str, user_id: str) -> str:
    return f"{kind}@{user_id}"[:64]


def _trim_text(text: str) -> str:
    text = " ".join((text or "").split())
    encoded = text.encode("utf-8")
    if len(encoded) <= 8000:
        return text
    return encoded[:8000].decode("utf-8", errors="ignore")


def remember(user_id: str, kind: str, text: str) -> dict[str, Any]:
    text = _trim_text(text)
    if not text:
        raise ValueError("记忆内容不能为空")
    kind = _normalize_kind(kind)
    result: dict[str, Any] = {
        "kind": kind,
        "text": text,
        "backend": "seekdb",
        "entry_id": "",
        "request_id": "",
        "scope_id": "",
        "ok": True,
    }
    if config.POWERCONTEXT_MODE != "off":
        try:
            scope_id = ensure_scope()
            response, request_id = _request(
                "POST",
                "/v1/memory/remember",
                {
                    "scope_id": scope_id,
                    "kind": _kind_tag(kind, user_id),
                    "text": text,
                    "reason": "citeflow remember",
                },
                timeout=max(4.0, config.POWERCONTEXT_TIMEOUT),
            )
            entry = response.get("entry") or {}
            citation = entry.get("citation", {})
            result.update(
                {
                    "backend": "powercontext",
                    "entry_id": citation.get("entry_id", ""),
                    "request_id": request_id,
                    "scope_id": scope_id,
                }
            )
        except MemoryUnavailable as exc:
            result["detail"] = str(exc)

    db.mirror_memory(
        user_id=user_id,
        kind=kind,
        text=text,
        backend=result["backend"],
        pc_entry_id=result["entry_id"],
        pc_request_id=result["request_id"],
    )
    return result


def remember_preference(user_id: str, text: str) -> dict[str, Any]:
    return remember(user_id, "preference", text)


def remember_insight(user_id: str, text: str) -> dict[str, Any]:
    return remember(user_id, "insight", text)


def _mirror_kind_map(user_id: str) -> dict[str, str]:
    return {row["text"]: row["kind"] for row in db.list_memories(user_id, limit=200)}


def _kind_from_tag(text: str, tags: list[str]) -> str:
    if tags:
        for tag in tags:
            prefix = str(tag).split("@", 1)[0]
            if prefix in _KINDS:
                return prefix
    return ""


def recall(user_id: str, query: str, limit: int = 5) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"backend": "seekdb", "hits": []}
    if config.POWERCONTEXT_MODE != "off":
        try:
            scope_id = ensure_scope()
            response, request_id = _request(
                "POST",
                "/v1/memory/search",
                {"scope_id": scope_id, "query": query[:500], "limit": max(1, limit), "mode": "fts"},
                timeout=max(3.0, config.POWERCONTEXT_TIMEOUT),
            )
            known = _mirror_kind_map(user_id)
            hits = []
            for hit in response.get("hits", []):
                text = str(hit.get("text", "")).strip()
                kind = known.get(text)
                if kind is None:
                    continue
                hits.append(
                    {
                        "text": text,
                        "kind": kind,
                        "score": round(float(hit.get("score", 0.0)), 4),
                        "matched_by": hit.get("matched_by", []),
                        "backend": "powercontext",
                        "entry_id": (hit.get("citation") or {}).get("entry_id", ""),
                    }
                )
            return {
                "backend": "powercontext",
                "scope_id": scope_id,
                "request_id": request_id,
                "hits": hits[:limit],
            }
        except MemoryUnavailable as exc:
            fallback_reason = str(exc)
    else:
        fallback_reason = "PowerContext 已关闭"

    rows = db.search_memories_fallback(user_id, query, limit)
    return {
        "backend": "seekdb",
        "detail": fallback_reason,
        "hits": [
            {
                "text": row["text"],
                "kind": row["kind"],
                "score": 0.0,
                "matched_by": ["seekdb-like"],
                "backend": row.get("backend", "seekdb"),
                "entry_id": "",
            }
            for row in rows
        ],
    }


def list_memories(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    rows = db.list_memories(user_id, limit)
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "text": row["text"],
            "backend": row["backend"],
            "entry_id": row.get("pc_entry_id", ""),
            "request_id": row.get("pc_request_id", ""),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def prepare_context(user_id: str, query: str, max_bytes: int = 4000) -> dict[str, Any]:
    if config.POWERCONTEXT_MODE == "off":
        return {"available": False, "detail": "PowerContext 已关闭", "content": ""}
    try:
        scope_id = ensure_scope()
        response, request_id = _request(
            "POST",
            "/v1/context/prepare",
            {"scope_id": scope_id, "query": query[:1000], "max_bytes": max_bytes},
            timeout=max(4.0, config.POWERCONTEXT_TIMEOUT),
        )
        return {
            "available": True,
            "scope_id": scope_id,
            "request_id": request_id,
            "status": response.get("status", ""),
            "content": response.get("content") or "",
            "content_bytes": int(response.get("content_bytes", 0) or 0),
        }
    except MemoryUnavailable as exc:
        return {"available": False, "detail": str(exc), "content": ""}


def get_progress(user_id: str) -> dict[str, Any] | None:
    row = db.get_progress(user_id)
    if not row:
        return None
    return {
        "document_id": row["document_id"],
        "page_no": int(row["page_no"]),
        "title": row.get("title") or "",
        "kind": row.get("kind") or "",
        "page_count": int(row.get("page_count") or 0),
        "updated_at": str(row["updated_at"]),
    }


def record_progress(user_id: str, document_id: str, page_no: int, title: str) -> dict[str, Any]:
    previous = db.get_progress(user_id)
    db.set_progress(user_id, document_id, page_no)
    should_remember = (
        previous is None
        or previous["document_id"] != document_id
        or abs(int(previous["page_no"]) - int(page_no)) >= 5
    )
    remembered = None
    if should_remember:
        try:
            remembered = remember(
                user_id,
                "progress",
                f"正在阅读《{title}》第 {page_no} 页",
            )
        except ValueError:
            remembered = None
    return {"progress": get_progress(user_id), "remembered": remembered}
