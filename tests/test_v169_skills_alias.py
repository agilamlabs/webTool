"""v1.6.9 SkillsConfig.enabled -> project_skills_enabled rename + alias tests.

The old ``enabled`` name was ambiguous (it only governed the
*project-tier* skill load, not the workspace or builtin tiers). v1.6.9
renames to ``project_skills_enabled`` and keeps the old name working
via ``AliasChoices`` for one release with a ``DeprecationWarning``.
"""

from __future__ import annotations

import warnings

import pytest
from web_agent import SkillsConfig


def test_new_name_works() -> None:
    cfg = SkillsConfig(project_skills_enabled=True)
    assert cfg.project_skills_enabled is True


def test_old_name_still_works_via_alias() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cfg = SkillsConfig(enabled=True)
    assert cfg.project_skills_enabled is True


def test_old_name_emits_deprecation_warning() -> None:
    with pytest.warns(DeprecationWarning, match="project_skills_enabled"):
        SkillsConfig(enabled=False)


def test_new_name_does_not_emit_deprecation_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        # Must NOT raise
        SkillsConfig(project_skills_enabled=False)


def test_default_is_false() -> None:
    cfg = SkillsConfig()
    assert cfg.project_skills_enabled is False
