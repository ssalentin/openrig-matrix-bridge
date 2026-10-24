import struct
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "src" / "openrig_matrix_bridge" / "assets" / "branding"
SOURCE_FILES = sorted((BRANDING / "source").glob("*.svg"))
EXPORT_FILES = sorted((BRANDING / "export").glob("*.png"))


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", data[16:24])


def test_ships_all_light_and_dark_vector_concepts():
    assert [path.name for path in SOURCE_FILES] == [
        "gateway-primary-dark.svg",
        "gateway-primary-light.svg",
        "relay-secondary-dark.svg",
        "relay-secondary-light.svg",
        "thread-tertiary-dark.svg",
        "thread-tertiary-light.svg",
    ]


def test_vector_sources_are_text_free_and_self_contained():
    for path in SOURCE_FILES:
        root = ET.parse(path).getroot()
        assert not root.findall(".//{http://www.w3.org/2000/svg}text")
        for element in root.iter():
            assert not element.attrib.get("href", "").startswith(("http:", "https:"))


def test_ships_matrix_avatar_and_small_size_exports():
    expected = {
        "gateway-primary-dark-256.png": (256, 256),
        "gateway-primary-light-256.png": (256, 256),
        "relay-secondary-dark-256.png": (256, 256),
        "relay-secondary-light-256.png": (256, 256),
        "thread-tertiary-dark-256.png": (256, 256),
        "thread-tertiary-light-256.png": (256, 256),
        "gateway-primary-light-32.png": (32, 32),
        "gateway-primary-dark-32.png": (32, 32),
        "relay-secondary-light-32.png": (32, 32),
        "relay-secondary-dark-32.png": (32, 32),
        "thread-tertiary-light-32.png": (32, 32),
        "thread-tertiary-dark-32.png": (32, 32),
    }
    actual = {
        path.name: png_dimensions(path)
        for path in EXPORT_FILES
        if path.name != "concepts-preview.png"
    }
    assert actual == expected


def test_ships_a_durable_visual_preview():
    preview = BRANDING / "export" / "concepts-preview.png"
    assert png_dimensions(preview) == (1020, 680)
