#!/usr/bin/env python3
"""Load the bundled sample documents through the running CiteFlow API."""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8848")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    try:
        response = httpx.post(f"{args.base_url}/api/demo/seed", timeout=args.timeout)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"导入失败：{exc}", file=sys.stderr)
        print("请确认 CiteFlow 已启动（scripts/run.sh）。", file=sys.stderr)
        return 1
    payload = response.json()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    failed = [item for item in payload.get("files", []) if item.get("error")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
