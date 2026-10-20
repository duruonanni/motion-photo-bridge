import asyncio
import sys

from playwright.async_api import async_playwright


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 1000})
        await page.goto("http://127.0.0.1:8899/index.html")
        await page.wait_for_timeout(5000)

        cards = await page.eval_on_selector_all(
            ".card",
            """els => els.map(e => ({
                 name: e.querySelector('.meta b').textContent,
                 badge: e.querySelector('.badge').textContent,
                 state: e.querySelector('.state').textContent }))""",
        )
        for c in cards:
            print(f"{c['name']:<28} {c['badge']:<12} {c['state']}")

        await page.click(".card:nth-child(1) .stage")
        await page.wait_for_timeout(2500)
        info = await page.evaluate(
            """() => { const v = document.querySelector('video');
                 return v ? { currentTime: +v.currentTime.toFixed(2), paused: v.paused,
                              readyState: v.readyState, duration: +(v.duration||0).toFixed(2),
                              w: v.videoWidth, h: v.videoHeight } : null; }"""
        )
        print("\n点击第 1 张后:", info)
        await page.screenshot(path="/tmp/vivo-checker.png", full_page=False)
        await browser.close()
    return 0


sys.exit(asyncio.run(main()))
