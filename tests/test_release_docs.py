from __future__ import annotations

import importlib.util
import struct
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINK_CHECKER = ROOT / "tools" / "check_markdown_links.py"
RELEASE_VERIFIER = ROOT / "tools" / "verify_release.py"

PUBLISHED_DOCS = {
    "docs/INSTALLATION.md",
    "docs/POSITIONING.md",
    "docs/RELEASE_NOTES_0.12.3.md",
    "docs/RELEASE_NOTES_0.12.2.md",
    "docs/RELEASE_NOTES_0.12.1.md",
    "docs/RELEASE_NOTES_0.12.0.md",
    "docs/RELEASE_NOTES_0.11.0.md",
    "docs/RELEASE_PLAN.md",
    "docs/RUNBOOK_0.12.3.md",
    "docs/RUNBOOK_0.12.2.md",
    "docs/RUNBOOK_0.12.1.md",
    "docs/RUNBOOK_0.12.0.md",
    "docs/RUNBOOK_0.11.0.md",
    "docs/SECURITY.md",
    "docs/TESTING.md",
    "docs/USER_GUIDE.md",
    "docs/VERIFICATION.md",
    "docs/artifacts/README.md",
    "docs/artifacts/final-0.12.3-integration-report.json",
    "docs/artifacts/final-0.12.2-integration-report.json",
    "docs/artifacts/final-0.12.1-integration-report.json",
    "docs/artifacts/final-0.12.0-integration-report.json",
    "docs/artifacts/final-0.11.0-integration-report.json",
    "docs/screenshots/README.md",
    "docs/screenshots/element-web-queue.png",
    "docs/screenshots/element-web-thread.png",
}
RELAY_PREVIEWS = {
    "docs/branding/artifacts/relay-avatar-light-bg-preview.png",
    "docs/branding/artifacts/relay-avatar-dark-bg-preview.png",
}
SCREENSHOTS = {
    "docs/screenshots/element-web-thread.png": (316, 610),
    "docs/screenshots/element-web-queue.png": (1010, 270),
}


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", data[16:24])


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_relative_link_checker_is_available():
    assert LINK_CHECKER.is_file()


def test_relative_link_checker_reports_missing_local_target(tmp_path: Path):
    checker = load_module(LINK_CHECKER, "release_link_checker")
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "[present](docs/present.md)\n"
        '[titled](docs/present.md "Read this")\n'
        "![missing image](docs/missing.png)\n"
        "An `[inline example](not-a-link.md)` is code.\n"
        "```text\n"
        "![fenced example](also-not-a-link.png)\n"
        "```\n"
        "```text\n"
        "![long-close example](long-close-not-a-link.png)\n"
        "````\n"
        "[external](https://example.org/report)\n"
        "[fragment](#setup)\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "present.md").write_text("present\n", encoding="utf-8")
    (tmp_path / "docs" / "broken.md").write_text(
        "[missing](../not-there.md)\n",
        encoding="utf-8",
    )

    missing = checker.find_missing_links(tmp_path)

    assert missing == [
        "README.md: docs/missing.png",
        "docs/broken.md: ../not-there.md",
    ]


def test_release_verifier_requires_all_published_evidence_docs():
    verifier = load_module(RELEASE_VERIFIER, "release_artifact_verifier")

    assert PUBLISHED_DOCS <= set(verifier.SDIST_SUPPORT_FILES)


def test_release_verifier_requires_relay_preview_assets():
    verifier = load_module(RELEASE_VERIFIER, "release_artifact_verifier_brand")

    assert RELAY_PREVIEWS <= set(verifier.SDIST_SUPPORT_FILES)


def test_release_verifier_requires_documentation_screenshots():
    verifier = load_module(RELEASE_VERIFIER, "release_artifact_verifier_screenshots")

    assert set(SCREENSHOTS) <= set(verifier.SDIST_SUPPORT_FILES)


def test_landing_page_is_concise_and_links_durable_docs():
    readme_path = ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")

    assert 160 <= len(readme.splitlines()) <= 220
    for target in (
        "docs/USER_GUIDE.md",
        "docs/INSTALLATION.md",
        "docs/SECURITY.md",
        "docs/VERIFICATION.md",
        "docs/branding/README.md",
        "docs/screenshots/README.md",
    ):
        assert f"]({target})" in readme
    assert "second agent process" in readme
    assert "## Quick start" in readme
    assert "## Current boundaries" in readme
    assert "docs/screenshots/agent-inbox-generic.png" in readme
    assert "docs/screenshots/element-web-queue.png" in readme


def test_docs_screenshots_match_documented_dimensions():
    provenance = (ROOT / "docs" / "screenshots" / "README.md").read_text(
        encoding="utf-8"
    )

    for relative, dimensions in SCREENSHOTS.items():
        assert png_dimensions(ROOT / relative) == dimensions
        width, height = dimensions
        assert f"{width} x {height}" in provenance
    assert "No MXID is visible" in provenance
    assert "hover tooltips" in provenance
    assert "not a live Codex model turn" in provenance


def make_sdist(path: Path, verifier, names: set[str]) -> None:
    root = f"openrig_matrix_bridge-{verifier.project_version()}"
    with tarfile.open(path, "w:gz") as archive:
        for name in sorted(names):
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = 0
            archive.addfile(info)


def test_release_verifier_accepts_complete_sdist(tmp_path: Path):
    verifier = load_module(RELEASE_VERIFIER, "release_artifact_verifier_complete")
    artifact = tmp_path / "complete.tar.gz"
    make_sdist(artifact, verifier, verifier.SDIST_SUPPORT_FILES)

    verifier.inspect_sdist(artifact)


def test_release_verifier_rejects_sdist_missing_evidence(tmp_path: Path):
    verifier = load_module(RELEASE_VERIFIER, "release_artifact_verifier_missing")
    artifact = tmp_path / "missing.tar.gz"
    make_sdist(
        artifact,
        verifier,
        set(verifier.SDIST_SUPPORT_FILES) - {"docs/VERIFICATION.md"},
    )

    with pytest.raises(SystemExit, match="docs/VERIFICATION.md"):
        verifier.inspect_sdist(artifact)
