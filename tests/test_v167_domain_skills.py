"""v1.6.7 Domain Skills foundation tests.

Covers parser, registry loading (3-tier priority), input validation,
URL discovery, and apply-dispatch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.config import AppConfig, SkillsConfig, WorkspaceConfig
from web_agent.domain_skills import (
    SkillError,
    SkillInputError,
    SkillNotFoundError,
    SkillNotRunnableError,
    SkillRegistry,
    parse_skill_file,
    validate_inputs,
)
from web_agent.models import DomainSkill, SkillInputSpec

# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


SAMPLE_SKILL_MD = """---
name: filing_search
domain: sec.gov
description: Find a SEC filing
inputs:
  company:
    type: str
    required: true
    description: Company name
  form_type:
    type: str
    required: false
    default: "10-K"
output_schema:
  filing_url: str
  filing_date: str
---

## Use case
Locate a recent SEC filing.

## Recommended flow
1. Search EDGAR for the company.
2. Open the filings list.
3. Filter by form_type.

## Known selectors
- Search box: `input#company`
- Filing table: `table.tableFile2`

## Known traps
- Some filings redirect to archive pages.
- Avoid raw .txt filings.

## Output expectation
Returns the URL and date of the first matching filing.
"""


def _write_skill(dir_: Path, name: str, content: str = SAMPLE_SKILL_MD) -> Path:
    p = dir_ / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------


def test_parse_full_frontmatter(tmp_path: Path) -> None:
    p = _write_skill(tmp_path, "filing_search")
    skill = parse_skill_file(p, source="project")

    assert skill.name == "filing_search"
    assert skill.domain == "sec.gov"
    assert skill.description == "Find a SEC filing"
    assert skill.runnable is False  # not declared in frontmatter
    assert "company" in skill.inputs
    assert skill.inputs["company"].required is True
    assert skill.inputs["form_type"].default == "10-K"
    assert skill.output_schema == {"filing_url": "str", "filing_date": "str"}
    assert skill.recommended_flow == [
        "Search EDGAR for the company.",
        "Open the filings list.",
        "Filter by form_type.",
    ]
    assert skill.known_selectors == {
        "Search box": "input#company",
        "Filing table": "table.tableFile2",
    }
    assert "Some filings redirect" in skill.known_traps[0]
    assert skill.source == "project"


def test_parse_missing_required_frontmatter_raises(tmp_path: Path) -> None:
    bad = "---\nname: x\n---\nbody"
    p = tmp_path / "bad.md"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(SkillError, match="domain"):
        parse_skill_file(p)


def test_parse_invalid_input_spec_raises(tmp_path: Path) -> None:
    bad = """---
name: x
domain: example.com
description: x
inputs:
  q: "not a mapping"
