#!/usr/bin/env python3
"""Export a small, real usage snapshot for the static project site.

Reads the running CiteFlow /api/stats endpoint and (when present) the latest
end-to-end verification report, then writes site/data/usage.json. No query text
beyond what /api/stats already returns, and no credentials, are exported.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("CITEFLOW_BASE_URL", "http://127.0.0.1:8848").rstrip("/")
OUTPUT = ROOT / "site" / "data" / "usage.json"
REPORT = ROOT / "runtime" / "verify_report.json"


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _local_label(value: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo

        local = value.astimezone(ZoneInfo("Asia/Shanghai"))
    except Exception:
        local = value
    return local.strftime("%Y-%m-%d %H:%M UTC+8")


def main() -> None:
    stats = _get_json(f"{BASE_URL}/api/stats")
    now = datetime.now(timezone.utc)
    verification = {}
    if REPORT.exists():
        report = json.loads(REPORT.read_text(encoding="utf-8"))
        checks = report.get("checks", [])
        verification = {
            "ok": bool(report.get("ok")),
            "checks_passed": sum(1 for item in checks if item.get("ok")),
            "checks_total": len(checks),
            "generated_at": report.get("generated_at", ""),
        }

    payload = {
        "generated_at": now.isoformat(),
        "generated_at_label": _local_label(now),
        "source": f"GET {BASE_URL}/api/stats",
        "scope": "开发期本机验证数据，不是外部用户数据",
        "dataset": stats.get("totals", {}),
        "queries": {
            "total": int(stats.get("sample_size", 0) or 0),
            "p50_ms": float((stats.get("latency_ms") or {}).get("p50", 0) or 0),
            "p95_ms": float((stats.get("latency_ms") or {}).get("p95", 0) or 0),
            "avg_ms": float((stats.get("latency_ms") or {}).get("avg", 0) or 0),
            "modes": stats.get("answer_modes", {}),
        },
        "memory": {
            "mode": (stats.get("memory") or {}).get("mode", ""),
            "available": bool((stats.get("memory") or {}).get("available")),
            "scope_id": (stats.get("memory") or {}).get("scope_id", ""),
        },
        "embedding": {
            "backend": (stats.get("embedding") or {}).get("backend", ""),
            "model": (stats.get("embedding") or {}).get("model", ""),
            "dim": (stats.get("embedding") or {}).get("dim", 0),
        },
        "verification": verification,
        "recent_queries": [
            {
                "query": item.get("query", ""),
                "latency_ms": item.get("latency_ms", 0),
                "mode": item.get("mode", ""),
                "answer_mode": item.get("answer_mode", ""),
                "created_at": item.get("created_at", ""),
            }
            for item in stats.get("recent_queries", [])[:6]
        ],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {OUTPUT.relative_to(ROOT)} · "
        f"{payload['queries']['total']} queries · "
        f"p50 {payload['queries']['p50_ms']:.0f} ms · "
        f"p95 {payload['queries']['p95_ms']:.0f} ms"
    )


if __name__ == "__main__":
    main()
