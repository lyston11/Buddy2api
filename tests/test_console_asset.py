"""Guard the inline console script against syntax errors.

Regression (2026-09-16): an edit to web/index.html left an unclosed array
literal inside a Vue setup function. The inline script failed to parse, Vue
never mounted and the console rendered a completely blank page — while the
HTTP response was still 200 with the expected byte count. Nothing that inspects
the response can catch this; the script has to be parsed.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

HTML = __import__("pathlib").Path(__file__).resolve().parents[1] / "web" / "index.html"

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _inline_scripts() -> list[str]:
    return _INLINE_SCRIPT.findall(HTML.read_text(encoding="utf-8"))


def test_console_inline_scripts_parse(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; cannot parse the console script")
    scripts = _inline_scripts()
    assert scripts, f"no inline <script> found in {HTML}"
    for index, code in enumerate(scripts):
        path = tmp_path / f"inline_{index}.js"
        path.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True
        )
        assert proc.returncode == 0, (
            f"{HTML.name}: inline script #{index} has a syntax error "
            f"(the console would render blank):\n{proc.stderr}"
        )