---
body
"""
    p = tmp_path / "bad.md"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(SkillError, match="must be a mapping"):
        parse_skill_file(p)


def test_parse_domain_lowercased(tmp_path: Path) -> None:
    md = SAMPLE_SKILL_MD.replace("domain: sec.gov", "domain: SEC.GOV")
    p = _write_skill(tmp_path, "filing_search", md)
    skill = parse_skill_file(p)
    assert skill.domain == "sec.gov"


# ----------------------------------------------------------------------
# input validation
# ----------------------------------------------------------------------


def test_validate_inputs_fills_default() -> None:
    skill = DomainSkill(
        name="x",
        domain="example.com",
        description="x",
        inputs={
            "q": SkillInputSpec(type="str", required=True),
            "n": SkillInputSpec(type="int", default=5),
        },
        source="builtin",
        source_path="/dev/null",
    )
    out = validate_inputs(skill, {"q": "hello"})
    assert out == {"q": "hello", "n": 5}


def test_validate_inputs_missing_required_raises() -> None:
    skill = DomainSkill(
        name="x",
        domain="example.com",
        description="x",
        inputs={"q": SkillInputSpec(type="str", required=True)},
        source="builtin",
        source_path="/dev/null",
    )
    with pytest.raises(SkillInputError, match="required input 'q'"):
        validate_inputs(skill, {})


def test_validate_inputs_coerces_int() -> None:
    skill = DomainSkill(
        name="x",
        domain="example.com",
        description="x",
        inputs={"n": SkillInputSpec(type="int")},
        source="builtin",
        source_path="/dev/null",
    )
    out = validate_inputs(skill, {"n": "42"})
    assert out == {"n": 42}


def test_validate_inputs_bool_string_coercion() -> None:
    skill = DomainSkill(
        name="x",
        domain="example.com",
        description="x",
        inputs={"flag": SkillInputSpec(type="bool")},
        source="builtin",
        source_path="/dev/null",
    )
    for raw, expected in [("true", True), ("0", False), ("yes", True), ("nope", False)]:
        out = validate_inputs(skill, {"flag": raw})
        assert out["flag"] is expected, raw


# ----------------------------------------------------------------------
# registry loading + priority
# ----------------------------------------------------------------------


def test_registry_disabled_when_all_flags_off(tmp_path: Path) -> None:
    cfg = AppConfig(
        base_dir=str(tmp_path),
        skills=SkillsConfig(project_skills_enabled=False, builtin_skills_enabled=False),
    )
    reg = SkillRegistry(cfg)
    assert reg.list_all() == []


def test_registry_loads_builtin_by_default(tmp_path: Path) -> None:
    """Default config has builtin_skills_enabled=True. The 3 bundled
    skills should appear regardless of project/workspace settings."""
    cfg = AppConfig(base_dir=str(tmp_path))
    reg = SkillRegistry(cfg)
    domains = {s.domain for s in reg.list_all()}
    assert "sec.gov" in domains
    assert "github.com" in domains
    assert "ec.europa.eu" in domains


def test_registry_project_overrides_builtin(tmp_path: Path) -> None:
    """If a project skill at .webtool-skills/<domain>/ has the same
    (domain, name) as a bundled skill, the project version wins."""
    skills_dir = tmp_path / ".webtool-skills" / "sec.gov"
    skills_dir.mkdir(parents=True)
    custom_md = SAMPLE_SKILL_MD.replace(
        "description: Find a SEC filing",
        "description: CUSTOM project override",
    )
    (skills_dir / "filing_search.md").write_text(custom_md, encoding="utf-8")

    cfg = AppConfig(
        base_dir=str(tmp_path),
        skills=SkillsConfig(project_skills_enabled=True, builtin_skills_enabled=True),
    )
    reg = SkillRegistry(cfg)
    s = reg.get("sec.gov", "filing_search")
    assert s is not None
    assert s.description == "CUSTOM project override"
    assert s.source == "project"


def test_registry_workspace_skills_loaded(tmp_path: Path) -> None:
    ws_skills = tmp_path / ".webtool-workspace" / "domain-skills"
    ws_skills.mkdir(parents=True)
    md = SAMPLE_SKILL_MD.replace("name: filing_search", "name: workspace_skill").replace(
        "domain: sec.gov", "domain: workspace-only.test"
    )
    (ws_skills / "skill.md").write_text(md, encoding="utf-8")

    cfg = AppConfig(
        base_dir=str(tmp_path),
        workspace=WorkspaceConfig(enabled=True),
        skills=SkillsConfig(builtin_skills_enabled=False),
    )
    reg = SkillRegistry(cfg)
    skill = reg.get("workspace-only.test", "workspace_skill")
    assert skill is not None
    assert skill.source == "workspace"


# ----------------------------------------------------------------------
# discovery
# ----------------------------------------------------------------------


def test_get_for_url_matches_host_suffix(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))
    reg = SkillRegistry(cfg)

    matches = reg.get_for_url("https://www.sec.gov/cgi-bin/browse-edgar")
    assert len(matches) == 1
    assert matches[0].name == "filing_search"


def test_get_for_url_no_match_returns_empty(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))
    reg = SkillRegistry(cfg)
    assert reg.get_for_url("https://nothing-matches.example") == []


def test_get_for_url_avoids_false_suffix(tmp_path: Path) -> None:
    """``sec.gov`` should NOT match ``not-sec.gov`` -- the suffix check
    requires a dot before the registered domain."""
    cfg = AppConfig(base_dir=str(tmp_path))
    reg = SkillRegistry(cfg)
    assert reg.get_for_url("https://not-sec.gov/path") == []


# ----------------------------------------------------------------------
# apply
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_unknown_skill_raises(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))
    reg = SkillRegistry(cfg)
    fake_agent = MagicMock()
    with pytest.raises(SkillNotFoundError):
        await reg.apply(fake_agent, "https://www.sec.gov/", "nonexistent_skill")


@pytest.mark.asyncio
async def test_apply_user_markdown_skill_not_runnable_raises(tmp_path: Path) -> None:
    """A user markdown skill (runnable: false) cannot be applied."""
    skills_dir = tmp_path / ".webtool-skills" / "example.com"
    skills_dir.mkdir(parents=True)
    md = SAMPLE_SKILL_MD.replace("domain: sec.gov", "domain: example.com")
    (skills_dir / "filing_search.md").write_text(md, encoding="utf-8")

    cfg = AppConfig(
        base_dir=str(tmp_path),
        skills=SkillsConfig(project_skills_enabled=True, builtin_skills_enabled=False),
    )
    reg = SkillRegistry(cfg)
    fake_agent = MagicMock()
    with pytest.raises(SkillNotRunnableError):
        await reg.apply(fake_agent, "https://example.com/", "filing_search")


@pytest.mark.asyncio
async def test_apply_runs_bundled_runner_end_to_end(tmp_path: Path) -> None:
    """The bundled sec.gov filing_search runner calls
    agent.search_and_extract. Mock the agent surface and verify the
    skill returns the expected output shape."""
    from web_agent.models import SearchResultItem

    cfg = AppConfig(base_dir=str(tmp_path))
    reg = SkillRegistry(cfg)

    fake_item = SearchResultItem(
        position=1,
        title="Apple Inc 10-K",
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm",
        snippet="Annual report",
    )
    # Cheaper: build a minimal AgentResult-shaped object
    fake_agent = MagicMock()
    fake_agent.search_and_extract = AsyncMock(
        return_value=MagicMock(
            pages=[
                MagicMock(
                    url=fake_item.url,
                    title="Apple Inc 10-K",
                    description="Annual report",
                    content="Filing body text here...",
                    date="2023-09-30",
                )
            ]
        )
    )
    fake_agent._correlation_id = "test-corr"

    result = await reg.apply(
        fake_agent,
        "https://www.sec.gov/cgi-bin/browse-edgar",
        "filing_search",
        {"company": "Apple Inc"},
    )

    assert result.succeeded
    assert result.skill_name == "filing_search"
    assert result.domain == "sec.gov"
    assert result.output["filing_url"] == fake_item.url
    assert result.output["form_type"] == "10-K"
    assert result.output["accession_number"] == "000032019323000106"
    fake_agent.search_and_extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_validates_required_inputs(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))
    reg = SkillRegistry(cfg)
    fake_agent = MagicMock()
    fake_agent._correlation_id = None
    with pytest.raises(SkillInputError, match="required input 'company'"):
        await reg.apply(fake_agent, "https://www.sec.gov/", "filing_search", {})


@pytest.mark.asyncio
async def test_apply_runner_exception_returns_failed_result(tmp_path: Path) -> None:
    """Runner-level errors don't propagate as exceptions; they show up
    as ``succeeded=False`` with a populated ``errors`` list."""
    cfg = AppConfig(base_dir=str(tmp_path))
    reg = SkillRegistry(cfg)
    fake_agent = MagicMock()
    fake_agent.search_and_extract = AsyncMock(side_effect=RuntimeError("boom"))
    fake_agent._correlation_id = None

    result = await reg.apply(
        fake_agent,
        "https://www.sec.gov/",
        "filing_search",
        {"company": "X"},
    )
    assert result.succeeded is False
    assert "boom" in result.errors[0].message


# ----------------------------------------------------------------------
# Bundled skills sanity
# ----------------------------------------------------------------------


def test_validate_inputs_caps_extra_keys(tmp_path: Path) -> None:
    """Code-review M3 regression: caller-supplied extras are passed
    through to the runner but capped at MAX_EXTRA_INPUTS to bound the
    call-surface against prompt-injection."""
    from web_agent.domain_skills import MAX_EXTRA_INPUTS

    skill = DomainSkill(
        name="x",
        domain="example.com",
        description="x",
        inputs={"q": SkillInputSpec(type="str", required=True)},
        source="builtin",
        source_path="/dev/null",
    )
    too_many = {"q": "v"} | {f"extra_{i}": i for i in range(MAX_EXTRA_INPUTS + 1)}
    with pytest.raises(SkillInputError, match="Too many extra input keys"):
        validate_inputs(skill, too_many)

    # Exactly at the cap is OK.
    at_cap = {"q": "v"} | {f"extra_{i}": i for i in range(MAX_EXTRA_INPUTS)}
    result = validate_inputs(skill, at_cap)
    assert len(result) == MAX_EXTRA_INPUTS + 1  # +1 for 'q'


def test_load_project_with_windows_absolute_skipped_safely(tmp_path: Path) -> None:
    """Code-review C1 regression: a Windows-style absolute path
    ('C:\\...') in skill_dirs is recognized as absolute on POSIX too
    via the cross-platform helper. The path won't exist on a Linux
    test runner, so the load is a no-op rather than a silent join to
    base_dir that would have produced a bogus '<base>/C:\\...' tree."""
    cfg = AppConfig(
        base_dir=str(tmp_path),
        skills=SkillsConfig(
            project_skills_enabled=True,
            builtin_skills_enabled=False,
            skill_dirs=[r"C:\not\a\real\directory\anywhere"],
        ),
    )
    # Constructing the registry MUST NOT raise and MUST NOT spuriously
    # land project skills under base_dir.
    reg = SkillRegistry(cfg)
    assert reg.list_all() == []


def test_all_builtin_skills_have_runners(tmp_path: Path) -> None:
    """Every module in BUILTIN_SKILLS exposes a callable ``run``."""
    from web_agent.builtin_skills import BUILTIN_SKILLS

    for module in BUILTIN_SKILLS:
        assert hasattr(module, "run"), f"{module.__name__} has no 'run'"
        assert callable(module.run)


def test_all_builtin_skill_md_files_parse(tmp_path: Path) -> None:
    """Every bundled skill.md parses without error and declares its 3 required fields."""
    from web_agent.builtin_skills import BUILTIN_SKILLS

    for module in BUILTIN_SKILLS:
        md_path = Path(module.__file__).parent / "skill.md"
        skill = parse_skill_file(md_path, source="builtin")
        assert skill.name
        assert skill.domain
        assert skill.description


def test_github_skill_sanitizes_query_operators() -> None:
    """Code-review M4 + v1.6.16 GH-1 regression: the github_release_download
    skill composes a search query that includes user-supplied ``repo`` and
    ``asset_pattern`` fields. Without sanitization, a prompt-injected
    pattern like '" OR site:evil.com'' could escape the
    ``site:github.com`` scope. The sanitizer strips quotes, parens,
    pipes, brackets, the ``site:`` field operator, and the standalone
    boolean ``OR`` -- collapsing the resulting whitespace -- while leaving
    ordinary query text intact."""
    from web_agent.builtin_skills.github_release_download import _sanitize_query_term

    # v1.6.16 GH-1: ``site:`` and the boolean ``OR`` are now actually
    # stripped (the prior regex removed neither), so the scope-escape
    # payload collapses to a bare hostname token with no operators.
    assert _sanitize_query_term('" OR site:evil.com"') == "evil.com"
    assert _sanitize_query_term("(group1 | group2)") == "group1 group2"
    assert _sanitize_query_term("normal-string_v1.0") == "normal-string_v1.0"
    assert _sanitize_query_term("") == ""
