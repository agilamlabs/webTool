"""v1.6.7: Domain Skills registry.

A "skill" is a structured markdown file describing how to handle a
specific website (sec.gov, github.com, etc.). Skills make webTool
behave like a learning system that accumulates reusable knowledge --
the strongest idea adapted from `browser-harness` per the upgrade
spec.

File format::

    ---
    name: filing_search
    domain: sec.gov
    description: Find and extract SEC filings for a company
    runnable: false
    inputs:
      company:
        type: str
        required: true
        description: Company name or CIK
      form_type:
        type: str
        required: false
        default: "10-K"
    output_schema:
      filing_url: str
      form_type: str
      filing_date: str
    ---

    ## Use case
    ...

    ## Recommended flow
    1. Search EDGAR for the company name.
    2. Open the company submissions page.
    3. Filter by form type.

    ## Known selectors
    - Search box: `input#company`
    - Filing table: `table.filings tbody tr`

    ## Known traps
    - Some links redirect to old accession-number pages.

    ## Output expectation
    Returns one record per filing with extracted body text.

Discovery order (priority on (domain, name) collision):

    project (highest) > workspace > builtin (lowest)

Bundled skills (under ``web_agent/builtin_skills/<name>/``) ship with
a Python runner exposing ``async run(agent, url, inputs) -> dict``.
User markdown skills are informational by default (``runnable=False``)
and ``apply_domain_skill`` raises ``SkillNotRunnableError`` for them.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol
from urllib.parse import urlparse

import frontmatter
from loguru import logger

from .config import AppConfig
from .exceptions import SkillError, SkillInputError, SkillNotFoundError, SkillNotRunnableError
from .models import (
    DomainSkill,
    SkillApplicationResult,
    SkillInputSpec,
    ToolError,
    ToolSeverity,
    ToolWarning,
)
from .utils import _is_cross_platform_absolute, safe_join_path

if TYPE_CHECKING:
    from .agent import Agent


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------
#
# v1.6.14 (review D-9): the skill exception hierarchy now lives in the
# central :mod:`web_agent.exceptions` module alongside every other
# public exception. They are re-exported here (and via ``__all__``) so
# existing ``from web_agent.domain_skills import SkillNotRunnableError``
# imports keep working unchanged.


# ----------------------------------------------------------------------
# Runner protocol
# ----------------------------------------------------------------------


class BuiltinSkillRunner(Protocol):
    """Bundled skills expose this callable so the registry can dispatch."""

    async def __call__(self, agent: Agent, url: str, inputs: dict[str, Any]) -> dict[str, Any]: ...


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------


def _parse_recommended_flow(body: str) -> list[str]:
    """Extract numbered steps from the '## Recommended flow' section."""
    match = re.search(
        r"##\s+Recommended\s+flow\s*\n(.+?)(?=\n##\s|\Z)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    section = match.group(1)
    steps: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        # Accept both "1." and "1)" numbered styles
        m = re.match(r"^\d+[\.\)]\s+(.+)$", line)
        if m:
            steps.append(m.group(1).strip())
    return steps


def _parse_bullet_dict(body: str, header: str) -> dict[str, str]:
    """Parse a section of ``- Label: value`` bullets into {label: value}."""
    pattern = rf"##\s+{re.escape(header)}\s*\n(.+?)(?=\n##\s|\Z)"
    match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
    if not match:
        return {}
    section = match.group(1)
    out: dict[str, str] = {}
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- "):
            line = line[2:].strip()
            # Split on first colon outside backticks
            colon_idx = -1
            in_tick = False
            for i, c in enumerate(line):
                if c == "`":
                    in_tick = not in_tick
                elif c == ":" and not in_tick:
                    colon_idx = i
                    break
            if colon_idx > 0:
                label = line[:colon_idx].strip()
                value = line[colon_idx + 1 :].strip().strip("`")
                if label and value:
                    out[label] = value
    return out


def _parse_bullet_list(body: str, header: str) -> list[str]:
    """Parse a section of ``- item`` bullets into a list."""
    pattern = rf"##\s+{re.escape(header)}\s*\n(.+?)(?=\n##\s|\Z)"
    match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    section = match.group(1)
    items: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
    return items


def _parse_section_text(body: str, header: str) -> Optional[str]:
    """Extract the free-text body of one '## Header' section."""
    pattern = rf"##\s+{re.escape(header)}\s*\n(.+?)(?=\n##\s|\Z)"
    match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    text = match.group(1).strip()
    return text or None


def parse_skill_file(path: Path, source: str = "project") -> DomainSkill:
    """Parse one .md skill file into a :class:`DomainSkill`.

    Raises ``SkillError`` if the frontmatter is missing required fields
    (``name``, ``domain``, ``description``) or fails type checks.
    """
    text = path.read_text(encoding="utf-8")
    try:
        post = frontmatter.loads(text)
    except Exception as exc:
        raise SkillError(f"Failed to parse frontmatter in {path}: {exc}") from exc

    meta = post.metadata or {}
    body = post.content or ""

    name = meta.get("name")
    domain = meta.get("domain")
    description = meta.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillError(f"{path}: frontmatter missing required 'name'")
    if not isinstance(domain, str) or not domain.strip():
        raise SkillError(f"{path}: frontmatter missing required 'domain'")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(f"{path}: frontmatter missing required 'description'")

    # Inputs: dict[str, dict] -> dict[str, SkillInputSpec]
    raw_inputs = meta.get("inputs", {}) or {}
    if not isinstance(raw_inputs, dict):
        raise SkillError(f"{path}: 'inputs' must be a mapping")
    inputs: dict[str, SkillInputSpec] = {}
    for key, spec in raw_inputs.items():
        if not isinstance(spec, dict):
            raise SkillError(f"{path}: input '{key}' must be a mapping")
        try:
            inputs[str(key)] = SkillInputSpec(**spec)
        except Exception as exc:
            raise SkillError(f"{path}: invalid input '{key}': {exc}") from exc

    output_schema = meta.get("output_schema", {}) or {}
    if not isinstance(output_schema, dict):
        raise SkillError(f"{path}: 'output_schema' must be a mapping")
    output_schema = {str(k): str(v) for k, v in output_schema.items()}

    runnable = bool(meta.get("runnable", False))

    return DomainSkill(
        name=name.strip(),
        domain=domain.strip().lower(),
        description=description.strip(),
        runnable=runnable,
        inputs=inputs,
        output_schema=output_schema,
        use_case=_parse_section_text(body, "Use case"),
        recommended_flow=_parse_recommended_flow(body),
        known_selectors=_parse_bullet_dict(body, "Known selectors"),
        known_traps=_parse_bullet_list(body, "Known traps"),
        output_expectation=_parse_section_text(body, "Output expectation"),
        source=source,  # type: ignore[arg-type]
        source_path=str(path.resolve()),
    )


# ----------------------------------------------------------------------
# Input validation
# ----------------------------------------------------------------------


def _coerce_input(value: Any, spec: SkillInputSpec) -> Any:
    """Coerce a raw input value to the declared spec.type. Raises on mismatch."""
    if spec.type == "str":
        return str(value)
    if spec.type == "int":
        return int(value)
    if spec.type == "float":
        return float(value)
    # spec.type is constrained by Literal["str", "int", "float", "bool"]
    # at the Pydantic level, so this final branch is "bool".
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


#: Maximum number of caller-supplied keys NOT declared in the skill's
#: ``inputs`` schema that ``validate_inputs`` will pass through to the
#: runner. Caps a prompt-injection vector that would otherwise let an
#: LLM-supplied input dict bloat the runner's call surface arbitrarily.
MAX_EXTRA_INPUTS = 20


def validate_inputs(skill: DomainSkill, supplied: dict[str, Any] | None) -> dict[str, Any]:
    """Validate caller-supplied inputs against the skill's declared schema.

    Fills in defaults; raises :class:`SkillInputError` for missing required
    fields or type-coercion failures. Extra keys (not declared in the
    skill's frontmatter) are passed through to the runner but capped at
    ``MAX_EXTRA_INPUTS`` to bound the call-surface size.
    """
    supplied = supplied or {}
    out: dict[str, Any] = {}
    for key, spec in skill.inputs.items():
        if key in supplied:
            try:
                out[key] = _coerce_input(supplied[key], spec)
            except (TypeError, ValueError) as exc:
                raise SkillInputError(f"input '{key}' must be {spec.type}: {exc}") from exc
        elif spec.required:
            raise SkillInputError(f"required input '{key}' missing")
        elif spec.default is not None:
            out[key] = spec.default

    # Accept extra keys (callers may pass session_id, tab_id, ad-hoc
    # context) but cap the count -- an unbounded passthrough is a
    # prompt-injection vector for future runners that iterate
    # ``inputs.items()``.
    extras = {k: v for k, v in supplied.items() if k not in skill.inputs}
    if len(extras) > MAX_EXTRA_INPUTS:
        raise SkillInputError(
            f"Too many extra input keys ({len(extras)}); "
            f"maximum is MAX_EXTRA_INPUTS={MAX_EXTRA_INPUTS}."
        )
    for key, value in extras.items():
        out[key] = value
    return out


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


class SkillRegistry:
    """In-memory registry of domain skills.

    Loaded once at Agent startup; reads markdown files from three
    directory tiers and indexes by ``(domain, name)``. Priority:
    project > workspace > builtin -- higher priority overrides lower
    on collision.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        # Skill metadata, keyed by (domain, name).
        self._skills: dict[tuple[str, str], DomainSkill] = {}
        # Bundled-skill runners, keyed by (domain, name). Only bundled
        # skills are dispatchable in v1.6.7 unless the workspace mode
        # allows adjacent Python.
        self._runners: dict[tuple[str, str], BuiltinSkillRunner] = {}
        # Load if ANY of the three sources is enabled. Each source's own
        # gate inside _load_all decides whether it actually loads anything.
        any_source = (
            config.skills.project_skills_enabled
            or config.skills.builtin_skills_enabled
            or config.workspace.enabled
        )
        if any_source:
            self._load_all()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """Load skills from builtin -> workspace -> project, in that order.

        Later sources override earlier ones on (domain, name) collision.
        """
        if self._config.skills.builtin_skills_enabled:
            self._load_builtin()
        if self._config.workspace.enabled:
            self._load_workspace()
        if self._config.skills.project_skills_enabled:  # v1.6.9: was .enabled
            self._load_project()
        logger.info("SkillRegistry loaded {n} skill(s)", n=len(self._skills))

    def _load_builtin(self) -> None:
        """Load skills shipped under ``web_agent/builtin_skills/<name>/``."""
        try:
            from . import builtin_skills

            registered = getattr(builtin_skills, "BUILTIN_SKILLS", [])
        except Exception as exc:
            logger.warning("Failed to import builtin_skills: {e}", e=exc)
            return
        for skill_module in registered:
            try:
                skill_md_path = Path(skill_module.__file__).parent / "skill.md"
                skill = parse_skill_file(skill_md_path, source="builtin")
                # Override the parsed runnable flag -- bundled skills are
                # ALWAYS runnable. The .md file's flag is informational.
                skill = skill.model_copy(update={"runnable": True})
                runner = getattr(skill_module, "run", None)
                if runner is None or not callable(runner):
                    logger.warning(
                        "Builtin skill {name} has no callable 'run'; skipping",
                        name=skill.name,
                    )
                    continue
                key = (skill.domain, skill.name)
                self._skills[key] = skill
                self._runners[key] = runner
            except Exception as exc:
                logger.warning(
                    "Failed to load builtin skill module {mod}: {e}",
                    mod=skill_module.__name__
                    if hasattr(skill_module, "__name__")
                    else skill_module,
                    e=exc,
                )

    def _load_workspace(self) -> None:
        """Load .md files under ``<workspace>/domain-skills/``."""
        ws_path = self._resolve_workspace_path()
        if ws_path is None:
            return
        skill_root = ws_path / "domain-skills"
        if not skill_root.is_dir():
            return
        for md_path in skill_root.glob("**/*.md"):
            try:
                skill = parse_skill_file(md_path, source="workspace")
                self._skills[(skill.domain, skill.name)] = skill
            except Exception as exc:
                logger.warning("Failed to load workspace skill {p}: {e}", p=md_path, e=exc)

    def _load_project(self) -> None:
        """Load .md files from every ``skills_dirs`` entry."""
        base = Path(self._config.base_dir).resolve()
        for raw_dir in self._config.skills.skill_dirs:
            try:
                # Use _is_cross_platform_absolute (v1.6.4 helper) instead of
                # Path.is_absolute() so a Windows-style 'C:\\...' path is
                # recognized as absolute on POSIX too (matches the project
                # convention documented in AGENTS.md).
                skill_root = (
                    Path(raw_dir)
                    if _is_cross_platform_absolute(raw_dir)
                    else safe_join_path(base, raw_dir)
                )
            except ValueError as exc:
                logger.warning("Skipping skill_dir {d}: {e}", d=raw_dir, e=exc)
                continue
            if not skill_root.is_dir():
                continue
            for md_path in skill_root.glob("**/*.md"):
                try:
                    skill = parse_skill_file(md_path, source="project")
                    self._skills[(skill.domain, skill.name)] = skill
                except Exception as exc:
                    logger.warning("Failed to load project skill {p}: {e}", p=md_path, e=exc)

    def _resolve_workspace_path(self) -> Optional[Path]:
        ws_cfg = self._config.workspace
        if not ws_cfg.enabled:
            return None
        base = Path(self._config.base_dir).resolve()
        if _is_cross_platform_absolute(ws_cfg.workspace_dir):
            return Path(ws_cfg.workspace_dir)
        try:
            return safe_join_path(base, ws_cfg.workspace_dir)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_all(self) -> list[DomainSkill]:
        return list(self._skills.values())

    def get(self, domain: str, name: str) -> Optional[DomainSkill]:
        return self._skills.get((domain.strip().lower(), name))

    def get_for_url(self, url: str) -> list[DomainSkill]:
        """Return skills whose ``domain`` is a suffix of the URL host.

        ``sec.gov`` matches both ``www.sec.gov`` and ``www.sec.gov`` ->
        also ``cgi-bin.sec.gov``. Bare ``com`` would match everything --
        we still allow it (caller can write narrower domains).
        """
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return []
        matches: list[DomainSkill] = []
        for (skill_domain, _name), skill in self._skills.items():
            if host == skill_domain or host.endswith("." + skill_domain):
                matches.append(skill)
        return matches

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    async def apply(
        self,
        agent: Agent,
        url: str,
        name: str,
        inputs: dict[str, Any] | None = None,
    ) -> SkillApplicationResult:
        """Dispatch a runnable skill against ``url`` with ``inputs``.

        Raises:
            SkillNotFoundError: no matching skill exists for this URL's
                domain + name.
            SkillNotRunnableError: skill exists but has no Python runner
                (informational-only markdown skill).
            SkillInputError: input validation failed.
        """
        # Resolve which skill: find one matching the URL's domain by name.
        matches = [s for s in self.get_for_url(url) if s.name == name]
        if not matches:
            raise SkillNotFoundError(f"No skill '{name}' for {urlparse(url).hostname or url!r}")
        # If multiple match (different domain entries both match), prefer
        # the most-specific (longest domain) one.
        skill = max(matches, key=lambda s: len(s.domain))
        if not skill.runnable:
            raise SkillNotRunnableError(
                f"Skill '{skill.domain}/{skill.name}' is markdown-only -- "
                f"use get_domain_skill to read its instructions, or supply "
                f"a workspace mode that permits adjacent Python helpers."
            )
        runner = self._runners.get((skill.domain, skill.name))
        if runner is None:
            raise SkillNotRunnableError(
                f"Skill '{skill.domain}/{skill.name}' is marked runnable but "
                f"no runner is registered. This is a bug in the bundled skill."
            )

        validated_inputs = validate_inputs(skill, inputs)
        correlation_id = getattr(agent, "_correlation_id", None) or None

        start = time.perf_counter()
        errors: list[ToolError] = []
        warnings: list[ToolWarning] = []
        try:
            output = await runner(agent, url, validated_inputs)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            return SkillApplicationResult(
                skill_name=skill.name,
                domain=skill.domain,
                url=url,
                inputs=validated_inputs,
                output={},
                succeeded=False,
                errors=[
                    ToolError(
                        code="skill_runner_error",
                        message=f"Skill runner raised: {exc}",
                        severity=ToolSeverity.ERROR,
                    )
                ],
                correlation_id=correlation_id,
                duration_ms=duration_ms,
            )

        duration_ms = (time.perf_counter() - start) * 1000
        # Best-effort output-schema sanity (warn on missing keys, don't fail)
        for expected_key in skill.output_schema:
            if expected_key not in output:
                warnings.append(
                    ToolWarning(
                        code="skill_output_missing_field",
                        message=(
                            f"Output schema declares '{expected_key}' but runner did not return it."
                        ),
                        severity=ToolSeverity.WARNING,
                    )
                )
        return SkillApplicationResult(
            skill_name=skill.name,
            domain=skill.domain,
            url=url,
            inputs=validated_inputs,
            output=output,
            succeeded=True,
            errors=errors,
            warnings=warnings,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
        )


__all__ = [
    "BuiltinSkillRunner",
    "SkillError",
    "SkillInputError",
    "SkillNotFoundError",
    "SkillNotRunnableError",
    "SkillRegistry",
    "parse_skill_file",
    "validate_inputs",
]
