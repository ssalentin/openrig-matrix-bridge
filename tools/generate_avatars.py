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

STYLES = ["hood", "soft", "tall", "block"]


def background(color: str, secondary: str) -> str:
    return f"""
  <defs>
    <radialGradient id="bg" cx="26%" cy="18%" r="96%">
      <stop offset="0" stop-color="#273752"/>
      <stop offset="0.58" stop-color="#151d2d"/>
      <stop offset="1" stop-color="#080c14"/>
    </radialGradient>
    <linearGradient id="body" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{secondary}"/>
      <stop offset="1" stop-color="{color}"/>
    </linearGradient>
  </defs>
  <rect x="12" y="12" width="488" height="488" rx="124" fill="url(#bg)"/>
  <circle cx="256" cy="272" r="190" fill="{color}" opacity="0.07"/>
  <circle cx="92" cy="96" r="9" fill="{secondary}" opacity="0.5"/>
  <circle cx="420" cy="112" r="7" fill="{color}" opacity="0.4"/>
  <circle cx="88" cy="414" r="6" fill="{color}" opacity="0.32"/>"""


def body(style: str) -> str:
    if style == "hood":
        return (
            'M116 420V252c0-96 58-156 140-156s140 60 140 156v168Z'
        )
    if style == "soft":
        return (
            "M102 420V264C102 158 168 100 252 100c76 0 136 46 150 126 "
            "5 29-3 58 1 91 3 25-13 49-39 55-19 4-37-3-55-3-23 0-40 13-63 13-28 0-50-14-76-14-27 0-47 12-68 12Z"
        )
    if style == "tall":
        return (
            "M128 424V218c0-76 54-132 128-132s128 56 128 132v206Z"
        )
    return (
        "M104 424V248c0-94 64-154 152-154s152 60 152 154v176Z"
    )


def face(style: str) -> str:
    if style == "hood":
        return """
    <rect x="190" y="236" width="34" height="58" rx="17" fill="#111827"/>
    <rect x="288" y="236" width="34" height="58" rx="17" fill="#111827"/>
    <path d="M218 338c23 20 53 20 76 0" fill="none" stroke="#111827"
          stroke-width="17" stroke-linecap="round"/>"""
    if style == "soft":
        return """
    <ellipse cx="207" cy="266" rx="31" ry="39" fill="#111827"/>
    <ellipse cx="305" cy="266" rx="31" ry="39" fill="#111827"/>
    <circle cx="216" cy="253" r="9" fill="#ffffff" opacity="0.9"/>
    <circle cx="314" cy="253" r="9" fill="#ffffff" opacity="0.9"/>
    <circle cx="166" cy="326" r="21" fill="#ffffff" opacity="0.15"/>
    <path d="M220 350c22 18 50 18 72 0" fill="none" stroke="#111827"
          stroke-width="16" stroke-linecap="round"/>"""
    if style == "tall":
        return """
    <path d="M177 270c17-20 43-20 60 0" fill="none" stroke="#111827"
          stroke-width="18" stroke-linecap="round"/>
    <path d="M275 270c17-20 43-20 60 0" fill="none" stroke="#111827"
          stroke-width="18" stroke-linecap="round"/>
    <path d="M218 343c24 24 52 24 76 0" fill="none" stroke="#111827"
          stroke-width="17" stroke-linecap="round"/>"""
    return """
    <rect x="174" y="248" width="66" height="26" rx="13" fill="#111827"/>
    <rect x="272" y="248" width="66" height="26" rx="13" fill="#111827"/>
    <circle cx="213" cy="261" r="8" fill="#ffffff" opacity="0.78"/>
    <circle cx="279" cy="261" r="8" fill="#ffffff" opacity="0.78"/>
    <path d="M222 340h68" fill="none" stroke="#111827"
          stroke-width="17" stroke-linecap="round"/>"""


def render_avatar(style: str, color: str, secondary: str) -> str:
    return f"""
  <path d="{body(style)}" fill="url(#body)"/>
  <ellipse cx="210" cy="176" rx="72" ry="34" fill="#ffffff" opacity="0.12"
           transform="rotate(-14 210 176)"/>
  {face(style).strip()}
  <circle cx="256" cy="392" r="8" fill="#ffffff" opacity="0.46"/>"""


def write_avatar(avatar_id: str, svg: str) -> None:
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
                + render_avatar(style, color, secondary)
                + "\n</svg>\n"
            )
            write_avatar(avatar_id, svg)
            manifest.append(
                {
                    "id": avatar_id,
                    "style": style,
                    "color": name,
                }
            )
            index += 1

    (PNG_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
