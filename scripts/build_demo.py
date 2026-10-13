"""Build the static GitHub Pages demo into docs/ from the real web UI.

The demo reuses app/static verbatim and swaps the HTTP backend for demo/demo.js,
so the published page always looks exactly like the application.

    uv run python scripts/build_demo.py           # (re)generate docs/
    uv run python scripts/build_demo.py --check   # fail if docs/ is out of date
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
DEMO_JS = ROOT / "demo" / "demo.js"
DOCS = ROOT / "docs"
ASSETS = DOCS / "assets"
COPIED = ["style.css", "theme.js", "app.js", "marvin.js", "vendor/marked.umd.js", "vendor/purify.min.js"]
REPO_URL = "https://github.com/lugosidomotor/IT-support-chatbot"

DEMO_BANNER = (
    '<div class="banner" role="note"><b>Static demo.</b>&nbsp;No server, no LLM: every question gets a '
    f'pre-scripted reply. <a href="{REPO_URL}" target="_blank" rel="noopener">Run the real agent →</a></div>'
)


def render_index() -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/', 'href="assets/').replace('src="/static/', 'src="assets/')
    html = html.replace('<title>Developer Support</title>', '<title>Developer Support · Demo</title>')
    html = html.replace('<script src="assets/app.js"></script>',
                        '<script src="assets/demo.js"></script>\n  <script src="assets/app.js"></script>')
    html = html.replace('<div class="banners" id="banners"></div>',
                        f'<div class="banners" id="banners"></div>\n      <div class="banners">{DEMO_BANNER}</div>')
    if "assets/demo.js" not in html or "/static/" in html:
        raise SystemExit("index.html structure changed; update scripts/build_demo.py")
    return html


def expected_files() -> dict[Path, bytes]:
    files = {DOCS / "index.html": render_index().encode(), DOCS / ".nojekyll": b"",
             ASSETS / "demo.js": DEMO_JS.read_bytes()}
    for name in COPIED:
        files[ASSETS / name] = (STATIC / name).read_bytes()
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify docs/ is up to date instead of writing")
    args = parser.parse_args()
    files = expected_files()
    if args.check:
        stale = [str(p.relative_to(ROOT)) for p, data in files.items() if not p.exists() or p.read_bytes() != data]
        if stale:
            print("Demo is out of date, run: uv run python scripts/build_demo.py\n  " + "\n  ".join(stale))
            return 1
        print("docs/ demo is up to date")
        return 0
    if ASSETS.exists():
        shutil.rmtree(ASSETS)
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f"Wrote {len(files)} files to {DOCS.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
