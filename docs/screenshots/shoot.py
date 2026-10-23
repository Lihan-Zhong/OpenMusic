"""The README's screenshots: docs/screenshots/<page>.<lang>.png for the workbench and the practice player.

    .venv/bin/python docs/screenshots/shoot.py

Needs the songs below processed in outputs/demucs6_full (split, analysed, transcribed, player built). It starts a
workbench of its own on PORT, shows only SONGS in its list, opens PLAYER_SONG in the practice player with an A-B
loop and the drum lane, and takes the pictures with the Playwright Chromium from requirements-dev.txt.
"""
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".cache" / "ms-playwright"))

from playwright.sync_api import sync_playwright

PORT = 8792
SONGS = ("Crayon Case - Surabaya", "想想XiangXiang - 第二张专辑的intro", "Chinese Football - 四月物语")
PLAYER_SONG, ROLL = "Crayon Case - Surabaya", "drums"
LOOP, AT = (17.6, 22.9), 19.6          # seconds: the loop is snapped to bars; the playhead inside it
VIEWPORT, SCALE = {"width": 1280, "height": 900}, 1.5


def clip(page, top, bottom, pad=16):
    """The full-width strip of the page from the top of element top to the bottom of element bottom."""
    a, b = page.locator(top).first.bounding_box(), page.locator(bottom).first.bounding_box()
    y0, y1 = max(a["y"] - pad, 0), b["y"] + b["height"] + pad
    return {"x": 0, "y": y0, "width": page.evaluate("document.documentElement.scrollWidth"), "height": y1 - y0}


def workbench(page, lang):
    page.goto(f"http://127.0.0.1:{PORT}/?lang={lang}")
    page.wait_for_function("/(Apple GPU|NVIDIA GPU|CPU)/.test(document.body.innerText)", timeout=120000)
    page.evaluate("""songs => { for (const tr of document.querySelectorAll('tbody tr'))
                                  if (!songs.some(s => tr.textContent.includes(s))) tr.remove(); }""", list(SONGS))
    page.screenshot(path=str(HERE / f"workbench.{lang}.png"), full_page=True, clip=clip(page, "header", "tbody tr >> nth=-1"))


def player(page, lang):
    name = "player.html" if lang == "zh" else "player.en.html"
    page.goto(f"http://127.0.0.1:{PORT}/outputs/demucs6_full/{name}?song=" + urllib.parse.quote(PLAYER_SONG))
    page.wait_for_function("document.documentElement.dataset.ready === '1'", timeout=120000)
    box = page.locator("#wave").bounding_box()
    duration = page.evaluate("() => window.player.tr.duration")
    x = lambda t: box["x"] + t / duration * box["width"]   # noqa: E731
    y = box["y"] + box["height"] * 0.4
    page.mouse.move(x(LOOP[0]), y)
    page.mouse.down()
    for k in range(1, 11):
        page.mouse.move(x(LOOP[0] + (LOOP[1] - LOOP[0]) * k / 10), y)
    page.mouse.up()
    page.wait_for_timeout(300)
    page.mouse.click(x(AT), y)
    page.evaluate(f"() => {{ const r = document.getElementById('roll'); r.value = '{ROLL}'; r.onchange(); }}")
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(800)
    page.screenshot(path=str(HERE / f"player.{lang}.png"), full_page=True, clip=clip(page, "header", "#roll"))
    section = page.evaluate("() => { const s = document.getElementById('stems').closest('section, .card') || "
                            "document.getElementById('stems').parentElement; s.id = s.id || 'stems-card'; return '#' + s.id; }")
    page.screenshot(path=str(HERE / f"mixer.{lang}.png"), full_page=True, clip=clip(page, section, section, pad=0))


def main():
    app = subprocess.Popen([sys.executable, str(ROOT / "experiments" / "app.py"), "--port", str(PORT), "--no-browser"],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for lang in ("zh", "en"):
                context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE)
                page = context.new_page()
                workbench(page, lang)
                player(page, lang)
                context.close()
                print(f"docs/screenshots/workbench.{lang}.png, player.{lang}.png, mixer.{lang}.png")
            browser.close()
    finally:
        app.terminate()
        app.wait(10)


if __name__ == "__main__":
    main()
