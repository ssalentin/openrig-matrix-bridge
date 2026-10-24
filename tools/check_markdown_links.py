#!/usr/bin/env python3
"""Check relative links in the project's published Markdown files."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

INLINE_LINK = re.compile(r"(?<!!)\[[^]]*\]\(([^)]+)\)")


def markdown_files(root: Path) -> list[Path]:
    return [root / "README.md", *sorted((root / "docs").rglob("*.md"))]


def find_missing_links(root: Path) -> list[str]:
    missing: list[str] = []
    for path in markdown_files(root):
        markdown = path.read_text(encoding="utf-8")
        for match in INLINE_LINK.finditer(markdown):
            target = match.group(1).strip().strip("<>")
            parts = urlsplit(target)
            if parts.scheme or target.startswith("#"):
                continue
            relative = unquote(parts.path)
            if not relative:
                continue
            if not (path.parent / relative).resolve().exists():
                missing.append(f"{path.relative_to(root)}: {target}")
    return sorted(missing)


def main() -> int:
    root = Path.cwd()
    files = markdown_files(root)
    missing = find_missing_links(root)
    print(f"checked={len(files)} missing={len(missing)}")
    for item in missing:
        print(f"MISSING {item}")
    return int(bool(missing))


if __name__ == "__main__":
    sys.exit(main())
