"""Draw the README flow diagram: docs/flow/flow.<lang>.mmd -> docs/flow/flow.<lang>.png (light theme).

    .venv/bin/python docs/flow/render.py

Edit the .mmd files (Mermaid), then run this. It loads Mermaid from cdn.jsdelivr.net and renders
in the Playwright Chromium from requirements-dev.txt.
"""
import html
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(HERE.parents[1] / ".cache" / "ms-playwright"))

from playwright.sync_api import sync_playwright

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>body{{margin:0;background:#ffffff;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
pre.mermaid{{display:inline-block;margin:0;padding:20px;background:#ffffff}}</style></head>
<body><pre class="mermaid">{code}</pre>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad: false, theme: "default", flowchart: {{useMaxWidth: false}}}});
mermaid.run().then(() => document.body.dataset.done = "ok", e => document.body.dataset.done = "error: " + e.message);</script>
</body></html>"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=2)
        for source in sorted(HERE.glob("flow.*.mmd")):
            page.set_content(PAGE.format(code=html.escape(source.read_text(encoding="utf-8"))))
            page.wait_for_function("document.body.dataset.done", timeout=30000)
            done = page.evaluate("document.body.dataset.done")
            if done != "ok":
                raise SystemExit(f"{source.name}: {done}")
            out = source.with_suffix(".png")
            page.locator("pre.mermaid").screenshot(path=str(out))
            print(out.relative_to(HERE.parents[1]))
        browser.close()


if __name__ == "__main__":
    main()
