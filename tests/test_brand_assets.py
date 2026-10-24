import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from openrig_matrix_bridge.avatar_pool import profile_png

ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "src" / "openrig_matrix_bridge" / "assets" / "branding"
SOURCE_FILES = sorted((BRANDING / "source").glob("*.svg"))
EXPORT_FILES = sorted((BRANDING / "export").glob("*.png"))


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", data[16:24])


def test_ships_only_the_selected_relay_identity():
    assert {path.name for path in SOURCE_FILES} == {
        "relay-avatar.svg",
        "relay-secondary-dark.svg",
        "relay-secondary-light.svg",
    }


def test_vector_sources_are_text_free_and_self_contained():
    for path in SOURCE_FILES:
        root = ET.parse(path).getroot()
        assert not root.findall(".//{http://www.w3.org/2000/svg}text")
        for element in root.iter():
            assert not element.attrib.get("href", "").startswith(("http:", "https:"))


def test_ships_matrix_avatar_and_small_size_exports():
    expected = {
        "relay-secondary-dark-256.png": (256, 256),
        "relay-secondary-light-256.png": (256, 256),
        "relay-secondary-light-32.png": (32, 32),
        "relay-secondary-dark-32.png": (32, 32),
        "relay-avatar-256.png": (256, 256),
        "relay-avatar-32.png": (32, 32),
    }
    actual = {
        path.name: png_dimensions(path)
        for path in EXPORT_FILES
    }
    assert actual == expected


def test_contained_relay_avatar_is_the_runtime_profile():
    relay_avatar = BRANDING / "export" / "relay-avatar-256.png"

    assert profile_png() == relay_avatar.read_bytes()


def test_relay_previews_and_readme_use_local_assets():
    docs = ROOT / "docs" / "branding"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    brand_copy = (docs / "README.md").read_text(encoding="utf-8")

    assert png_dimensions(docs / "artifacts" / "relay-avatar-light-bg-preview.png") == (
        512,
        512,
    )
    assert png_dimensions(docs / "artifacts" / "relay-avatar-dark-bg-preview.png") == (
        512,
        512,
    )
    assert "relay-secondary-light-256.png" in readme
    assert "relay-secondary-dark-256.png" in readme
    assert 'alt="Relay"' in readme
    assert "Selected identity: Relay" in brand_copy
    assert "Gateway" not in brand_copy
    assert "Thread" not in brand_copy
