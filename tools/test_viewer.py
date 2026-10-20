"""Smoke-test docs/viewer.html over file:// — no server, the way a user would use it.

    python3 tools/test_viewer.py [sample.jpg]
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

VIEWER = Path(__file__).resolve().parents[1] / "docs" / "viewer.html"
SAMPLE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/demo-out/demo.jpg")


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 900, "height": 1100})
        await page.goto(VIEWER.as_uri())
        await page.set_input_files("#picker", str(SAMPLE))
        await page.wait_for_timeout(2500)

        badge = await page.inner_text(".badge")
        rows = await page.eval_on_selector_all(
            "table tr", "els => els.map(e => e.innerText.replace(/\\t+/g, ' : '))"
        )
        print("badge:", badge)
        for row in rows:
            print("  ", row)

        await page.click(".stage")
        await page.wait_for_timeout(2000)
        info = await page.evaluate(
            """() => { const v = document.querySelector('video');
                 return v ? { playing: !v.paused, t: +v.currentTime.toFixed(2),
                              w: v.videoWidth, h: v.videoHeight, dur: +(v.duration||0).toFixed(2) } : null; }"""
        )
        print("after click:", info)
        await page.screenshot(path="/tmp/viewer-test.png")
        await browser.close()

    ok = "Motion Photo" in badge and bool(info) and info["playing"] and info["w"] > 0
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
