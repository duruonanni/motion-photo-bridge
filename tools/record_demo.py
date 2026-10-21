"""Record docs/viewer.html inspecting a real Motion Photo, and turn it into the README GIF.

Drives the page a user actually gets — no server needed, the viewer reads the file
straight off disk.

    python3 tools/record_demo.py [photo.jpg]
"""
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VIEWER = REPO / "docs" / "viewer.html"
OUT = REPO / "docs" / "demo.gif"
SAMPLE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/demo-out/demo.jpg")
REC = Path.home() / ".hermes/cache/scratch/mpb-rec"

W, H = 430, 720


async def record(sample: Path) -> Path:
    from playwright.async_api import async_playwright

    shutil.rmtree(REC, ignore_errors=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(REC),
            record_video_size={"width": W, "height": H},
        )
        page = await ctx.new_page()
        await page.goto(VIEWER.as_uri())
        await page.set_input_files("#picker", str(sample))
        await page.wait_for_timeout(1000)
        await page.click(".stage")
        await page.wait_for_timeout(2600)
        await ctx.close()
        await browser.close()
    return next(REC.glob("*.webm"))


def to_gif(webm: Path) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vf = ("fps=10,scale=380:-1:flags=lanczos,split[a][b];"
          "[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-vf", vf, "-loop", "0", str(OUT)],
        check=True,
    )


if __name__ == "__main__":
    if not SAMPLE.exists():
        raise SystemExit(f"sample not found: {SAMPLE}")
    to_gif(asyncio.run(record(SAMPLE)))
    print(f"{OUT}  {OUT.stat().st_size / 1e6:.2f} MB")
