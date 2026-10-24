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

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path("/tmp/openrig-avatar-fal-masters")
ENDPOINT = "openai/gpt-image-2.5/sunburst/text-to-image"
MODEL = "GPT Image 2.5 Sunburst"
QUALITY = "medium"

SHAPES = [
    (
        "round bell scallop",
        (
            "a round bell-shaped ghost with two short side ears, three soft "
            "scallops at the hem and a circular pale face patch"
        ),
    ),
    (
        "pinched bell",
        (
            "a bell ghost pinched at one shoulder, ending in one narrow curled "
            "tail, with a tall oval face patch"
        ),
    ),
    (
        "stretched bell",
        (
            "a vertically stretched bell ghost with a curled quiff and a broad "
            "arched face patch"
        ),
    ),
    (
        "lobed bell",
        (
            "a broad bell ghost with two round side lobes, a stepped four-point "
            "hem and a softly squared face patch"
        ),
    ),
    (
        "pear ghost",
        (
            "a pear-shaped ghost with one drooping ear, a comma-shaped tail and "
            "an off-center oval face patch"
        ),
    ),
    (
        "leaning bean",
        (
            "an asymmetric bean ghost leaning left, with a small cape tab and a "
            "round face patch tilted with the lean"
        ),
    ),
    (
        "almond crown",
        (
            "a plump almond-shaped ghost with a pointed crown, three tufted "
            "feet and a long vertical face patch"
        ),
    ),
    (
        "square split",
        (
            "a softly square ghost with rounded shoulder caps, a split skirt and "
            "a broad semicircular face patch"
        ),
    ),
    (
        "flame spiral",
        (
            "a tall flame-shaped ghost with a rippling body and a spiral-shaped "
            "tip, carrying a narrow teardrop face patch"
        ),
    ),
    (
        "candle ribbons",
        (
            "a thin candle ghost with a bulbous head, two ribbon-like lower tails "
            "and a small oval face patch"
        ),
    ),
    (
        "lightning hem",
        (
            "a narrow lightning-shaped ghost with a zigzag lower edge and a "
            "faceted diamond face patch"
        ),
    ),
    (
        "side-fin teardrop",
        (
            "a tall teardrop ghost leaning right with one curved side fin and a "
            "long pointed face patch"
        ),
    ),
    (
        "cumulus",
        (
            "a wide cumulus ghost with two top bumps, four round lower lobes and "
            "a wide oval face patch"
        ),
    ),
    (
        "fan ridge",
        (
            "a broad fan-shaped ghost with ridged sides, three triangular hem "
            "points and a flat-topped face patch"
        ),
    ),
    (
        "spread cloud",
        (
            "a cloud ghost with one long side arm, a blunt bottom and an "
            "asymmetric face patch"
        ),
    ),
    (
        "mushroom bell",
        (
            "a bulbous mushroom ghost with a wavy cap, two tiny feet and a short "
            "round face patch"
        ),
    ),
    (
        "comet drop",
        (
            "a drop-shaped comet ghost with a curved upper tip and a single "
            "spiral tail, featuring an oval face patch"
        ),
    ),
    (
        "horned drop",
        (
            "an inverted-drop ghost with two small horns, a scalloped base and a "
            "tall narrow face patch"
        ),
    ),
    (
        "long comet",
        (
            "a long comet ghost sweeping to one side with a tapered trail and a "
            "round face patch near the head"
        ),
    ),
    (
        "lantern oval",
        (
            "an oval lantern ghost with a dimpled crown, one short tail and a "
            "vertical pill-shaped face patch"
        ),
    ),
    (
        "classic sheet",
        (
            "a classic draped sheet ghost with an asymmetric hood fold, one "
            "upturned corner and a heart-shaped face patch"
        ),
    ),
    (
        "sleeved sheet",
        (
            "a sheet ghost with wide sculptural sleeves, a divided pointed hem "
            "and an oval face patch"
        ),
    ),
    (
        "pleated sheet",
        (
            "a pleated sheet ghost with a tall top knot, five crisp folds and a "
            "small round face patch"
        ),
    ),
    (
        "draped diagonal",
        (
            "a draped ghost with one shoulder peak, a broad crooked hem and an "
            "off-center diamond face patch"
        ),
    ),
    (
        "pebble stack",
        (
            "a ghost made from three stacked pebble forms, with crescent ears, "
            "floating lower tips and a wide oval face patch"
        ),
    ),
    (
        "double sphere",
        (
            "a snow-creature ghost with two unequal spheres, small side puffs "
            "and a tall oval face patch"
        ),
    ),
    (
        "totem lobes",
        (
            "a totem ghost with three slim stacked lobes, tendril tails and a "
            "narrow vertical face patch"
        ),
    ),
    (
        "stone bulge",
        (
            "a rounded stone ghost with one large side bulge, a cleft hem and a "
            "broad asymmetric face patch"
        ),
    ),
    (
        "pointed cowl",
        (
            "a hooded cape ghost with a pointed cowl, two swept cape wings and a "
            "round face patch"
        ),
    ),
    (
        "square hood",
        (
            "a hooded ghost with squared shoulders, three cloak tails and a "
            "softly rectangular face patch"
        ),
    ),
    (
        "halo ribbons",
        (
            "a round halo ghost with a circular cape edge, two side ribbons and "
            "an oval face patch"
        ),
    ),
    (
        "bishop fold",
        (
            "an asymmetric bishop-shaped ghost with a tall hood, one folded "
            "sleeve and a tapered face patch"
        ),
    ),
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


def prompt_for(shape: str, shape_detail: str, palette: str) -> str:
    return (
        "Use case: stylized-concept\n"
        "Asset type: 1:1 profile portrait for a professional OpenRig AI agent\n"
        "Primary request: One original, friendly ghost character with an "
        f"instantly distinctive {shape} silhouette.\n"
        f"Subject: {shape_detail}.\n"
        "Style/medium: contemporary flat vector character illustration; precise "
        "geometric planes, crisp negative space, clean edges, restrained details "
        "and the calm product polish of Muse's agent mascot without copying its "
        "specific design. The result must feel designed for a serious software "
        "product team, not for a children's game.\n"
        "Reference goal: the controlled variety and interface-scale simplicity "
        "of contemporary AI-agent avatar systems such as Grok Bot and Muse, "
        "while creating an original OpenRig ghost language; do not copy their "
        "exact body shapes, face, eyes or accessories.\n"
        "Face: clearly visible and facing the viewer; two small, calm, solid "
        "dark eyes and one tiny restrained mouth line. Friendly and intelligent, "
        "not surprised, not grinning and not baby-like.\n"
        "Composition/framing: single centered full ghost, silhouette complete "
        "inside a generous circular-safe area, readable at 48 pixels.\n"
        f"Color palette: one flat {palette} body color, a slightly darker edge "
        "tone for separation, and a warm off-white face patch if needed.\n"
        "Scene/backdrop: completely flat very dark blue-slate field filling the "
        "entire square canvas edge-to-edge; no scenery, circle or inner tile.\n"
        "Constraints: original ghost design; no resemblance to an existing "
        "branded mascot, television character, game character or logo; no text, "
        "letters, numbers, watermark or border; no props; no cropped edges.\n"
        "Avoid: child, baby proportions, oversized anime eyes, glossy eyes, "
        "blush, broad smile, mascot costume, robot, animal, skull, Halloween "
        "horror, clay, 3D, plastic toy, gradients, soft shadows, busy details, "
        "photorealism."
    )


def jobs_for(preview: bool) -> list[dict[str, str]]:
    jobs = []
    for shape_index, (shape, detail) in enumerate(SHAPES):
        palette, description = PALETTES[shape_index % len(PALETTES)]
        avatar_id = (
            f"preview-{shape_index + 1:02d}"
            if preview
            else f"openrig-{shape_index + 1:02d}"
        )
        jobs.append(
            {
                "id": avatar_id,
                "shape": shape,
                "palette": palette,
                "palette_detail": description,
                "prompt": prompt_for(shape, detail, description),
            }
        )
    return jobs


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
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    if not os.environ.get("FAL_KEY"):
        raise SystemExit("FAL_KEY is not set")

    args.output.mkdir(parents=True, exist_ok=True)
    jobs = jobs_for(args.preview)
    records = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(generate, job, args.output): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            record = future.result()
            records.append(record)
            print(f"{job['id']} ready: {record['source_path']}", flush=True)

    records.sort(key=lambda record: record["id"])
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "model": MODEL,
                "endpoint": ENDPOINT,
                "quality": QUALITY,
                "preview": args.preview,
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
