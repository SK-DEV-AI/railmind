#!/usr/bin/env python3
"""Render IRCTC with a real browser and report whether our URL pre-fills it.

This is the check that killed the prefill claim on 2026-10-24: the page loads, the
form renders, and the fields are empty. It is kept in the repo so the claim can be
re-tested in one command instead of trusted.

Why the executable path is resolved rather than taken from Playwright: the Playwright
that `uv` installs may expect a different chromium build than the one already in the
cache. Pointing `executable_path` at a build that exists avoids a download and works
offline. Set `RAILMIND_CHROMIUM` to override; otherwise the newest cached build wins.

Nothing here logs in, searches, books or pays. It opens a page and reads the form.

Usage
    uv run --with playwright python scripts/irctc_check.py
    uv run --with playwright python scripts/irctc_check.py --shot /tmp/irctc.png
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

CHROMIUM_CACHE = Path.home() / ".cache/ms-playwright"


def chromium_executable() -> str | None:
    """The chromium binary to drive, or None to let Playwright pick its own."""
    override = os.environ.get("RAILMIND_CHROMIUM")
    if override:
        return override
    builds = sorted(CHROMIUM_CACHE.glob("chromium-*/chrome-linux64/chrome"))
    return str(builds[-1]) if builds else None


URLS = {
    "new-beta-with-params": "https://www.irctc.co.in/eticket/train-search"
    "?fromStation=CSMT&toStation=PUNE&date=20261026",
    "old-site-with-params": "https://www.irctc.co.in/nget/train-search"
    "?fromStation=CSMT&toStation=PUNE&date=20261026",
}
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
READ = """() => {
  const keys = /from|origin|dest|date|station|journey/i;
  return [...document.querySelectorAll('input')]
    .filter(el => keys.test([el.id, el.name, el.placeholder,
                             el.getAttribute('formcontrolname') || ''].join(' ')))
    .filter(el => el.offsetWidth || el.offsetHeight)
    .map(el => ({field: (el.placeholder || el.id || el.name || '?').slice(0, 30),
                 value: el.value}));
}"""


async def check(url: str, shot: Path | None) -> dict:
    executable = chromium_executable()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path=executable,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1400, "height": 1000},
            user_agent=UA,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            # First visit shows a language chooser; it covers the form.
            chooser = page.locator("text=English").first
            if await chooser.count():
                await chooser.click(timeout=2500)
            await page.wait_for_timeout(4000)
            fields = await page.evaluate(READ)
            if shot:
                shot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(shot))
            return {
                "status": response.status if response else None,
                "url": page.url,
                "fields": fields,
                "filled": [f for f in fields if f["value"]],
            }
        finally:
            await browser.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shot", type=Path, help="write a screenshot here")
    args = parser.parse_args()

    verdict = 0
    for label, url in URLS.items():
        result = await check(url, args.shot)
        if "error" in result:
            print(f"[{label}] {result['error']}")
            return 2
        print(f"[{label}] status={result['status']}")
        print(f"  filled fields: {json.dumps(result['filled'], ensure_ascii=False)}")
        if result["filled"]:
            print("  NOTE: something pre-filled; re-examine the prefill claim")
            verdict = 1
        else:
            print("  empty form: URL parameters are ignored, as recorded on 2026-10-24")
    return verdict


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
