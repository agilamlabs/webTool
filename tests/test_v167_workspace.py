"""v1.6.7 Workspace tests.

Verifies mode gates, path traversal protection, and integration with the
SkillRegistry's workspace-tier loading.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_agent.config import AppConfig, SkillsConfig, WorkspaceConfig
from web_agent.workspace import (
    SKILLS_DIR,
    Workspace,
    WorkspaceError,
)


def _ws(tmp_path: Path, **overrides) -> Workspace:
    cfg = AppConfig(
        base_dir=str(tmp_path),
        workspace=WorkspaceConfig(**{"enabled": True, **overrides}),
    )
    return Workspace(cfg, audit=None)


# ----------------------------------------------------------------------
# disabled by default
# ----------------------------------------------------------------------


def test_workspace_disabled_blocks_writes(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))  # enabled=False default
    ws = Workspace(cfg)
    assert ws.enabled() is False
    with pytest.raises(WorkspaceError, match="disabled"):
        ws.write_file("domain-skills/x.md", "body")


def test_workspace_disabled_list_skills_returns_empty(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))
    ws = Workspace(cfg)
    assert ws.list_skills() == []


# ----------------------------------------------------------------------
# markdown_skills_only (default mode)
# ----------------------------------------------------------------------


def test_markdown_skills_only_allows_md_under_skills_dir(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    p = ws.write_file("domain-skills/example.com/lookup.md", "# Lookup")
    assert p.exists()
    assert p.name == "lookup.md"


def test_markdown_skills_only_blocks_py_writes(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    with pytest.raises(WorkspaceError, match=r"only \.md files"):
        ws.write_file("domain-skills/x.py", "code")


def test_markdown_skills_only_blocks_md_outside_skills_dir(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    with pytest.raises(WorkspaceError, match=SKILLS_DIR):
        ws.write_file("notes/x.md", "body")


def test_markdown_skills_only_blocks_helpers_py(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.write_file("helpers.py", "import os")


def test_markdown_skills_only_write_skill_convenience(tmp_path: Path) -> None:
    """write_skill() auto-routes to domain-skills/ and appends .md."""
    ws = _ws(tmp_path)
    p = ws.write_skill("sec.gov/filing_search", "---\nname: x\n---\n")
    assert p.exists()
    assert "domain-skills" in p.parts
    assert p.suffix == ".md"


# ----------------------------------------------------------------------
# read_only mode
# ----------------------------------------------------------------------


def test_read_only_blocks_all_writes(tmp_path: Path) -> None:
    ws = _ws(tmp_path, mode="read_only")
    for path in ("domain-skills/x.md", "notes/x.txt", "helpers.py"):
        with pytest.raises(WorkspaceError, match="read_only"):
            ws.write_file(path, "body")


def test_read_only_allows_reads(tmp_path: Path) -> None:
    """Reads work even in read_only mode (the gate applies to writes)."""
    # First enable markdown mode briefly to create a file
    ws_write = _ws(tmp_path, mode="markdown_skills_only")
    ws_write.write_file("domain-skills/exists.md", "hello")

    ws_read = _ws(tmp_path, mode="read_only")
    assert ws_read.read_file("domain-skills/exists.md") == "hello"


# ----------------------------------------------------------------------
# reviewed_python_helpers mode
# ----------------------------------------------------------------------


def test_reviewed_python_helpers_allows_helpers_py(tmp_path: Path) -> None:
    ws = _ws(tmp_path, mode="reviewed_python_helpers")
    p = ws.write_file("helpers.py", "def hi(): return 1")
    assert p.name == "helpers.py"
    assert p.exists()


def test_reviewed_python_helpers_allows_md_anywhere(tmp_path: Path) -> None:
    ws = _ws(tmp_path, mode="reviewed_python_helpers")
    ws.write_file("notes/foo.md", "body")
    ws.write_file("domain-skills/x.md", "body")
    ws.write_file("anywhere/y.md", "body")  # all OK in this mode


def test_reviewed_python_helpers_blocks_arbitrary_py(tmp_path: Path) -> None:
    ws = _ws(tmp_path, mode="reviewed_python_helpers")
    with pytest.raises(WorkspaceError):
        ws.write_file("evil.py", "os.system('rm -rf')")


def test_reviewed_python_helpers_execute_gate_default_false(tmp_path: Path) -> None:
    """Even with mode=reviewed_python_helpers, execute_helpers defaults
    False -- writing helpers.py is allowed but the module is NOT imported."""
    ws = _ws(tmp_path, mode="reviewed_python_helpers")
    ws.write_file("helpers.py", "x = 1")
    assert ws.helper_module_path() is None  # execute_helpers=False


def test_reviewed_python_helpers_execute_true_returns_path(tmp_path: Path) -> None:
    ws = _ws(tmp_path, mode="reviewed_python_helpers", execute_helpers=True)
    ws.write_file("helpers.py", "x = 1")
    p = ws.helper_module_path()
    assert p is not None
    assert p.name == "helpers.py"


# ----------------------------------------------------------------------
# unsafe_python_helpers mode
# ----------------------------------------------------------------------


def test_unsafe_python_helpers_no_restrictions(tmp_path: Path) -> None:
    ws = _ws(tmp_path, mode="unsafe_python_helpers")
    # Any extension, any path
    ws.write_file("scratch/anything.py", "code")
    ws.write_file("data.json", "{}")
    ws.write_file("docs/notes.txt", "txt")


# ----------------------------------------------------------------------
# path traversal
# ----------------------------------------------------------------------


def test_path_traversal_blocked_in_all_modes(tmp_path: Path) -> None:
    """safe_join_path rejects ../ escapes in any workspace mode."""
    for mode in ("markdown_skills_only", "unsafe_python_helpers"):
        ws = _ws(tmp_path, mode=mode)
        with pytest.raises(ValueError, match="escapes"):
            ws.write_file("../escaped.md", "body")


def test_absolute_path_blocked(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    with pytest.raises(ValueError, match="Absolute"):
        ws.write_file("/etc/evil.md", "body")


# ----------------------------------------------------------------------
# Integration with SkillRegistry
# ----------------------------------------------------------------------


def test_workspace_skills_loaded_by_registry(tmp_path: Path) -> None:
    """End-to-end: write a .md skill via the workspace, then the
    SkillRegistry's workspace-tier load picks it up."""
    from web_agent.domain_skills import SkillRegistry

    ws_cfg = WorkspaceConfig(enabled=True, mode="markdown_skills_only")
    cfg = AppConfig(
        base_dir=str(tmp_path),
        workspace=ws_cfg,
        skills=SkillsConfig(builtin_skills_enabled=False),
    )

    skill_md = """---
name: ws_skill
domain: workspace-source.test
description: Loaded from workspace
---

## Use case
Test the workspace load path.
"""
    ws = Workspace(cfg)
    ws.write_skill("workspace-source.test/ws_skill.md", skill_md)

    reg = SkillRegistry(cfg)
    s = reg.get("workspace-source.test", "ws_skill")
    assert s is not None
    assert s.source == "workspace"
