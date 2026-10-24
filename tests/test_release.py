from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path

from openrig_matrix_bridge import __version__
from openrig_matrix_bridge.avatar_pool import avatar_pool_ids, profile_png

ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_synchronized_across_metadata():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    project_version = pyproject["project"]["version"]
    locked_project = next(
        package
        for package in lock["package"]
        if package["name"] == "openrig-matrix-bridge"
    )

    assert project_version == __version__ == locked_project["version"]
    assert changelog.splitlines()[2] == f"## {project_version}"


def test_release_requires_installed_avatar_assets():
    branding_dir = (
        resources.files("openrig_matrix_bridge.assets")
        .joinpath("branding")
        .joinpath("export")
    )
    avatar_dir = resources.files("openrig_matrix_bridge.assets").joinpath("avatars")

    assert avatar_dir.joinpath("manifest.json").is_file()
    assert len(avatar_pool_ids()) == 32
    assert (
        profile_png()
        == branding_dir.joinpath("relay-avatar-256.png").read_bytes()
    )


def test_dev_extra_contains_release_validation_tools():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_requirements = "\n".join(
        pyproject["project"]["optional-dependencies"]["dev"]
    )

    assert "build" in dev_requirements
    assert "pytest" in dev_requirements
    assert "ruff" in dev_requirements
