#!/usr/bin/env python3
"""Build-artifact inspection and clean-install smoke checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDIST_SUPPORT_FILES = {
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "config.example.yaml",
    "deploy/openrig-matrix-bridge.service",
    "deploy/openrig-matrix-start",
    "docs/TESTING.md",
    "tools/fake_openrig.py",
    "tools/matrix_integration.py",
    "tools/verify_release.py",
}
INSTALL_PROBE = r"""
import json
from importlib import metadata, resources

from openrig_matrix_bridge import __version__
from openrig_matrix_bridge.avatar_pool import avatar_pool_ids, profile_png

avatars = resources.files("openrig_matrix_bridge.assets").joinpath("avatars")
print(json.dumps({
    "version": __version__,
    "distribution_version": metadata.version("openrig-matrix-bridge"),
    "avatar_count": len(avatar_pool_ids()),
    "manifest_present": avatars.joinpath("manifest.json").is_file(),
    "profile_png": profile_png()[:8].hex(),
}))
"""


def project_version() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


def artifact_paths(dist: Path, version: str) -> tuple[Path, Path]:
    wheels = sorted(dist.glob(f"openrig_matrix_bridge-{version}-*.whl"))
    sdists = sorted(dist.glob(f"openrig_matrix_bridge-{version}.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            f"expected one wheel and one sdist for {version} in {dist}; "
            f"found {len(wheels)} wheel(s), {len(sdists)} sdist(s)"
        )
    return wheels[0], sdists[0]


def inspect_wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    package = "openrig_matrix_bridge"
    required = {
        f"{package}/__init__.py",
        f"{package}/cli.py",
        f"{package}/assets/avatars/manifest.json",
        f"{package}/assets/avatars/openrig-profile.png",
        *(f"{package}/assets/avatars/openrig-{number:02}.png" for number in range(1, 33)),
    }
    missing = sorted(required - names)
    if missing:
        raise SystemExit(f"wheel is missing required files: {', '.join(missing)}")
    dist_info = f"{package}-{version}.dist-info/"
    if not any(name.startswith(dist_info) for name in names):
        raise SystemExit(f"wheel has no {version} dist-info directory")
    if f"{dist_info}licenses/LICENSE" not in names:
        raise SystemExit("wheel does not contain its MIT license file")
    print(f"wheel contents: {path.name} ({len(names)} files)")


def inspect_sdist(path: Path) -> None:
    root = f"openrig_matrix_bridge-{project_version()}"
    with tarfile.open(path) as archive:
        names = set(archive.getnames())
    missing = sorted(
        relative
        for relative in SDIST_SUPPORT_FILES
        if f"{root}/{relative}" not in names
    )
    if missing:
        raise SystemExit(f"sdist is missing release support files: {', '.join(missing)}")
    print(f"sdist contents: {path.name} ({len(names)} files)")


def install_smoke(artifact: Path, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"openrig-{artifact.suffix}-install-") as raw:
        workdir = Path(raw)
        venv = workdir / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            cwd=workdir,
        )
        python = venv / "bin" / "python"
        console = venv / "bin" / "openrig-matrix-bridge"
        install = [str(python), "-m", "pip", "install", "--quiet", str(artifact)]
        subprocess.run(install, check=True, cwd=workdir)

        environment = {
            **os.environ,
            "HOME": str(workdir / "home"),
            "PYTHONNOUSERSITE": "1",
        }
        Path(environment["HOME"]).mkdir()
        version_result = subprocess.run(
            [str(console), "--version"],
            check=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            env=environment,
        )
        if version_result.stdout.strip() != version:
            raise SystemExit(
                f"{artifact.name}: console version was {version_result.stdout.strip()!r}, "
                f"expected {version!r}"
            )
        help_result = subprocess.run(
            [str(console), "--help"],
            check=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            env=environment,
        )
        if "OpenRig Matrix bridge" not in help_result.stdout:
            raise SystemExit(f"{artifact.name}: console --help is missing product text")
        try:
            probe = subprocess.run(
                [str(python), "-c", INSTALL_PROBE],
                check=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                env=environment,
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip()
            raise SystemExit(
                f"{artifact.name}: installed-package probe failed: {detail}"
            ) from exc
        result = json.loads(probe.stdout)
        if result["version"] != version or result["distribution_version"] != version:
            raise SystemExit(f"{artifact.name}: installed metadata version mismatch: {result}")
        if result["avatar_count"] != 32 or not result["manifest_present"]:
            raise SystemExit(f"{artifact.name}: installed avatar assets are incomplete: {result}")
        if result["profile_png"] != b"\x89PNG\r\n\x1a\n".hex():
            raise SystemExit(f"{artifact.name}: installed profile asset is not a PNG")
        print(
            f"clean install: {artifact.name} -> console --version/--help and "
            f"{result['avatar_count']} avatars ({venv})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="inspect archive contents without creating clean environments",
    )
    args = parser.parse_args()
    dist = args.dist.resolve()
    version = project_version()
    wheel, sdist = artifact_paths(dist, version)
    inspect_wheel(wheel, version)
    inspect_sdist(sdist)
    if not args.inspect_only:
        install_smoke(wheel, version)
        install_smoke(sdist, version)
    print(f"release artifacts verified for {version}")


if __name__ == "__main__":
    main()
