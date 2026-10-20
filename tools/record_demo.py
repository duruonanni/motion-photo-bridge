"""Record the Motion Photo demo page and turn it into a README GIF.

Serves nothing itself: expects the page at http://127.0.0.1:8898/index.html.
"""
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

REC = Path("/home/duruo/.hermes/cache/scratch/mpb-rec")
OUT = Path("/home/duruo/hermes-workspace/projects/motion-photo-bridge/docs/demo.gif")


async def record() -> Path:
    from playwright.async_api import async_playwright

    shutil.rmtree(REC, ignore_errors=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": 520, "height": 620},
            record_video_dir=str(REC),
            record_video_size={"width": 520, "height": 620},
        )
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:8898/index.html")
        await page.wait_for_function("window.__ready === true", timeout=30000)
        await page.wait_for_timeout(1200)
        await page.click("#stage")
        await page.wait_for_timeout(2800)
        await ctx.close()
        await browser.close()
    return next(REC.glob("*.webm"))


def to_gif(webm: Path) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vf = "fps=11,scale=400:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-vf", vf, "-loop", "0", str(OUT)],
        check=True,
    )


if __name__ == "__main__":
    src = asyncio.run(record())
    to_gif(src)
    print(f"{OUT}  {OUT.stat().st_size / 1e6:.2f} MB")
    sys.exit(0)
