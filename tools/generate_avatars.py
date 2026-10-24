from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG_DIR = ROOT / "assets" / "avatars" / "svg"
PNG_DIR = ROOT / "src" / "openrig_matrix_bridge" / "assets" / "avatars"

COLORS = [
    ("coral", "#ff6b6b", "#ff9f80"),
    ("amber", "#ffb020", "#ffe066"),
    ("lime", "#a7e22e", "#d8f56a"),
    ("green", "#37d67a", "#7ee7a7"),
    ("teal", "#22c7b8", "#72eadf"),
    ("sky", "#38bdf8", "#8ddcff"),
    ("blue", "#5b7cfa", "#9eb0ff"),
    ("violet", "#9b6dff", "#c8afff"),
]

STYLES = ["round", "visor", "antenna", "wide"]


def background(color: str, secondary: str) -> str:
    return f"""
  <defs>
    <radialGradient id="bg" cx="28%" cy="20%" r="95%">
      <stop offset="0" stop-color="#25324a"/>
      <stop offset="0.58" stop-color="#151d2d"/>
      <stop offset="1" stop-color="#090d16"/>
    </radialGradient>
    <linearGradient id="body" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{secondary}"/>
      <stop offset="1" stop-color="{color}"/>
    </linearGradient>
  </defs>
  <rect x="12" y="12" width="488" height="488" rx="124" fill="url(#bg)"/>
  <circle cx="256" cy="256" r="188" fill="{color}" opacity="0.07"/>
  <circle cx="92" cy="100" r="10" fill="{secondary}" opacity="0.45"/>
  <circle cx="420" cy="118" r="7" fill="{color}" opacity="0.35"/>
  <circle cx="88" cy="410" r="6" fill="{color}" opacity="0.3"/>"""


def friendly(color: str) -> str:
    return f"""
  <g>
    <rect x="104" y="132" width="304" height="272" rx="116" fill="url(#body)"/>
    <rect x="74" y="224" width="48" height="104" rx="24" fill="{color}"/>
    <rect x="390" y="224" width="48" height="104" rx="24" fill="{color}"/>
    <ellipse cx="196" cy="243" rx="31" ry="37" fill="#111827"/>
    <ellipse cx="316" cy="243" rx="31" ry="37" fill="#111827"/>
    <circle cx="205" cy="230" r="9" fill="#ffffff" opacity="0.85"/>
    <circle cx="325" cy="230" r="9" fill="#ffffff" opacity="0.85"/>
    <path d="M216 322c24 22 56 22 80 0" fill="none" stroke="#111827" stroke-width="18" stroke-linecap="round"/>
  </g>"""


def visor(color: str, secondary: str) -> str:
    return f"""
  <g>
    <rect x="88" y="142" width="336" height="248" rx="78" fill="url(#body)"/>
    <path d="M112 196h288" stroke="#ffffff" stroke-width="10" stroke-linecap="round" opacity="0.22"/>
    <rect x="140" y="218" width="232" height="86" rx="43" fill="#101827"/>
    <circle cx="212" cy="261" r="18" fill="{secondary}"/>
    <circle cx="300" cy="261" r="18" fill="{secondary}"/>
    <path d="M204 348h104" stroke="#101827" stroke-width="16" stroke-linecap="round"/>
  </g>"""


def antenna(color: str, secondary: str) -> str:
    return f"""
  <g>
    <rect x="246" y="88" width="20" height="54" rx="10" fill="{color}"/>
    <circle cx="256" cy="78" r="26" fill="{secondary}"/>
    <circle cx="256" cy="78" r="11" fill="#ffffff" opacity="0.85"/>
    <rect x="116" y="136" width="280" height="280" rx="140" fill="url(#body)"/>
    <path d="M186 242c18-18 46-18 64 0" fill="none" stroke="#111827" stroke-width="18" stroke-linecap="round"/>
    <path d="M262 242c18-18 46-18 64 0" fill="none" stroke="#111827" stroke-width="18" stroke-linecap="round"/>
    <circle cx="184" cy="314" r="22" fill="#ffffff" opacity="0.22"/>
    <path d="M216 338c24 18 56 18 80 0" fill="none" stroke="#111827" stroke-width="16" stroke-linecap="round"/>
  </g>"""


def wide(color: str) -> str:
    return f"""
  <g>
    <path d="M86 198c0-38 31-69 69-69h202c38 0 69 31 69 69v122c0 38-31 69-69 69H155c-38 0-69-31-69-69Z" fill="url(#body)"/>
    <rect x="54" y="210" width="50" height="96" rx="22" fill="{color}"/>
    <rect x="408" y="210" width="50" height="96" rx="22" fill="{color}"/>
    <rect x="158" y="226" width="196" height="64" rx="32" fill="#111827"/>
    <circle cx="212" cy="258" r="13" fill="#ffffff"/>
    <circle cx="300" cy="258" r="13" fill="#ffffff"/>
    <circle cx="256" cy="348" r="21" fill="#111827"/>
    <circle cx="256" cy="348" r="7" fill="#ffffff" opacity="0.75"/>
  </g>"""


def render_style(style: str, color: str, secondary: str) -> str:
    if style == "round":
        return friendly(color)
    if style == "visor":
        return visor(color, secondary)
    if style == "antenna":
        return antenna(color, secondary)
    return wide(color)


def main() -> None:
    SVG_DIR.mkdir(parents=True, exist_ok=True)
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    index = 1
    for style in STYLES:
        for name, color, secondary in COLORS:
            avatar_id = f"openrig-{index:02d}"
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">\n'
                + background(color, secondary)
                + render_style(style, color, secondary)
                + "\n</svg>\n"
            )
            svg_path = SVG_DIR / f"{avatar_id}.svg"
            png_path = PNG_DIR / f"{avatar_id}.png"
            svg_path.write_text(svg, encoding="utf-8")
            subprocess.run(
                [
                    "rsvg-convert",
                    "-w",
                    "512",
                    "-h",
                    "512",
                    str(svg_path),
                    "-o",
                    str(png_path),
                ],
                check=True,
            )
            manifest.append(
                {
                    "id": avatar_id,
                    "style": style,
                    "color": name,
                }
            )
            index += 1
    profile_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">\n'
        + background("#38bdf8", "#8ddcff")
        + """
  <ellipse cx="256" cy="250" rx="202" ry="76" fill="none" stroke="#8ddcff"
           stroke-width="10" opacity="0.55" transform="rotate(-18 256 250)"/>
  <circle cx="423" cy="176" r="18" fill="#8ddcff"/>
"""
        + friendly("#38bdf8")
        + "\n</svg>\n"
    )
    svg_path = SVG_DIR / "openrig-profile.svg"
    png_path = PNG_DIR / "openrig-profile.png"
    svg_path.write_text(profile_svg, encoding="utf-8")
    subprocess.run(
        [
            "rsvg-convert",
            "-w",
            "512",
            "-h",
            "512",
            str(svg_path),
            "-o",
            str(png_path),
        ],
        check=True,
    )
    manifest.append(
        {
            "id": "openrig-profile",
            "style": "profile",
            "color": "sky",
            "profile_only": True,
        }
    )
    (PNG_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
