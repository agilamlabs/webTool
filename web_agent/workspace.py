"""v1.6.7: Agent-editable workspace with safety modes.

A "workspace" is a directory the agent reads from and (in some modes)
writes to. Default layout::

    .webtool-workspace/
        domain-skills/      # user-authored markdown skills (auto-loaded)
        notes/              # agent-authored free-text notes
        helpers.py          # Python helpers (gated by mode)

Safety modes (set via ``WorkspaceConfig.mode``):
    * ``read_only`` -- blocks every write.
    * ``markdown_skills_only`` (default) -- allows ``.md`` writes under
      ``domain-skills/``; everything else blocked.
    * ``reviewed_python_helpers`` -- adds ``helpers.py`` write; execution
      requires a second opt-in (``WorkspaceConfig.execute_helpers``).
    * ``unsafe_python_helpers`` -- no restrictions.

Path traversal is blocked unconditionally via ``safe_join_path``
(v1.6.4). Every write hits the audit log when ``audit.enabled`` AND
``WorkspaceConfig.audit_helper_usage`` are both True.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from loguru import logger

from .config import AppConfig
from .exceptions import WebAgentError
from .utils import _is_cross_platform_absolute, safe_join_path

if TYPE_CHECKING:
    from .audit import AuditLogger


class WorkspaceError(WebAgentError):
    """Workspace mode gate blocked a read/write operation."""


# Subdirectories below the workspace root that have special meaning.
SKILLS_DIR = "domain-skills"
NOTES_DIR = "notes"
HELPERS_FILE = "helpers.py"


class Workspace:
    """Mode-gated read/write access to the agent's workspace directory."""

    def __init__(
        self,
        config: AppConfig,
        audit: Optional[AuditLogger] = None,
    ) -> None:
        self._config = config
        self._ws_cfg = config.workspace
        self._audit = audit

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def enabled(self) -> bool:
        return self._ws_cfg.enabled

    def root(self) -> Path:
        """Return the resolved workspace root.

        Always returns a Path (not Optional) so callers can subscribe
        without nullchecks; gate calls behind :meth:`enabled` if you
        care whether the workspace is active.
        """
        base = Path(self._config.base_dir).resolve()
        if _is_cross_platform_absolute(self._ws_cfg.workspace_dir):
            return Path(self._ws_cfg.workspace_dir)
        return safe_join_path(base, self._ws_cfg.workspace_dir)

    def _resolve(self, rel_path: str) -> Path:
        """Resolve ``rel_path`` under the workspace root with traversal protection."""
        return safe_join_path(self.root(), rel_path)

    # ------------------------------------------------------------------
    # Mode gates
    # ------------------------------------------------------------------

    def _check_enabled(self) -> None:
        if not self._ws_cfg.enabled:
            raise WorkspaceError(
                "Workspace is disabled. Set workspace.enabled=True to opt in."
            )

    def _check_write_allowed(self, rel_path: str) -> None:
        """Raise WorkspaceError if the configured mode forbids writing rel_path."""
        self._check_enabled()
        mode = self._ws_cfg.mode
        if mode == "read_only":
            raise WorkspaceError(
                f"Workspace mode is 'read_only'; cannot write {rel_path!r}."
            )

        # markdown_skills_only: must be .md AND under domain-skills/
        if mode == "markdown_skills_only":
            p = Path(rel_path)
            if p.suffix.lower() != ".md":
                raise WorkspaceError(
                    f"Mode 'markdown_skills_only': only .md files allowed "
                    f"(got {p.suffix!r} for {rel_path!r})."
                )
            # First path component must be SKILLS_DIR
            parts = p.parts
            if not parts or parts[0] != SKILLS_DIR:
                raise WorkspaceError(
                    f"Mode 'markdown_skills_only': writes must be under "
                    f"'{SKILLS_DIR}/' (got {rel_path!r})."
                )

        # reviewed_python_helpers: .md anywhere + helpers.py at root
        if mode == "reviewed_python_helpers":
            p = Path(rel_path)
            if p.suffix.lower() == ".md":
                return  # any .md ok
            # Only the EXACT root-level helpers.py qualifies. We must NOT
            # accept e.g. ``subdir/helpers.py`` -- that would let a caller
            # write arbitrary .py anywhere under the workspace as long as
            # the basename matched HELPERS_FILE, defeating the mode's
            # "single reviewed helper file" intent.
            if Path(rel_path) == Path(HELPERS_FILE):
                return  # helpers.py at workspace root ok
            raise WorkspaceError(
                f"Mode 'reviewed_python_helpers': writes limited to .md and "
                f"the root-level '{HELPERS_FILE}' (got {rel_path!r})."
            )

        # unsafe_python_helpers: no restrictions

    # ------------------------------------------------------------------
    # Public ops
    # ------------------------------------------------------------------

    def list_skills(self) -> list[Path]:
        """List ``.md`` files under ``<workspace>/domain-skills/``."""
        if not self._ws_cfg.enabled:
            return []
        skill_root = self.root() / SKILLS_DIR
        if not skill_root.is_dir():
            return []
        return sorted(skill_root.glob("**/*.md"))

    def read_file(self, rel_path: str) -> str:
        """Read a file from the workspace.

        Read access is allowed in every mode (even ``read_only``) -- the
        gate applies to writes only. Path traversal is still blocked.
        """
        self._check_enabled()
        p = self._resolve(rel_path)
        if not p.is_file():
            raise WorkspaceError(f"Workspace file not found: {rel_path!r}")
        return p.read_text(encoding="utf-8")

    def write_file(self, rel_path: str, content: str) -> Path:
        """Write a file into the workspace.

        Traversal protection: ``safe_join_path`` runs first (rejects
        absolute paths + ``..`` escapes), THEN the mode gate decides
        whether the (legal) path is allowed under the current mode.

        Audit-logged when ``audit.enabled`` and
        ``workspace.audit_helper_usage`` are both True.
        """
        self._check_enabled()
        # Path-safety check FIRST -- traversal must be rejected
        # regardless of mode, before the mode-specific gate runs.
        p = self._resolve(rel_path)
        self._check_write_allowed(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self._audit_write(rel_path, len(content))
        return p

    def write_skill(self, name: str, content: str) -> Path:
        """Convenience: write a markdown skill under ``domain-skills/<name>.md``.

        Auto-appends ``.md`` if missing. Slashes in ``name`` are allowed
        (e.g. ``"sec.gov/filing_search"`` -> writes
        ``domain-skills/sec.gov/filing_search.md``).
        """
        if not name.endswith(".md"):
            name = name + ".md"
        return self.write_file(f"{SKILLS_DIR}/{name}", content)

    def write_note(self, name: str, content: str) -> Path:
        """Convenience: write a free-text note under ``notes/<name>``."""
        return self.write_file(f"{NOTES_DIR}/{name}", content)

    def helper_module_path(self) -> Optional[Path]:
        """Return the path to ``helpers.py`` if present and execution allowed.

        Returns None when:
          - workspace disabled, or
          - mode is not one of the python-helper modes, or
          - ``execute_helpers`` is False, or
          - ``helpers.py`` does not exist.
        """
        if not self._ws_cfg.enabled:
            return None
        if self._ws_cfg.mode not in (
            "reviewed_python_helpers",
            "unsafe_python_helpers",
        ):
            return None
        if not self._ws_cfg.execute_helpers:
            return None
        p = self.root() / HELPERS_FILE
        return p if p.is_file() else None

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _audit_write(self, rel_path: str, n_chars: int) -> None:
        """Best-effort write to audit log; never raises."""
        if not self._ws_cfg.audit_helper_usage:
            return
        if self._audit is None or not getattr(self._audit, "enabled", False):
            return
        # Use a lightweight info-level loguru record; the AuditLogger
        # API is "scope() context manager" oriented (not direct write),
        # so we keep this as a structured log entry rather than try to
        # synthesize an audit scope here.
        with contextlib.suppress(Exception):
            logger.info(
                "Workspace write: mode={mode} path={rel} chars={n}",
                mode=self._ws_cfg.mode,
                rel=rel_path,
                n=n_chars,
            )


__all__ = ["HELPERS_FILE", "NOTES_DIR", "SKILLS_DIR", "Workspace", "WorkspaceError"]
