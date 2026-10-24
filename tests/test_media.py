from openrig_matrix_bridge.media import (
    detect_image_type,
    encrypted_image_content,
)


def test_detects_supported_image_signatures():
    assert detect_image_type(b"\x89PNG\r\n\x1a\n" + b"data").suffix == ".png"
    assert detect_image_type(b"\xff\xd8\xff" + b"data").suffix == ".jpg"
    assert detect_image_type(b"GIF89a" + b"data").suffix == ".gif"
    assert detect_image_type(b"RIFF" + b"\0" * 4 + b"WEBP").suffix == ".webp"
    assert detect_image_type(b"not an image") is None


def test_builds_encrypted_image_message():
    content = encrypted_image_content(
        filename="screenshot.png",
        caption="**Ansicht**",
        mime_type="image/png",
        size=123,
        upload_url="mxc://example.org/file",
        encryption={"key": {"k": "secret"}, "iv": "iv", "hashes": {"sha256": "hash"}},
        formatted_caption="<p><strong>Ansicht</strong></p>",
    )

    assert content["msgtype"] == "m.image"
    assert content["file"]["url"] == "mxc://example.org/file"
    assert content["info"] == {"mimetype": "image/png", "size": 123}
    assert content["format"] == "org.matrix.custom.html"
    assert content["formatted_body"] == "<p><strong>Ansicht</strong></p>"
