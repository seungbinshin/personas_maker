"""Markdown → HTML converter for research reports."""

from __future__ import annotations

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


def convert_report(md_text: str, title: str) -> str:
    """Convert markdown text to a styled HTML document."""
    body = markdown.markdown(md_text, extensions=_EXTENSIONS)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ko">\n<head>\n'
        '<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>"
    )
