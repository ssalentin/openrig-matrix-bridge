"""Generate the OpenRig ghost avatar pool with fal.ai.

This is a maintainer tool, not a runtime dependency. Set ``FAL_KEY`` before
running it. Raw 1024 px masters are kept outside the packaged assets so the
wheel only carries the resized 512 px avatars.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fal_client

DEFAULT_OUTPUT = Path("/tmp/openrig-avatar-fal-masters")
ENDPOINT = "openai/gpt-image-2.5/sunburst/text-to-image"
MODEL = "GPT Image 2.5 Sunburst"
QUALITY = "medium"

BODY_SHAPES = [
    ("circle", "a clean circular body with one softly rippled lower edge"),
    ("squircle", "a rounded-square body with one subtly notched lower corner"),
    ("pill", "a vertical pill-shaped body with a straight lower edge"),
    ("oblong", "a horizontal oblong body with a shallow central dip"),
    ("triangle", "an upright triangular body with softly rounded corners"),
    ("diamond", "a tall diamond-shaped body with softly rounded corners"),
    ("hexagon", "a clean hexagonal body tilted a few degrees"),
    ("octagon", "a compact octagonal body with one widened lower edge"),
    ("cloud", "a symmetrical cloud body with three large upper bumps"),
    ("star", "a rounded five-point star body"),
    (
        "crescent",
        "a crescent-moon body with the opening facing right and one rounded tip",
    ),
    (
        "heart",
        "a soft geometric heart body with the point forming the ghost tail",
    ),
    ("leaf", "an asymmetric leaf-shaped body with one curled tip"),
    (
        "teardrop",
        "an inverted teardrop body with a narrow curling point below",
    ),
    ("arch", "a tall arch-shaped body with two straight feet"),
    ("trapezoid", "a broad trapezoid body with a rippled wide base"),
    (
        "lens",
        "a horizontal lens-shaped body with two tapered pointed ends",
    ),
    (
        "semicircle",
        "a perfect half-circle body with its flat edge resting horizontally",
    ),
    (
        "comet",
        "a round head with one thick crescent-shaped trail sweeping to the side",
    ),
    (
        "flower",
        "a body with eight large rounded petals around a simple face",
    ),
    (
        "mushroom",
        "a wide mushroom-shaped body with a short rounded stem",
    ),
    ("hourglass", "an hourglass body with a narrow waist and matching lobes"),
    ("shield", "a heraldic shield body with one pointed lower tip"),
    (
        "pennant",
        "a triangular pennant body with a gently waving lower edge",
    ),
    ("bean", "a kidney-bean body bent toward the right"),
    ("snowfigure", "two unequal spheres stacked vertically"),
    (
        "gem",
        "a faceted gem-shaped body with four broad sides and a blunt base",
    ),
    (
        "crown",
        "a broad crown-shaped body with three large rounded points on top",
    ),
    (
        "notebook",
        "a portrait rectangle with a rounded spine and one small corner tab",
    ),
    (
        "lightning",
        "a single softly rounded lightning-bolt body tilted to one side",
    ),
    (
        "bubbles",
        "three unequal overlapping circular lobes forming one connected body",
    ),
    (
        "ribbon",
        "an S-shaped ribbon body with two rounded ends and a gentle twist",
    ),
]

FACE_STYLES = [
    "two tiny round eyes and a short upward mouth",
    "two narrow vertical eyes and a small straight mouth",
    "two dot eyes on a broad oval face patch and a minimal dot mouth",
    "two calm oval eyes on a tall face patch and a shallow U-shaped mouth",
]

PALETTES = [
    ("coral", "one flat saturated coral red across the whole ghost"),
    ("saffron", "one flat saturated saffron yellow across the whole ghost"),
    ("lime", "one flat saturated lime green across the whole ghost"),
    ("jade", "one flat saturated jade green across the whole ghost"),
    ("turquoise", "one flat saturated turquoise across the whole ghost"),
    ("azure", "one flat saturated azure blue across the whole ghost"),
    ("periwinkle", "one flat saturated periwinkle blue across the whole ghost"),
    ("violet", "one flat saturated violet across the whole ghost"),
]

SHAPES = [
    (name, f"{body_detail}; face with {FACE_STYLES[index % len(FACE_STYLES)]}")
    for index, (name, body_detail) in enumerate(BODY_SHAPES)
]


def prompt_for(shape: str, shape_detail: str, palette: str) -> str:
    return (
        "Use case: stylized-concept\n"
        "Asset type: 1:1 profile avatar for a professional OpenRig AI agent\n"
        "Primary request: design one original minimalist ghost whose body has "
        f"the unmistakable silhouette of a {shape}.\n"
        f"Subject: {shape_detail}.\n"
        "Shape hierarchy: the silhouette must be recognizable before the face "
        "is noticed. Treat it like a coherent interface avatar system with "
        "controlled, strongly varied geometric archetypes.\n"
        "Style/medium: contemporary flat vector character illustration; crisp "
        "edges, broad simple color fields, subtle darker edge tone and a calm "
        "Muse-inspired product character language. Keep it original; do not "
        "copy Muse, Grok Bot or any other branded character.\n"
        "Face: visible and facing the viewer; small simple eyes and minimal "
        "mouth. The expression is calm, intelligent and quietly friendly, "
        "never surprised, grinning or baby-like.\n"
        "Composition/framing: one centered figure, complete silhouette inside "
        "a generous circular-safe area, readable at 32 pixels.\n"
        f"Color palette: {palette}, with off-white or pale cream for the face "
        "markings and near-black for the pupils and mouth.\n"
        "Scene/backdrop: completely flat very dark blue-slate field filling the "
        "entire square canvas edge-to-edge; no scenery, glow, circle or tile.\n"
        "Constraints: exactly one character; no text, letters, numbers, logos, "
        "watermark, border or props; no cropped shape; no limbs or accessories "
        "beyond those explicitly required by the silhouette.\n"
        "Avoid: cute mascot, child, baby proportions, oversized anime eyes, "
        "glossy eyes, blush, smiley emoji, robot, animal costume, skull, "
        "Halloween horror, clay, 3D, plastic toy, gradients, shadows, texture, "
        "busy details, photorealism."
    )


def generate(job: dict[str, str], output_dir: Path) -> dict[str, str]:
    handle = fal_client.submit(
        ENDPOINT,
        arguments={
            "prompt": job["prompt"],
            "image_size": "square_hd",
            "background": "opaque",
            "quality": QUALITY,
            "num_images": 1,
            "output_format": "png",
        },
    )
    result = handle.get()
    image = result["images"][0]
    output_path = output_dir / f"{job['id']}.png"
    urllib.request.urlretrieve(image["url"], output_path)
    return {
        **job,
        "model": MODEL,
        "endpoint": ENDPOINT,
        "quality": QUALITY,
        "request_id": str(getattr(handle, "request_id", "")),
        "source_url": image["url"],
        "source_path": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("FAL_KEY"):
        raise SystemExit("FAL_KEY is not set")

    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    jobs = []
    for index, (shape, detail) in enumerate(SHAPES):
        palette, palette_detail = PALETTES[index % len(PALETTES)]
        jobs.append(
            {
                "id": f"openrig-{index + 1:02d}",
                "shape": shape,
                "palette": palette,
                "prompt": prompt_for(shape, detail, palette_detail),
            }
        )

    pending_jobs = [
        job
        for job in jobs
        if not (args.resume and (args.output / f"{job['id']}.png").is_file())
    ]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(generate, job, args.output): job for job in pending_jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            record = future.result()
            records.append(record)
            print(f"{job['id']} ready: {record['source_path']}", flush=True)

    generated_by_id = {record["id"]: record for record in records}
    for job in jobs:
        if job["id"] in generated_by_id:
            continue
        path = args.output / f"{job['id']}.png"
        if path.is_file():
            records.append(
                {
                    **job,
                    "model": MODEL,
                    "endpoint": ENDPOINT,
                    "quality": QUALITY,
                    "source_path": str(path),
                }
            )

    records.sort(key=lambda record: record["id"])
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "model": MODEL,
                "endpoint": ENDPOINT,
                "quality": QUALITY,
                "avatars": records,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"manifest: {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
