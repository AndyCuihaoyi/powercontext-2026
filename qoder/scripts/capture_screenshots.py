#!/usr/bin/env python3
"""Capture the screenshots used by the README and the Qoder Sites page.

Requires the dev extra and a Chromium bundle:
  uv pip install -r requirements-dev.txt
  playwright install chromium
  .venv/bin/python scripts/capture_screenshots.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHROME = os.environ.get(
    "PLAYWRIGHT_CHROMIUM",
    str(Path.home() / ".cache/ms-playwright/chromium-1187/chrome-linux/chrome"),
)


async def _wait_answer(page) -> None:
    await page.wait_for_selector(".answer-card", timeout=120_000)
    await page.wait_for_timeout(1800)


async def capture(base_url: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    executable = DEFAULT_CHROME if Path(DEFAULT_CHROME).exists() else None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--no-sandbox"],
        )
        page = await browser.new_page(
            viewport={"width": 1500, "height": 1100}, device_scale_factor=1.25
        )
        errors: list[str] = []
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        await page.goto(base_url, wait_until="networkidle")
        await page.wait_for_selector("#suggestions button", timeout=30_000)
        await page.wait_for_timeout(1200)
        await page.screenshot(path=out_dir / "home.png")

        await page.click('#suggestions button:has-text("3M 供应链风险")')
        await _wait_answer(page)
        await page.screenshot(path=out_dir / "answer.png")

        await page.click("#view-toggle button[data-view='image']")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=out_dir / "evidence.png")

        await page.click(".tabs .tab[data-tab='memory']")
        await page.wait_for_timeout(800)
        await page.eval_on_selector("#tab-memory", "node => { node.scrollTop = 0; }")
        await page.screenshot(path=out_dir / "memory.png")

        await page.click(".tabs .tab[data-tab='tech']")
        await page.click("#btn-explain")
        await page.wait_for_function(
            "() => !document.querySelector('#tech-plan').textContent.includes('点击')",
            timeout=60_000,
        )
        await page.click("#btn-stats")
        await page.wait_for_timeout(1200)
        await page.screenshot(path=out_dir / "tech.png")

        await page.click('.doc-item:has-text("3M 2022 10-K") .doc-main')
        await page.fill("#query", "raw materials supply chain risk")
        await page.click("#btn-ask")
        await _wait_answer(page)
        await page.click(".tabs .tab[data-tab='tech']")
        await page.click("#btn-explain")
        await page.wait_for_function(
            "() => !document.querySelector('#tech-plan').textContent.includes('点击')",
            timeout=60_000,
        )
        await page.screenshot(path=out_dir / "tech-filtered.png")

        mobile = await browser.new_page(
            viewport={"width": 390, "height": 844}, device_scale_factor=2
        )
        await mobile.goto(base_url, wait_until="networkidle")
        await mobile.wait_for_selector("#suggestions button", timeout=30_000)
        await mobile.wait_for_timeout(1200)
        await mobile.locator(".ask-card").scroll_into_view_if_needed()
        await mobile.wait_for_timeout(500)
        await mobile.screenshot(path=out_dir / "mobile.png")

        await browser.close()
        if errors:
            print("浏览器控制台错误：")
            for error in errors:
                print(" -", error)
            raise SystemExit(1)
    print(f"截图已写入 {out_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8848")
    parser.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / "site" / "assets")
    args = parser.parse_args()
    asyncio.run(capture(args.base_url, args.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
