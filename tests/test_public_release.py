"""Regression checks for publishing SelfApply without candidate data."""

import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_source_contains_no_obvious_candidate_identifiers() -> None:
    runtime = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (ROOT / "src" / "selfapply").rglob("*")
        if path.is_file() and path.suffix in {".py", ".yaml", ".json", ".html", ".js"}
    ).casefold()
    forbidden_patterns = (
        r"/users/[a-z0-9._-]+/",
        r"\b[a-z0-9._%+-]+@gmail\.com\b",
    )
    for pattern in forbidden_patterns:
        assert not re.search(pattern, runtime), (
            f"candidate-specific value leaked into runtime: {pattern}"
        )


def test_repository_does_not_track_runtime_candidate_artifacts() -> None:
    forbidden_names = {
        "profile.json", "resume.pdf", "resume.docx", "resume.txt",
        "selfapply.db", ".env",
    }
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True,
    ).stdout.decode().split("\0")
    leaked = {
        path for path in tracked
        if path and Path(path).name.casefold() in forbidden_names
    }
    assert not leaked, f"runtime candidate artifacts are tracked: {sorted(leaked)}"


def test_public_example_contains_no_saved_password() -> None:
    example = json.loads((ROOT / "profile.example.json").read_text(encoding="utf-8"))
    assert "password" not in example.get("personal", {})


def test_documentation_screenshots_are_packaged() -> None:
    assert (ROOT / "docs/images/selfapply-onboarding.jpg").is_file()
    assert (ROOT / "docs/images/selfapply-profile.jpg").is_file()
