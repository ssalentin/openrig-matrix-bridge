from __future__ import annotations

import re
from importlib import resources

POOL_AVATAR = re.compile(r"^openrig-\d{2}\.png$")


def avatar_pool_ids() -> list[str]:
    asset_dir = resources.files("openrig_matrix_bridge.assets").joinpath("avatars")
    return sorted(
        item.name.removesuffix(".png")
        for item in asset_dir.iterdir()
        if item.is_file() and POOL_AVATAR.fullmatch(item.name)
    )


def avatar_png(avatar_id: str) -> bytes:
    if not re.fullmatch(r"openrig-\d{2}", avatar_id):
        raise ValueError(f"invalid avatar id: {avatar_id}")
    asset = (
        resources.files("openrig_matrix_bridge.assets")
        .joinpath("avatars")
        .joinpath(f"{avatar_id}.png")
    )
    return asset.read_bytes()


def profile_png() -> bytes:
    asset = (
        resources.files("openrig_matrix_bridge.assets")
        .joinpath("avatars")
        .joinpath("openrig-profile.png")
    )
    return asset.read_bytes()


def assign_unique_avatar_ids(
    agent_keys: list[str],
    existing: dict[str, str],
) -> dict[str, str]:
    pool = avatar_pool_ids()
    available = set(pool)
    assigned: dict[str, str] = {}
    for key in agent_keys:
        candidate = existing.get(key)
        if candidate in available:
            assigned[key] = candidate
            available.remove(candidate)
    for key in agent_keys:
        if key in assigned or not available:
            continue
        selected = min(available)
        assigned[key] = selected
        available.remove(selected)
    return assigned
