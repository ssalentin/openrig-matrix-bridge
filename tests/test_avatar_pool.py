from pathlib import Path

from openrig_matrix_bridge.avatar_pool import (
    assign_unique_avatar_ids,
    avatar_png,
    avatar_pool_fingerprint,
    avatar_pool_ids,
    profile_png,
)

ROOT = Path(__file__).resolve().parents[1]


def test_ships_a_large_unique_avatar_pool():
    ids = avatar_pool_ids()

    assert len(ids) == 32
    assert len(set(ids)) == 32
    assert all(avatar_png(avatar_id).startswith(b"\x89PNG") for avatar_id in ids)
    assert profile_png().startswith(b"\x89PNG")
    assert len(avatar_pool_fingerprint()) == 64


def test_profile_is_the_selected_relay_avatar():
    relay_avatar = (
        ROOT
        / "src"
        / "openrig_matrix_bridge"
        / "assets"
        / "branding"
        / "export"
        / "relay-avatar-256.png"
    )

    assert profile_png() == relay_avatar.read_bytes()


def test_preserves_valid_assignments_and_never_duplicates():
    ids = avatar_pool_ids()
    assigned = assign_unique_avatar_ids(
        ["advisor", "operator", "queue", "curator"],
        {
            "advisor": ids[5],
            "operator": ids[5],
        },
    )

    assert assigned["advisor"] == ids[5]
    assert len(set(assigned.values())) == len(assigned)
    assert set(assigned.values()).issubset(ids)


def test_new_agents_receive_unused_avatars_deterministically():
    first = assign_unique_avatar_ids(["advisor", "operator"], {})
    second = assign_unique_avatar_ids(["advisor", "operator", "queue"], first)

    assert second["advisor"] == first["advisor"]
    assert second["operator"] == first["operator"]
    assert second["queue"] not in first.values()
