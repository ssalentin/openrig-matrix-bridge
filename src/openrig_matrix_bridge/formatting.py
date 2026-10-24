from __future__ import annotations

import mistune

MATRIX_HTML_LIMIT = 60_000


class MatrixHTMLRenderer(mistune.HTMLRenderer):
    def image(self, text: str, url: str, title: str | None = None) -> str:
        del url, title
        label = mistune.escape(text or "Bild")
        return f"<span>[Bild nicht eingebettet: {label}]</span>"


_MARKDOWN = mistune.create_markdown(
    escape=True,
    renderer=MatrixHTMLRenderer(escape=True),
    plugins=("table", "strikethrough", "url"),
)


def markdown_to_matrix_html(text: str) -> str | None:
    rendered = _MARKDOWN(text).strip()
    if not rendered or len(rendered.encode("utf-8")) > MATRIX_HTML_LIMIT:
        return None
    return rendered
