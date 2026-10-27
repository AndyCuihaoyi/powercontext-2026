"""Local embedding providers.

Default is a multilingual sentence-transformer exported to ONNX and run by
fastembed. If the model cannot be loaded (for example offline first run), the
app falls back to a deterministic hashing embedder so every feature still runs.
The active backend is always reported in the UI and in /api/health.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from typing import Iterable

from . import config

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")


def _normalize(vector: list[float]) -> list[float]:
    norm = sum(value * value for value in vector) ** 0.5
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def _tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _WORD_RE.findall(lowered)
    for run in _CJK_RE.findall(lowered):
        for width in (2, 3):
            tokens.extend(run[i : i + width] for i in range(max(0, len(run) - width + 1)))
    return tokens


class HashEmbedding:
    """Deterministic lexical fallback: char n-grams hashed into fixed dimensions."""

    backend = "hash"
    model = "blake2b-ngram"

    def __init__(self, dim: int = config.EMBEDDING_DIM) -> None:
        self.dim = dim
        self.status = "fallback: 未加载语义模型，使用确定性词法向量"

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in _tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        return _normalize(vector)


class FastEmbedEmbedding:
    backend = "fastembed"

    def __init__(
        self,
        model_name: str = config.EMBEDDING_MODEL,
        cache_dir: str | None = None,
        dim: int = config.EMBEDDING_DIM,
    ) -> None:
        self.model = model_name
        self.dim = dim
        self.cache_dir = cache_dir or str(config.EMBEDDING_CACHE_DIR)
        self.status = "not loaded"
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            if config.HF_ENDPOINT:
                os.environ.setdefault("HF_ENDPOINT", config.HF_ENDPOINT)
            from fastembed import TextEmbedding

            threads = max(1, min(8, os.cpu_count() or 4))
            self._model = TextEmbedding(
                model_name=self.model,
                cache_dir=self.cache_dir,
                threads=threads,
            )
            self.status = "ready"
            return self._model

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        items = [text if text.strip() else " " for text in texts]
        if not items:
            return []
        model = self._ensure_model()
        vectors = model.embed(items, batch_size=config.EMBEDDING_BATCH_SIZE)
        result = [_normalize([float(value) for value in vector]) for vector in vectors]
        if result and len(result[0]) != self.dim:
            raise RuntimeError(f"embedding dimension mismatch: expected {self.dim}, got {len(result[0])}")
        return result

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def warmup(self) -> None:
        self.embed_query("CiteFlow warmup")


_provider = None
_provider_lock = threading.Lock()


def get_provider():
    global _provider
    if _provider is not None:
        return _provider
    with _provider_lock:
        if _provider is not None:
            return _provider
        backend = config.EMBEDDING_BACKEND
        if backend == "hash":
            _provider = HashEmbedding()
        elif backend == "fastembed":
            candidate = FastEmbedEmbedding()
            try:
                candidate.warmup()
                _provider = candidate
            except Exception as exc:  # noqa: BLE001 - surface as a visible degraded mode
                fallback = HashEmbedding()
                fallback.status = f"fallback: fastembed 加载失败（{type(exc).__name__}: {str(exc)[:160]}）"
                _provider = fallback
        else:  # auto
            _provider = FastEmbedEmbedding()
        return _provider


def warmup_provider() -> None:
    provider = get_provider()
    if isinstance(provider, FastEmbedEmbedding) and provider.status != "ready":
        try:
            provider.warmup()
        except Exception as exc:  # noqa: BLE001
            global _provider
            fallback = HashEmbedding()
            fallback.status = f"fallback: fastembed 加载失败（{type(exc).__name__}: {str(exc)[:160]}）"
            _provider = fallback


def provider_info() -> dict:
    provider = _provider
    if provider is None:
        return {
            "backend": "pending",
            "configured": config.EMBEDDING_BACKEND,
            "model": config.EMBEDDING_MODEL,
            "dim": config.EMBEDDING_DIM,
            "status": "模型在首次检索或导入时加载",
        }
    return {
        "backend": provider.backend,
        "configured": config.EMBEDDING_BACKEND,
        "model": provider.model,
        "dim": provider.dim,
        "status": provider.status,
    }


def reset_provider() -> None:
    global _provider
    _provider = None
