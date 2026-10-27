#!/usr/bin/env python3
"""End-to-end verification against a running CiteFlow instance.

Checks the five acceptance points that can be verified locally:
  * sample ingestion and page-level evidence
  * hybrid retrieval (FTS + vector in one SQL) and its EXPLAIN plan
  * extractive answers with resolvable citations
  * PowerContext write / recall / context.prepare reuse
  * usage statistics

Run:  .venv/bin/python scripts/verify_demo.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("CITEFLOW_RUNTIME_DIR", PROJECT_ROOT / "runtime"))

QUERIES = [
    ("3M 财报", "3M 在 2022 年面临的主要供应链和原材料风险是什么？"),
    ("Boeing 财报", "波音 2022 年的商用飞机交付与订单情况如何？"),
    ("中文方法笔记", "做财报研读时应该怎样记录证据？"),
    ("英文跨语言", "What are the main supply chain risks?"),
]
PREFERENCE = "分析财报时优先关注供应链、现金流与诉讼风险。"


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: Any = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)


def _short_hits(hits: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    return [
        {
            "title": hit.get("doc_title"),
            "kind": hit.get("doc_kind"),
            "page_no": hit.get("page_no"),
            "heading": hit.get("heading"),
            "matched_by": hit.get("matched_by"),
            "rrf_score": hit.get("rrf_score"),
            "snippet": (hit.get("snippet") or "")[:220],
        }
        for hit in hits[:limit]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8848")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--skip-seed", action="store_true")
    args = parser.parse_args()

    report = Report()
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "queries": [],
    }

    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
        try:
            health = client.get("/api/health").json()
        except Exception as exc:  # noqa: BLE001
            print(f"无法连接 CiteFlow：{exc}", file=sys.stderr)
            print("请先运行 scripts/run.sh。", file=sys.stderr)
            return 1
        result["health"] = health
        report.add(
            "seekdb 可连接",
            bool(health.get("seekdb", {}).get("ok")),
            health.get("seekdb", {}).get("version", ""),
        )
        backend = health.get("embedding", {}).get("backend", "")
        report.add(
            "嵌入后端可用",
            backend in {"fastembed", "hash"},
            f"{backend} · {health.get('embedding', {}).get('dim')}d · {health.get('embedding', {}).get('status', '')}",
        )

        if not args.skip_seed:
            seed = client.post("/api/demo/seed").json()
            result["seed"] = seed
            errors = [item for item in seed.get("files", []) if item.get("error")]
            report.add(
                "样例导入",
                not errors and len(seed.get("files", [])) >= 5,
                f"{len(seed.get('files', []))} 个文件，耗时 {seed.get('elapsed_ms', 0)} ms",
            )

        home = client.get("/api/home").json()
        result["library"] = {
            "counts": home.get("counts"),
            "collections": home.get("collections"),
            "documents": [
                {"title": doc["title"], "kind": doc["kind"], "pages": doc["page_count"]}
                for doc in home.get("documents", [])
            ],
        }
        report.add(
            "书架非空",
            (home.get("counts") or {}).get("documents", 0) >= 5,
            str(home.get("counts")),
        )

        plan_checked = False
        for label, query in QUERIES:
            response = client.post(
                "/api/ask",
                json={"query": query, "top_k": 8, "language": "auto", "use_llm": False},
            )
            response.raise_for_status()
            payload = response.json()
            answer = payload.get("answer") or {}
            hits = payload.get("hits") or []
            trace = payload.get("trace") or {}
            citations = answer.get("citations") or []
            bullets = answer.get("bullets") or []
            report.add(
                f"[{label}] 返回证据",
                bool(hits) and bool(citations),
                f"{len(hits)} hits · {len(citations)} citations · {len(bullets)} bullets · {answer.get('mode')}",
            )
            report.add(
                f"[{label}] 双通道召回",
                int(trace.get("fts_hits", 0)) > 0 and int(trace.get("vector_hits", 0)) > 0,
                f"fts={trace.get('fts_hits')} vector={trace.get('vector_hits')} fused={trace.get('fused_hits')}",
            )
            page_ok = False
            page_detail = ""
            if citations:
                citation = citations[0]
                page = client.get(
                    f"/api/documents/{citation['document_id']}/page/{citation['page_no']}"
                )
                page_ok = page.status_code == 200 and bool(page.json().get("text"))
                page_detail = f"{citation['title']} {citation['label']} → HTTP {page.status_code}"
                report.add(f"[{label}] 引用可回查原文页", page_ok, page_detail)

            if not plan_checked:
                explain = client.get("/api/retrieval/explain")
                if explain.status_code == 200:
                    explain_payload = explain.json()
                    plan = explain_payload.get("plan", "")
                    has_text = "TEXT RETRIEVAL SCAN" in plan
                    has_vector = "VECTOR INDEX" in plan
                    report.add(
                        "单 SQL 混合执行计划",
                        has_text and has_vector,
                        f"TEXT RETRIEVAL SCAN={has_text} · VECTOR INDEX={has_vector}",
                    )
                    result["hybrid"] = {
                        "sql": explain_payload.get("sql", ""),
                        "plan_excerpt": plan[:6000],
                        "has_text_scan": has_text,
                        "has_vector_scan": has_vector,
                    }
                    plan_checked = True
                else:
                    report.add("单 SQL 混合执行计划", False, f"EXPLAIN HTTP {explain.status_code}")

            result["queries"].append(
                {
                    "label": label,
                    "query": query,
                    "answer_mode": answer.get("mode"),
                    "answer_text": answer.get("text", ""),
                    "bullets": bullets,
                    "citations": citations,
                    "applied_memories": answer.get("applied_memories", []),
                    "trace": trace,
                    "hits": _short_hits(hits),
                }
            )

        # ---- Structured filter pushdown ------------------------------------------
        documents = home.get("documents", [])
        target = next(
            (doc for doc in documents if doc.get("kind") == "pdf" and "3M" in doc.get("title", "")),
            next((doc for doc in documents if doc.get("kind") == "pdf"), None),
        )
        if target:
            filtered = client.post(
                "/api/ask",
                json={
                    "query": "supply chain risk",
                    "document_ids": [target["id"]],
                    "top_k": 5,
                    "use_llm": False,
                },
            ).json()
            explain = client.get("/api/retrieval/explain")
            if explain.status_code == 200:
                explain_payload = explain.json()
                plan = explain_payload.get("plan", "")
                only_target = all(
                    hit.get("document_id") == target["id"] for hit in filtered.get("hits", [])
                )
                has_text = "TEXT RETRIEVAL SCAN" in plan
                has_prefilter = "ADAPTIVE SCAN (PRE-FILTER)" in plan
                report.add(
                    "结构化过滤下推",
                    only_target and has_text and has_prefilter,
                    f"doc={target['title']} · TEXT RETRIEVAL SCAN={has_text} · PRE-FILTER={has_prefilter}",
                )
                result["hybrid_filtered"] = {
                    "document": {"id": target["id"], "title": target["title"]},
                    "sql": explain_payload.get("sql", ""),
                    "plan_excerpt": plan[:6000],
                    "all_hits_in_document": only_target,
                }

        # ---- PowerContext memory -------------------------------------------------
        memory_state = client.get("/api/memory").json()
        result["memory"] = {"state": memory_state}
        status = memory_state.get("status") or {}
        report.add(
            "PowerContext 在线",
            status.get("mode") == "powercontext" and bool(status.get("available")),
            f"{status.get('mode')} · {status.get('detail', '')}",
        )
        memories = memory_state.get("memories") or []
        existing = next((item for item in memories if item.get("text") == PREFERENCE), None)
        if existing:
            record = existing
        else:
            created = client.post(
                "/api/memory", json={"kind": "preference", "text": PREFERENCE}
            ).json()
            record = created.get("record") or {}
        result["memory"]["record"] = record
        report.add(
            "记忆写入 PowerContext",
            record.get("backend") == "powercontext" and bool(record.get("entry_id")),
            f"entry={record.get('entry_id')} request={record.get('request_id')}",
        )

        search = client.post(
            "/api/memory/search", json={"query": "供应链 风险", "limit": 5}
        ).json()
        result["memory"]["search"] = search
        search_hits = search.get("hits") or []
        report.add(
            "记忆检索可复用",
            search.get("backend") == "powercontext"
            and any(item.get("text") == PREFERENCE for item in search_hits),
            f"{len(search_hits)} hits · request={search.get('request_id')}",
        )

        context = client.get("/api/memory/context", params={"q": "供应链风险"}).json()
        result["memory"]["context"] = context
        report.add(
            "context.prepare 可注入",
            bool(context.get("available")) and bool(context.get("content")),
            f"{context.get('content_bytes', 0)} bytes · request={context.get('request_id')}",
        )

        memory_answer = client.post(
            "/api/ask",
            json={
                "query": "供应链风险",
                "top_k": 8,
                "language": "auto",
                "use_llm": False,
            },
        ).json()
        applied = (memory_answer.get("answer") or {}).get("applied_memories") or []
        result["memory"]["applied_in_answer"] = applied
        report.add(
            "记忆进入回答上下文",
            PREFERENCE in applied,
            f"applied={len(applied)}",
        )

        # ---- Usage statistics ----------------------------------------------------
        stats = client.get("/api/stats").json()
        result["stats"] = stats
        totals = stats.get("totals") or {}
        report.add(
            "查询日志可核对",
            int(totals.get("queries", 0)) > 0,
            f"queries={totals.get('queries')} · p50={stats.get('latency_ms', {}).get('p50')} ms · "
            f"p95={stats.get('latency_ms', {}).get('p95')} ms",
        )

    result["checks"] = report.checks
    result["ok"] = report.ok
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    report_path = RUNTIME_DIR / "verify_report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{report_path}")
    print(f"结论：{'全部通过' if report.ok else '存在失败项'}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
