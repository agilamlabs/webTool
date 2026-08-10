"""Regression checks for repository automation configuration."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).parents[1]


def test_github_actions_are_pinned_to_full_shas() -> None:
    workflow = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in uses)


def test_dependabot_tracks_python_and_github_actions() -> None:
    config = yaml.safe_load((_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))

    assert config["version"] == 2
    assert {update["package-ecosystem"] for update in config["updates"]} == {
        "github-actions",
        "pip",
    }

    pip_updates = next(
        update for update in config["updates"] if update["package-ecosystem"] == "pip"
    )
    assert pip_updates["ignore"] == [
        {"dependency-name": "mcp", "update-types": ["version-update:semver-major"]}
    ]


def test_mcp_dependency_stays_on_compatible_major() -> None:
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert pyproject.count('"mcp[cli]>=1.0.0,<2.0.0"') == 2
