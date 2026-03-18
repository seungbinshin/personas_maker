"""JSON digest → HTML newspaper generator with archive navigation. No LLM needed."""

from __future__ import annotations

import html
import json
from pathlib import Path


_CSS = """\
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: "Georgia", "Noto Serif KR", serif;
    max-width: 780px; margin: 0 auto; padding: 24px 20px;
    color: #1a1a1a; background: #fafaf8;
}
.masthead {
    text-align: center; border-bottom: 3px double #1a1a1a;
    padding-bottom: 12px; margin-bottom: 20px;
}
.masthead h1 { font-size: 2em; letter-spacing: 2px; }
.masthead .date { font-size: 0.9em; color: #666; margin-top: 4px; }
.nav {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 20px; padding: 8px 0;
    border-bottom: 1px solid #e0e0e0; font-size: 0.85em;
}
.nav a {
    color: #0d47a1; text-decoration: none; padding: 4px 10px;
    border: 1px solid #ccc; border-radius: 4px;
}
.nav a:hover { background: #e3f2fd; }
.nav .disabled { color: #ccc; border-color: #eee; pointer-events: none; }
.nav .archive-link { font-weight: 600; }
.section { margin-bottom: 24px; }
.section-title {
    font-size: 1.1em; font-weight: 700; text-transform: uppercase;
    letter-spacing: 1px; border-bottom: 1px solid #1a1a1a;
    padding-bottom: 4px; margin-bottom: 12px; color: #333;
}
.article { margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px dotted #ccc; }
.article:last-child { border-bottom: none; }
.article-title { font-size: 1.05em; font-weight: 700; line-height: 1.4; }
.article-title a { color: #0d47a1; text-decoration: none; }
.article-title a:hover { text-decoration: underline; }
.article-summary { font-size: 0.92em; line-height: 1.6; color: #333; margin-top: 4px; }
.article-meta { font-size: 0.78em; color: #888; margin-top: 4px; }
.article-date { color: #666; }
.credibility-tag {
    display: inline-block; font-size: 0.7em; padding: 1px 5px;
    border-radius: 3px; margin-left: 4px; vertical-align: middle;
}
.cred-official { background: #e8f5e9; color: #2e7d32; }
.cred-major_media { background: #e3f2fd; color: #1565c0; }
.cred-blog { background: #fff3e0; color: #e65100; }
.rumors-box {
    background: #fff8e1; border: 1px solid #ffe082; border-radius: 6px;
    padding: 14px 16px; margin-top: 20px;
}
.rumors-box h3 {
    font-size: 0.95em; color: #f57f17; margin-bottom: 8px;
    letter-spacing: 1px;
}
.rumor-item { font-size: 0.88em; line-height: 1.5; margin-bottom: 8px; color: #555; }
.rumor-item a { color: #bf360c; }
.footer {
    text-align: center; font-size: 0.75em; color: #aaa;
    margin-top: 24px; padding-top: 12px; border-top: 1px solid #ddd;
}
/* Index page */
.index-list { list-style: none; padding: 0; }
.index-list li {
    padding: 10px 0; border-bottom: 1px dotted #ccc;
}
.index-list li a { color: #0d47a1; text-decoration: none; font-weight: 600; }
.index-list li a:hover { text-decoration: underline; }
.index-list .meta { font-size: 0.85em; color: #888; margin-top: 2px; }
"""

_CATEGORY_LABELS = {
    "technology": "Technology",
    "economy": "Economy",
}

_CRED_LABELS = {
    "official": "공식",
    "major_media": "주요매체",
    "blog": "블로그",
}


