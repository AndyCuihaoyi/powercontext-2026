"""Runtime configuration for CiteFlow.

All values can be overridden with environment variables. Secrets (LLM API key)
are read from the environment only and are never written to logs or the repo.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("CITEFLOW_RUNTIME_DIR", ROOT / "runtime")).resolve()
UPLOAD_DIR = RUNTIME_DIR / "uploads"
PAGE_CACHE_DIR = RUNTIME_DIR / "page_cache"
EMBEDDING_CACHE_DIR = RUNTIME_DIR / "embedding_cache"
SAMPLES_DIR = ROOT / "data" / "samples"
WEB_DIR = ROOT / "app" / "web"

DEFAULT_COLLECTION = os.environ.get("CITEFLOW_DEFAULT_COLLECTION", "我的书架")
DEMO_COLLECTION = os.environ.get("CITEFLOW_DEMO_COLLECTION", "示例资料")

SEEKDB_HOST = os.environ.get("CITEFLOW_SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = int(os.environ.get("CITEFLOW_SEEKDB_PORT", "2891"))
SEEKDB_USER = os.environ.get("CITEFLOW_SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.environ.get("CITEFLOW_SEEKDB_PASSWORD", "")
SEEKDB_DATABASE = os.environ.get("CITEFLOW_SEEKDB_DATABASE", "citeflow")

EMBEDDING_BACKEND = os.environ.get("CITEFLOW_EMBEDDING_BACKEND", "auto").lower()
EMBEDDING_MODEL = os.environ.get(
    "CITEFLOW_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
EMBEDDING_DIM = int(os.environ.get("CITEFLOW_EMBEDDING_DIM", "384"))
EMBEDDING_BATCH_SIZE = int(os.environ.get("CITEFLOW_EMBEDDING_BATCH_SIZE", "32"))
HF_ENDPOINT = os.environ.get("CITEFLOW_HF_ENDPOINT", "")

POWERCONTEXT_URL = os.environ.get("CITEFLOW_POWERCONTEXT_URL", "http://127.0.0.1:17429").rstrip("/")
POWERCONTEXT_MODE = os.environ.get("CITEFLOW_POWERCONTEXT", "auto").lower()  # auto / off
POWERCONTEXT_TIMEOUT = float(os.environ.get("CITEFLOW_POWERCONTEXT_TIMEOUT", "2.0"))
POWERCONTEXT_SCOPE_FILE = RUNTIME_DIR / "powercontext_scope.json"
POWERCONTEXT_SCOPE_TITLE = "CiteFlow 研读记忆"
POWERCONTEXT_SCOPE_SUMMARY = "CiteFlow 本地研读助手的偏好、进度与结论记忆"
POWERCONTEXT_SCOPE_KEY = "citeflow-local-v1"

LLM_BASE_URL = os.environ.get("CITEFLOW_LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("CITEFLOW_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("CITEFLOW_LLM_MODEL", "")
LLM_TIMEOUT = float(os.environ.get("CITEFLOW_LLM_TIMEOUT", "60"))
LLM_FORCE_OFF = os.environ.get("CITEFLOW_LLM", "auto").lower() == "off"

MAX_UPLOAD_BYTES = int(os.environ.get("CITEFLOW_MAX_UPLOAD_MB", "30")) * 1024 * 1024
MAX_QUERY_CHARS = 2000
TOP_K_DEFAULT = 8
PER_CHANNEL_LIMIT = 24
RRF_K = 60

APP_HOST = os.environ.get("CITEFLOW_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("CITEFLOW_PORT", "8848"))

ALLOWED_EXTENSIONS = {".pdf": "pdf", ".md": "markdown", ".markdown": "markdown", ".txt": "text"}


def ensure_dirs() -> None:
    for path in (RUNTIME_DIR, UPLOAD_DIR, PAGE_CACHE_DIR, EMBEDDING_CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def llm_available() -> bool:
    return bool(not LLM_FORCE_OFF and LLM_BASE_URL and LLM_API_KEY and LLM_MODEL)
