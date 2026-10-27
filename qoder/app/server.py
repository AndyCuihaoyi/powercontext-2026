"""CiteFlow HTTP service: library, hybrid retrieval, grounded answers, memory."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import answer as answer_mod
from . import config, db, embeddings, ingest, memory, retrieval

logger = logging.getLogger("citeflow")
STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    try:
        db.init_schema()
    except Exception as exc:  # noqa: BLE001 - the UI shows a degraded state
        logger.warning("seekdb 初始化失败: %s", exc)
    threading.Thread(target=embeddings.warmup_provider, daemon=True).start()
    yield


app = FastAPI(title="CiteFlow", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def session_cookie(request: Request, call_next):
    response = await call_next(request)
    if not request.cookies.get("citeflow_session"):
        response.set_cookie(
            "citeflow_session",
            "ses_" + secrets.token_hex(6),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
    return response


class SearchBody(BaseModel):
    query: str
    collection_id: str | None = None
    document_ids: list[str] = Field(default_factory=list)
    top_k: int = config.TOP_K_DEFAULT
    language: str = "auto"
    use_llm: bool = True


class MemoryBody(BaseModel):
    kind: str = "insight"
    text: str


class MemorySearchBody(BaseModel):
    query: str
    limit: int = 5


class ProgressBody(BaseModel):
    document_id: str
    page_no: int


def _user(request: Request) -> str:
    value = (request.headers.get("x-citeflow-user") or "local").strip()
    return value[:64] or "local"


def _session(request: Request) -> str:
    return (request.cookies.get("citeflow_session") or "anonymous")[:64]


def _public_doc(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id", "collection_id", "title", "filename", "kind", "page_count",
        "chunk_count", "char_count", "created_at",
    )
    return {key: row.get(key) for key in keys}


def _public_hit(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": hit["id"],
        "document_id": hit["document_id"],
        "doc_title": hit.get("doc_title", ""),
        "doc_kind": hit.get("doc_kind", ""),
        "filename": hit.get("filename", ""),
        "page_no": hit["page_no"],
        "doc_page_count": hit.get("doc_page_count", 0),
        "heading": hit.get("heading", ""),
        "text": hit["text"],
        "snippet": hit["text"][:260],
        "matched_by": hit.get("matched_by", ""),
        "channels": hit.get("channels", {}),
        "rrf_score": hit.get("rrf_score", 0.0),
    }


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ValueError,)) or exc.__class__.__name__ == "InvalidFileError":
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, db.DBError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


def _sample_files() -> list[Path]:
    if not config.SAMPLES_DIR.exists():
        return []
    return sorted(
        path
        for path in config.SAMPLES_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in config.ALLOWED_EXTENSIONS
    )


@app.get("/api/health")
def health() -> dict[str, Any]:
    seekdb_ok = False
    version = ""
    counts: dict[str, int] = {}
    detail = ""
    try:
        seekdb_ok = db.ping()
        version = db.server_version()
        counts = db.counts()
    except db.DBError as exc:
        detail = str(exc)
    provider = embeddings.provider_info()
    return {
        "status": "ok" if seekdb_ok else "degraded",
        "uptime_s": int(time.time() - STARTED_AT),
        "seekdb": {"ok": seekdb_ok, "version": version, "detail": detail},
        "embedding": provider,
        "memory": memory.status(),
        "llm": {
            "available": config.llm_available(),
            "model": config.LLM_MODEL or "",
            "disabled": config.LLM_FORCE_OFF,
        },
        "counts": counts,
    }


@app.get("/api/home")
def home(request: Request) -> dict[str, Any]:
    user = _user(request)
    try:
        documents = [_public_doc(row) for row in db.list_documents()]
        collections = db.list_collections()
        counts = db.counts()
        recent = db.recent_queries(8)
    except db.DBError as exc:
        raise _http_error(exc) from exc

    known = {item["collection_id"] for item in collections}
    for name in (config.DEFAULT_COLLECTION, config.DEMO_COLLECTION):
        if name not in known:
            collections.insert(
                0,
                {"collection_id": name, "documents": 0, "pages": 0, "chars": 0, "chunks": 0},
            )
    collections.sort(
        key=lambda item: (
            0 if item["collection_id"] == config.DEFAULT_COLLECTION else
            1 if item["collection_id"] == config.DEMO_COLLECTION else 2,
            item["collection_id"],
        )
    )
    demo_seeded = any(doc["collection_id"] == config.DEMO_COLLECTION for doc in documents)
    return {
        "collections": collections,
        "documents": documents,
        "counts": counts,
        "recent_queries": recent,
        "memory": memory.status(),
        "progress": memory.get_progress(user),
        "demo": {
            "seeded": demo_seeded,
            "sample_files": [path.name for path in _sample_files()],
        },
        "embedding": embeddings.provider_info(),
        "llm_available": config.llm_available(),
    }


@app.get("/api/documents/{document_id}")
def document_detail(document_id: str) -> dict[str, Any]:
    try:
        document = db.get_document(document_id)
    except db.DBError as exc:
        raise _http_error(exc) from exc
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    return _public_doc(document)


@app.post("/api/upload")
def upload(
    request: Request,
    file: UploadFile = File(...),
    collection_id: str | None = Form(None),
    title: str | None = Form(None),
) -> dict[str, Any]:
    data = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    filename = file.filename or "upload.txt"
    collection = (collection_id or config.DEFAULT_COLLECTION).strip() or config.DEFAULT_COLLECTION
    try:
        result = ingest.ingest_bytes(
            data,
            filename,
            collection_id=collection,
            title=(title or "").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    result["document"] = _public_doc(result["document"])
    return result


@app.post("/api/demo/seed")
def demo_seed() -> dict[str, Any]:
    if not _sample_files():
        raise HTTPException(status_code=400, detail="data/samples 下没有可导入的样例文件")
    try:
        return ingest.seed_samples(config.DEMO_COLLECTION)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: str) -> dict[str, Any]:
    try:
        document = db.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="文档不存在")
        deleted = db.delete_document(document_id)
        stored = config.RUNTIME_DIR / document["stored_path"]
        stored.unlink(missing_ok=True)
        for cached in config.PAGE_CACHE_DIR.glob(f"{document_id}_*.png"):
            cached.unlink(missing_ok=True)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    return {"deleted": deleted, "document_id": document_id}


@app.get("/api/documents/{document_id}/page/{page_no}")
def document_page(request: Request, document_id: str, page_no: int) -> dict[str, Any]:
    user = _user(request)
    try:
        payload = ingest.page_text(document_id, page_no)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    try:
        memory.record_progress(user, document_id, page_no, payload["title"])
    except Exception:  # noqa: BLE001 - progress is best effort
        pass
    payload["progress"] = memory.get_progress(user)
    return payload


@app.get("/api/documents/{document_id}/page/{page_no}/image")
def document_page_image(document_id: str, page_no: int) -> Response:
    try:
        payload = ingest.page_image(document_id, page_no)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    return Response(
        content=payload,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _run_search(body: SearchBody, request: Request, mode: str) -> tuple[dict[str, Any], str, str]:
    user = _user(request)
    session = _session(request)
    try:
        result = retrieval.search(
            body.query,
            collection_id=body.collection_id,
            document_ids=body.document_ids,
            top_k=body.top_k,
            user_id=user,
            session_id=session,
            mode=mode,
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    return result, user, session


@app.post("/api/search")
def search(body: SearchBody, request: Request) -> dict[str, Any]:
    result, _, _ = _run_search(body, request, "search")
    return {
        "hits": [_public_hit(hit) for hit in result["hits"]],
        "trace": result["trace"],
        "answer": None,
        "memory": None,
    }


@app.post("/api/ask")
def ask(body: SearchBody, request: Request) -> dict[str, Any]:
    result, user, session = _run_search(body, request, "ask")
    recall = memory.recall(user, body.query, limit=5)
    memory_texts = [hit["text"] for hit in recall.get("hits", [])]
    answer = answer_mod.answer(
        body.query,
        result["hits"],
        memory_texts=memory_texts,
        language=body.language,
        use_llm=body.use_llm,
    )
    try:
        db.set_last_answer_mode(user, session, answer["mode"])
    except Exception:  # noqa: BLE001 - stats must not block answers
        pass
    return {
        "hits": [_public_hit(hit) for hit in result["hits"]],
        "trace": result["trace"],
        "answer": answer,
        "memory": recall,
    }


@app.get("/api/retrieval/last")
def retrieval_last() -> dict[str, Any]:
    return retrieval.last_evidence()


@app.get("/api/retrieval/explain")
def retrieval_explain() -> dict[str, Any]:
    try:
        payload = retrieval.explain_last()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except db.DBError as exc:
        raise _http_error(exc) from exc
    payload["plan"] = payload.get("plan", "")[:12000]
    return payload


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    try:
        payload = db.usage_stats()
        payload["recent_queries"] = db.recent_queries(15)
    except db.DBError as exc:
        raise _http_error(exc) from exc
    payload["embedding"] = embeddings.provider_info()
    payload["memory"] = memory.status()
    payload["llm_available"] = config.llm_available()
    return payload


@app.get("/api/memory")
def memory_list(request: Request) -> dict[str, Any]:
    user = _user(request)
    try:
        return {
            "status": memory.status(),
            "memories": memory.list_memories(user),
            "progress": memory.get_progress(user),
            "scope_file": str(config.POWERCONTEXT_SCOPE_FILE.name),
        }
    except db.DBError as exc:
        raise _http_error(exc) from exc


@app.post("/api/memory")
def memory_add(body: MemoryBody, request: Request) -> dict[str, Any]:
    try:
        record = memory.remember(_user(request), body.kind, body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except db.DBError as exc:
        raise _http_error(exc) from exc
    return {"record": record, "status": memory.status()}


@app.post("/api/memory/search")
def memory_search(body: MemorySearchBody, request: Request) -> dict[str, Any]:
    return memory.recall(_user(request), body.query, limit=max(1, min(body.limit, 20)))


@app.get("/api/memory/context")
def memory_context(request: Request, q: str = "阅读偏好") -> dict[str, Any]:
    return memory.prepare_context(_user(request), q)


@app.post("/api/progress")
def save_progress(body: ProgressBody, request: Request) -> dict[str, Any]:
    document = db.get_document(body.document_id)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    page_no = int(body.page_no)
    if page_no < 1 or page_no > int(document["page_count"]):
        raise HTTPException(status_code=400, detail="页码超出范围")
    try:
        return memory.record_progress(
            _user(request), body.document_id, page_no, document["title"]
        )
    except db.DBError as exc:
        raise _http_error(exc) from exc


@app.exception_handler(db.DBError)
async def db_error_handler(_: Request, exc: db.DBError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


if config.WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    path = config.WEB_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=500, detail="前端尚未构建：app/web/index.html 缺失")
    return FileResponse(path)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.server:app",
        host=config.APP_HOST,
        port=config.APP_PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
