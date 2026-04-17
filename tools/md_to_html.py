"""Markdown → HTML converter for research reports."""

from __future__ import annotations

import re

import markdown


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


def convert_report(md_text: str, title: str) -> str:
    """Convert markdown text to a styled HTML document."""
    protected, math_blocks = _protect_math(md_text)
    body = markdown.markdown(protected, extensions=_EXTENSIONS)
    body = _restore_math(body, math_blocks)
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
