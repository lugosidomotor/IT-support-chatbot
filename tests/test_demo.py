"""The static GitHub Pages demo must stay in sync with the real UI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_demo_is_up_to_date():
    result = subprocess.run([sys.executable, "scripts/build_demo.py", "--check"], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_demo_uses_scripted_backend_and_relative_assets():
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert html.index("assets/demo.js") < html.index("assets/app.js")
    assert "/static/" not in html and "/api/" not in html
    demo = (ROOT / "demo" / "demo.js").read_text(encoding="utf-8")
    assert "window.DSA_BACKEND" in demo and demo.count('",\n') >= 5
