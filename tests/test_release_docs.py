from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_CHECKER = ROOT / "tools" / "check_markdown_links.py"
RELEASE_VERIFIER = ROOT / "tools" / "verify_release.py"

PUBLISHED_DOCS = {
    "docs/POSITIONING.md",
    "docs/RELEASE_PLAN.md",
    "docs/TESTING.md",
    "docs/VERIFICATION.md",
    "docs/artifacts/README.md",
}


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

    assert missing == ["docs/broken.md: ../not-there.md"]


def test_release_verifier_requires_all_published_evidence_docs():
    verifier = load_module(RELEASE_VERIFIER, "release_artifact_verifier")

    assert PUBLISHED_DOCS <= set(verifier.SDIST_SUPPORT_FILES)
