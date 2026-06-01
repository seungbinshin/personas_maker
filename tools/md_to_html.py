"""Markdown → HTML converter for research reports."""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

import markdown

logger = logging.getLogger(__name__)


_CSS = """\
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    max-width: 900px;
    margin: 40px auto;
    padding: 0 20px;
    color: #1a1a1a;
    line-height: 1.7;
}
h1, h2, h3, h4 { color: #0d1117; margin-top: 1.5em; }
h1 { font-size: 1.8em; border-bottom: 2px solid #d0d7de; padding-bottom: 0.3em; }
h2 { font-size: 1.4em; border-bottom: 1px solid #d0d7de; padding-bottom: 0.2em; }
h3 { font-size: 1.15em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #d0d7de; padding: 8px 12px; text-align: left; }
th { background: #f6f8fa; font-weight: 600; }
tr:nth-child(even) { background: #f6f8fa; }
code { background: #f6f8fa; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
pre { background: #f6f8fa; padding: 16px; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote {
    border-left: 4px solid #d0d7de;
    margin: 1em 0;
    padding: 0.5em 1em;
    color: #57606a;
    background: #f6f8fa;
}
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
ul, ol { padding-left: 1.5em; }
hr { border: none; border-top: 1px solid #d0d7de; margin: 2em 0; }
"""

_EXTENSIONS = ["tables", "fenced_code", "toc", "nl2br", "sane_lists"]

_KATEX = """\
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body, {
    delimiters: [
      {left: '$$', right: '$$', display: true},
      {left: '$', right: '$', display: false}
    ],
    throwOnError: false
  });"></script>
"""


def _protect_math(md_text: str) -> tuple[str, list[str]]:
    """Extract LaTeX math blocks before markdown processing to prevent mangling.

    Returns (text_with_placeholders, list_of_extracted_blocks).
    """
    blocks: list[str] = []

    def _replace(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f"\x00MATH{len(blocks) - 1}\x00"

    # Display math first ($$...$$), then inline ($...$)
    text = re.sub(r"\$\$.+?\$\$", _replace, md_text, flags=re.DOTALL)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", _replace, text)
    return text, blocks


def _restore_math(html: str, blocks: list[str]) -> str:
    """Restore extracted math blocks into the HTML output."""
    for i, block in enumerate(blocks):
        html = html.replace(f"\x00MATH{i}\x00", block)
    return html


_IMG_TAG = re.compile(r'<img\s+([^>]*?)src="([^"]+)"([^>]*)>', re.IGNORECASE)
_SVG_HEADER = re.compile(r"<\?xml[^>]*\?>\s*", re.IGNORECASE)
_SVG_DOCTYPE = re.compile(r"<!DOCTYPE[^>]*>\s*", re.IGNORECASE)


def _inline_images(html: str, base_dir: Path) -> str:
    """Replace <img src="figures/foo.svg"> with inline SVG / base64-encoded raster.

    Skips absolute URLs (http/https/data:). Logs and leaves the tag untouched if the
    file is missing — broken image is better than a hard failure.
    """

    def _resolve(rel_path: str) -> Path | None:
        if rel_path.startswith(("http://", "https://", "data:", "/")):
            return None
        candidate = (base_dir / rel_path).resolve()
        try:
            candidate.relative_to(base_dir.resolve())
        except ValueError:
            logger.warning("Image %s escapes base_dir; skipping inline", rel_path)
            return None
        return candidate if candidate.is_file() else None

    def _replace(m: re.Match) -> str:
        before, src, after = m.group(1), m.group(2), m.group(3)
        path = _resolve(src)
        if path is None:
            if not src.startswith(("http://", "https://", "data:")):
                logger.warning("Inline-image source not found: %s (base=%s)", src, base_dir)
            return m.group(0)

        suffix = path.suffix.lower()
        if suffix == ".svg":
            try:
                svg = path.read_text(encoding="utf-8")
            except OSError:
                return m.group(0)
            svg = _SVG_HEADER.sub("", svg).strip()
            svg = _SVG_DOCTYPE.sub("", svg).strip()
            # Strip the <img> tag entirely; embed SVG inline so it renders standalone.
            return f'<figure class="report-figure">{svg}</figure>'
        if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }[suffix]
            try:
                data = path.read_bytes()
            except OSError:
                return m.group(0)
            b64 = base64.b64encode(data).decode("ascii")
            return f'<img {before}src="data:{mime};base64,{b64}"{after}>'
        return m.group(0)

    return _IMG_TAG.sub(_replace, html)


def convert_report(md_text: str, title: str, base_dir: Path | None = None) -> str:
    """Convert markdown text to a styled HTML document.

    If `base_dir` is given, relative `<img src="...">` references are resolved against
    it and inlined (SVG embedded as <svg>, raster encoded as data: URLs) so the
    resulting HTML is self-contained for Slack/email distribution.
    """
    protected, math_blocks = _protect_math(md_text)
    body = markdown.markdown(protected, extensions=_EXTENSIONS)
    body = _restore_math(body, math_blocks)
    if base_dir is not None:
        body = _inline_images(body, base_dir)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko">\n<head>\n'
        '<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        f"<style>{_CSS}</style>\n"
        f"{_KATEX}"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>"
    )