def generate_newspaper(
    digest: dict,
    prev_filename: str | None = None,
    next_filename: str | None = None,
) -> str:
    """Convert a structured digest JSON into a styled HTML newspaper page."""
    date = html.escape(digest.get("date", ""))

    # Navigation
    prev_link = (
        f'<a href="{prev_filename}">&larr; Previous</a>'
        if prev_filename else '<span class="disabled">&larr; Previous</span>'
    )
    next_link = (
        f'<a href="{next_filename}">Next &rarr;</a>'
        if next_filename else '<span class="disabled">Next &rarr;</span>'
    )
    nav_html = (
        f'<div class="nav">'
        f'{prev_link}'
        f'<a class="archive-link" href="index.html">Past Issues</a>'
        f'{next_link}'
        f'</div>'
    )

    # Sections
    sections_html = []
    for section in digest.get("sections", []):
        cat = section.get("category", "")
        label = _CATEGORY_LABELS.get(cat, cat.title())
        articles_html = []

        for art in section.get("articles", []):
            title = html.escape(art.get("title", ""))
            url = art.get("source_url", "")
            summary = html.escape(art.get("summary", ""))
            source = html.escape(art.get("source", ""))
            pub_date = html.escape(art.get("published_date", ""))
            cred = art.get("credibility", "blog")
            cred_cls = f"cred-{cred}" if cred in _CRED_LABELS else "cred-blog"
            cred_label = _CRED_LABELS.get(cred, cred)

            title_link = (
                f'<a href="{html.escape(url)}" target="_blank">{title}</a>'
                if url else title
            )
            date_span = f'<span class="article-date">{pub_date}</span> · ' if pub_date else ""
            articles_html.append(
                f'<div class="article">'
                f'<div class="article-title">{title_link}</div>'
                f'<div class="article-summary">{summary}</div>'
                f'<div class="article-meta">{date_span}{source}'
                f'<span class="credibility-tag {cred_cls}">{cred_label}</span></div>'
                f'</div>'
            )

        if articles_html:
            sections_html.append(
                f'<div class="section">'
                f'<div class="section-title">{html.escape(label)}</div>'
                f'{"".join(articles_html)}'
                f'</div>'
            )

    # Rumors
    rumors_html = ""
    rumors = digest.get("rumors", [])
    if rumors:
        items = []
        for r in rumors:
            snippet = html.escape(r.get("snippet", ""))
            src = html.escape(r.get("source", ""))
            url = r.get("source_url", "")
            src_link = (
                f'<a href="{html.escape(url)}" target="_blank">{src}</a>'
                if url else src
            )
            items.append(f'<div class="rumor-item">{snippet} — {src_link}</div>')
        rumors_html = (
            f'<div class="rumors-box">'
            f'<h3>Rumors &amp; Unverified</h3>'
            f'{"".join(items)}'
            f'</div>'
        )

    return (
        '<!DOCTYPE html>\n'
        '<html lang="ko">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>Daily Tech Brief — {date}</title>\n'
        f'<style>{_CSS}</style>\n'
        '</head>\n<body>\n'
        '<div class="masthead">\n'
        '<h1>DAILY TECH BRIEF</h1>\n'
        f'<div class="date">{date}</div>\n'
        '</div>\n'
        f'{nav_html}\n'
        f'{"".join(sections_html)}\n'
        f'{rumors_html}\n'
        '<div class="footer">Auto-generated by Persona Reporter</div>\n'
        '</body>\n</html>'
    )


def generate_index(digests_dir: Path) -> str:
    """Generate an index.html listing all past issues with links."""
    entries = []
    for path in sorted(digests_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            digest = data.get("digest", data)
            date_str = data.get("date", digest.get("date", path.stem))
            article_count = sum(
                len(s.get("articles", [])) for s in digest.get("sections", [])
            )
            rumor_count = len(digest.get("rumors", []))
            html_file = f"briefing_{date_str.replace('-', '')}.html"
            entries.append({
                "date": date_str,
                "html_file": html_file,
                "article_count": article_count,
                "rumor_count": rumor_count,
            })
        except (json.JSONDecodeError, OSError):
            continue

    items_html = []
    for e in entries:
        items_html.append(
            f'<li>'
            f'<a href="{e["html_file"]}">{e["date"]}</a>'
            f'<div class="meta">{e["article_count"]} articles · '
            f'{e["rumor_count"]} rumors</div>'
            f'</li>'
        )

    return (
        '<!DOCTYPE html>\n'
        '<html lang="ko">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>Daily Tech Brief — Archive</title>\n'
        f'<style>{_CSS}</style>\n'
        '</head>\n<body>\n'
        '<div class="masthead">\n'
        '<h1>DAILY TECH BRIEF</h1>\n'
        '<div class="date">Archive</div>\n'
        '</div>\n'
        f'<ul class="index-list">{"".join(items_html)}</ul>\n'
        '<div class="footer">Auto-generated by Persona Reporter</div>\n'
        '</body>\n</html>'
    )
