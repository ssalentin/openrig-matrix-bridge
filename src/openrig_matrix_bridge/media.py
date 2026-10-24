from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MediaError(ValueError):
    pass


GENERIC_MIME_TYPE = "application/octet-stream"
VOICE_MESSAGE_MARKERS = (
    "org.matrix.msc2516.voice",
    "org.matrix.msc3245.voice",
)
_MIME_TYPE = re.compile(
    r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+/[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
)


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


def normalize_mime_type(value: object) -> str:
    if not isinstance(value, str):
        return GENERIC_MIME_TYPE
    candidate = value.split(";", 1)[0].strip().lower()
    if len(candidate) > 255 or _MIME_TYPE.fullmatch(candidate) is None:
        return GENERIC_MIME_TYPE
    return candidate


def is_voice_message_content(content: object) -> bool:
    if not isinstance(content, dict):
        return False
    return any(marker in content for marker in VOICE_MESSAGE_MARKERS)


def detect_image_file(path: Path) -> ImageType | None:
    with path.open("rb") as handle:
        return detect_image_type(handle.read(16))


def _encrypted_media_content(
    *,
    msgtype: str,
    filename: str,
    caption: str,
    mime_type: str,
    size: int,
    upload_url: str,
    encryption: dict[str, Any],
    formatted_caption: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "msgtype": msgtype,
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
    return _encrypted_media_content(
        msgtype="m.image",
        filename=filename,
        caption=caption,
        mime_type=mime_type,
        size=size,
        upload_url=upload_url,
        encryption=encryption,
        formatted_caption=formatted_caption,
    )


def encrypted_file_content(
    *,
    filename: str,
    caption: str,
    mime_type: str,
    size: int,
    upload_url: str,
    encryption: dict[str, Any],
    formatted_caption: str | None = None,
) -> dict[str, Any]:
    return _encrypted_media_content(
        msgtype="m.file",
        filename=filename,
        caption=caption,
        mime_type=mime_type,
        size=size,
        upload_url=upload_url,
        encryption=encryption,
        formatted_caption=formatted_caption,
    )
