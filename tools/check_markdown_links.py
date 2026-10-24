#!/usr/bin/env python3
"""Check relative links in the project's published Markdown files."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

INLINE_LINK = re.compile(r"!?\[[^]]*\]\(([^)\n]+)\)")
ANGLE_DESTINATION = re.compile(r"<([^>]+)>")
FENCE_START = re.compile(r"^ {0,3}(`{3,}|~{3,}).*$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
INLINE_CODE = re.compile(r"`+[^`\n]*`+")


def markdown_files(root: Path) -> list[Path]:
    return [root / "README.md", *sorted((root / "docs").rglob("*.md"))]


def without_fenced_code(markdown: str) -> str:
    lines: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in markdown.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if fence_char:
            match = FENCE_CLOSE.match(stripped)
            if (
                match
                and match.group(1)[0] == fence_char
                and len(match.group(1)) >= fence_length
            ):
                fence_char = ""
                fence_length = 0
            continue
        match = FENCE_START.match(stripped)
        if match:
            fence_char = match.group(1)[0]
            fence_length = len(match.group(1))
            continue
        lines.append(line)
    return "".join(lines)


def find_missing_links(root: Path) -> list[str]:
    missing: list[str] = []
    for path in markdown_files(root):
        markdown = path.read_text(encoding="utf-8")
        markdown = without_fenced_code(markdown)
        markdown = INLINE_CODE.sub("", markdown)
        for match in INLINE_LINK.finditer(markdown):
            destination = match.group(1).strip()
            if angled := ANGLE_DESTINATION.match(destination):
                target = angled.group(1)
            else:
                target = destination.split(maxsplit=1)[0]
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
