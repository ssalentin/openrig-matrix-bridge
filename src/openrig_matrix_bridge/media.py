from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MediaError(ValueError):
    pass


@dataclass(frozen=True)
class ImageType:
    suffix: str
    mime_type: str


def detect_image_type(data: bytes) -> ImageType | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ImageType(".png", "image/png")
    if data.startswith(b"\xff\xd8\xff"):
        return ImageType(".jpg", "image/jpeg")
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ImageType(".gif", "image/gif")
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ImageType(".webp", "image/webp")
    return None


def detect_image_file(path: Path) -> ImageType | None:
    with path.open("rb") as handle:
        return detect_image_type(handle.read(16))


def encrypted_image_content(
    *,
    filename: str,
    caption: str,
    mime_type: str,
    size: int,
    upload_url: str,
    encryption: dict[str, Any],
    formatted_caption: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "msgtype": "m.image",
        "body": caption or filename,
        "filename": filename,
        "info": {
            "mimetype": mime_type,
            "size": size,
        },
        "file": {
            "url": upload_url,
            **encryption,
        },
    }
    if formatted_caption:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = formatted_caption
    return content
