"""Snapshot normalization, hashing, path-confined storage, and diffing.

The change-monitoring CORE: pure logic plus a path-confined filesystem store.
There is NO Playwright, browser, or network here -- the actual page fetch +
extraction is performed by the caller (the Agent / recipe layer), which then
hands a normalized :class:`~web_agent.models.PageSnapshot` to :class:`SnapshotStore`
for persistence and to :func:`diff_snapshots` for comparison.

Three responsibilities:

1. :func:`normalize_content` -- canonicalize raw extracted text (line endings,
   trailing whitespace, blank-line runs) so a snapshot diff reflects MEANINGFUL
   change, not cosmetic churn. :func:`content_hash` then gives a cheap
   SHA-256 equality check over that normalized form.
2. :class:`SnapshotStore` -- an atomic, path-traversal-confined JSON store keyed
   by a caller-supplied label. A crafted label (``"../../etc/passwd"``) is
   sanitized to a safe name INSIDE the snapshot dir, and the resolved path is
   re-verified against the dir before any write -- it can never escape.
3. :func:`diff_snapshots` -- a pure ``difflib`` diff producing a
   :class:`~web_agent.models.SnapshotDiff` (changed flag, similarity ratio, and
   bounded added/removed line lists with full counts).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from loguru import logger

from .models import PageSnapshot, SnapshotDiff
from .utils import safe_join_path

__all__ = [
    "SnapshotStore",
    "content_hash",
    "diff_snapshots",
    "normalize_content",
]

# Maximum length of a sanitized label, bounding the on-disk filename stem so a
# pathologically long URL/label can't blow past filesystem name limits (~255 on
# most platforms) once the ``.json`` suffix and the dir prefix are added.
_MAX_LABEL_LEN = 120

# Characters preserved verbatim in a sanitized label; everything else (path
# separators, ``..`` sequences, spaces, unicode, control chars) collapses to
# ``_``.
_SAFE_LABEL_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_REPEATED_UNDERSCORE = re.compile(r"_+")


def normalize_content(text: str) -> str:
    """Canonicalize extracted page text for stable change detection.

    Applies, in order:

    1. Unify line endings: ``\\r\\n`` and lone ``\\r`` both become ``\\n``.
    2. Strip trailing whitespace from every line.
    3. Collapse any run of 3+ consecutive blank lines down to a single blank
       line (so cosmetic vertical-whitespace churn does not register as change).
    4. Strip leading and trailing blank lines.

    Case and inner (non-trailing) content are preserved exactly. An empty or
    whitespace-only input returns ``""``.

    Args:
        text: Raw extracted content.

    Returns:
        The normalized string (``""`` for empty/whitespace-only input).
    """
    if not text or not text.strip():
        return ""

    # 1. Unify line endings. Handle CRLF before lone CR so we don't double-split.
    unified = text.replace("\r\n", "\n").replace("\r", "\n")

    # 2. Strip trailing whitespace per line.
    lines = [line.rstrip() for line in unified.split("\n")]

    # 3. Collapse runs of 3+ blank lines to a SINGLE blank line. A "blank" line
    #    is empty after the trailing-whitespace strip above. Runs of 1 or 2
    #    blanks are preserved verbatim; only a run of 3+ is squeezed to one.
    collapsed: list[str] = []
    blank_run = 0

    def _flush_blanks(count: int) -> None:
        # 0 stays 0; 1 or 2 are preserved; 3+ collapses to a single blank.
        emit = count if count < 3 else 1
        collapsed.extend([""] * emit)

    for line in lines:
        if line == "":
            blank_run += 1
        else:
            _flush_blanks(blank_run)
            blank_run = 0
            collapsed.append(line)
    _flush_blanks(blank_run)

    # 4. Strip leading and trailing blank lines.
    start = 0
    end = len(collapsed)
    while start < end and collapsed[start] == "":
        start += 1
    while end > start and collapsed[end - 1] == "":
        end -= 1

    return "\n".join(collapsed[start:end])


def content_hash(text: str) -> str:
    """Return the SHA-256 hex digest of ``text``'s UTF-8 bytes.

    The caller passes already-:func:`normalize_content`-d text so the hash is a
    stable, cheap equality check over the canonical content form.

    Args:
        text: Normalized content.

    Returns:
        64-character lowercase hex SHA-256 digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sanitize_label(label: str) -> str:
    """Reduce a caller label to a safe filename stem.

    Keeps only ``[A-Za-z0-9._-]``; every other character (including ``/``,
    ``\\``, and the characters of any ``..`` sequence) becomes ``_``. Repeated
    underscores collapse, leading dots are stripped (so a label can't produce a
    hidden dotfile or a ``..`` stem), and the result is bounded to
    :data:`_MAX_LABEL_LEN` characters.

    Args:
        label: Caller-supplied snapshot key.

    Returns:
        A non-empty safe filename stem (no extension).

    Raises:
        ValueError: If ``label`` is empty/whitespace, or if sanitization leaves
            nothing usable (e.g. a label of only path separators / dots).
    """
    if not label or not label.strip():
        raise ValueError("Snapshot label must be a non-empty string")

    sanitized = _SAFE_LABEL_CHARS.sub("_", label.strip())
    sanitized = _REPEATED_UNDERSCORE.sub("_", sanitized)
    # Strip leading dots so the stem can never be "", ".", ".." or a dotfile.
    sanitized = sanitized.lstrip(".")
    # Strip leading/trailing underscores introduced by the substitutions for a
    # tidier filename (e.g. "/a/b/" -> "a_b" rather than "_a_b_").
    sanitized = sanitized.strip("_")
    sanitized = sanitized[:_MAX_LABEL_LEN]
    # The length bound can re-expose a trailing separator-derived underscore.
    sanitized = sanitized.strip("_")

    if not sanitized:
        raise ValueError(f"Snapshot label sanitized to empty: {label!r}")
    return sanitized


class SnapshotStore:
    """A path-confined, atomic JSON store for :class:`PageSnapshot` objects.

    Snapshots are persisted one-per-label as ``<dir>/<sanitized-label>.json``.
    Writes are atomic (temp file in the same directory then :func:`os.replace`)
    so a crashed/concurrent write never leaves a half-written, unparseable file
    that :meth:`load` would silently drop. Every label passes through
    :meth:`path_for`, which sanitizes it AND re-verifies the resolved path is
    inside the snapshot dir, so a traversal label can never escape.
    """

    def __init__(self, snapshot_dir: str | Path) -> None:
        """Store ``snapshot_dir`` as a resolved path WITHOUT creating it.

        The directory is created lazily on the first :meth:`save`, so merely
        constructing a store (e.g. to call :meth:`exists` / :meth:`load`) never
        touches the filesystem.

        Args:
            snapshot_dir: Directory under which snapshots are stored.
        """
        self._dir = Path(snapshot_dir).resolve()

    @property
    def snapshot_dir(self) -> Path:
        """The resolved snapshot directory (not necessarily created yet)."""
        return self._dir

    def path_for(self, label: str) -> Path:
        """Return the resolved, confined file path for ``label``.

        Sanitizes the label to a safe filename stem, then routes it through
        :func:`web_agent.utils.safe_join_path`, which resolves the candidate and
        verifies it stays inside the snapshot dir (defense-in-depth on top of
        sanitization). A label that sanitizes to a safe name lands at
        ``<dir>/<stem>.json``; a label that somehow still escaped would raise.

        Args:
            label: Caller-supplied snapshot key.

        Returns:
            The absolute path the snapshot for ``label`` is stored at.

        Raises:
            ValueError: If ``label`` is empty/whitespace, sanitizes to empty, or
                (defensively) resolves outside the snapshot dir.
        """
        stem = _sanitize_label(label)
        # safe_join_path resolves the candidate against the (resolved) dir and
        # raises ValueError if it escapes -- the authoritative confinement gate.
        return safe_join_path(self._dir, f"{stem}.json")

    def exists(self, label: str) -> bool:
        """Return True if a stored snapshot file exists for ``label``.

        Args:
            label: Caller-supplied snapshot key.

        Returns:
            True when the confined path for ``label`` is an existing file.

        Raises:
            ValueError: If ``label`` is empty/whitespace or sanitizes to empty.
        """
        return self.path_for(label).is_file()

    def save(self, snapshot: PageSnapshot, label: str) -> Path:
        """Persist ``snapshot`` under ``label`` atomically; return its path.

        Creates the snapshot dir on first use, writes the model's JSON to a
        temp file in the SAME directory (so :func:`os.replace` is atomic on
        every platform -- a cross-directory rename would not be), then renames
        it into place. A reader therefore only ever sees a complete file.

        Args:
            snapshot: The snapshot to persist.
            label: Caller-supplied snapshot key (sanitized + confined).

        Returns:
            The absolute path the snapshot was written to.

        Raises:
            ValueError: If ``label`` is empty/whitespace or sanitizes to empty.
            OSError: If the directory cannot be created or the file written.
        """
        target = self.path_for(label)
        self._dir.mkdir(parents=True, exist_ok=True)

        payload = snapshot.model_dump_json()
        # Temp file in the same dir guarantees an atomic same-filesystem replace.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=".tmp", dir=str(self._dir)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, target)
        except BaseException:
            # Never leave the temp file behind on any failure (including
            # KeyboardInterrupt). The replace above either succeeded (temp gone)
            # or did not run (temp still present) -- clean it up here.
            tmp_path.unlink(missing_ok=True)
            raise
        return target

    def load(self, label: str) -> Optional[PageSnapshot]:
        """Load the snapshot stored under ``label``, or ``None``.

        Returns ``None`` -- never raises -- when the file is missing OR present
        but unparseable (truncated / corrupt / not valid snapshot JSON), so a
        damaged baseline degrades to "no baseline" (first-snapshot semantics)
        rather than crashing the caller's diff.

        Args:
            label: Caller-supplied snapshot key.

        Returns:
            The parsed :class:`PageSnapshot`, or ``None`` if missing/unparseable.

        Raises:
            ValueError: If ``label`` is empty/whitespace or sanitizes to empty
                (a programming error in the key, distinct from a missing file).
        """
        path = self.path_for(label)
        if not path.is_file():
            return None
        try:
            return PageSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ValueError covers pydantic's ValidationError and JSON decode
            # errors; OSError covers a read race (file vanished/permission).
            logger.debug(
                "SnapshotStore.load: dropping unparseable snapshot {p}: {e}",
                p=path,
                e=exc,
            )
            return None


def _split_lines(text: str) -> list[str]:
    """Split content into lines for line-based diffing (no trailing newlines)."""
    return text.splitlines()


def diff_snapshots(
    old: Optional[PageSnapshot],
    new: PageSnapshot,
    *,
    max_lines: int = 200,
) -> SnapshotDiff:
    """Compare a baseline snapshot against a new capture (pure; no I/O).

    When ``old`` is ``None`` this is the FIRST snapshot for the label: the diff
    reports ``is_first_snapshot=True`` / ``changed=True`` and lists the new
    content as added lines. Otherwise it compares the two normalized contents
    via :mod:`difflib` -- ``changed`` keys off the content-hash equality, and
    ``similarity`` is the ``SequenceMatcher`` ratio of the full texts.

    Added/removed line lists are each capped at ``max_lines`` while
    ``added_count`` / ``removed_count`` report the FULL totals (so a caller can
    tell a 3-line change from a 3000-line one even when the lists are truncated);
    ``truncated`` is set when either list was capped.

    Args:
        old: The stored baseline snapshot, or ``None`` for a first capture.
        new: The freshly captured snapshot to compare.
        max_lines: Per-list cap on ``added_lines`` / ``removed_lines``.

    Returns:
        A :class:`SnapshotDiff` describing the change.
    """
    cap = max(0, max_lines)

    if old is None:
        new_lines = _split_lines(new.content)
        total = len(new_lines)
        capped = new_lines[:cap]
        return SnapshotDiff(
            url=new.url,
            changed=True,
            is_first_snapshot=True,
            similarity=0.0,
            old_hash=None,
            new_hash=new.content_hash,
            old_captured_at=None,
            new_captured_at=new.captured_at,
            added_lines=capped,
            removed_lines=[],
            added_count=total,
            removed_count=0,
            truncated=total > len(capped),
            summary=f"first snapshot ({total} lines captured)",
            correlation_id=new.correlation_id,
        )

    changed = old.content_hash != new.content_hash
    similarity = round(
        difflib.SequenceMatcher(None, old.content, new.content).ratio(), 4
    )

    old_lines = _split_lines(old.content)
    new_lines = _split_lines(new.content)

    # SequenceMatcher opcodes give a precise, order- and duplicate-aware line
    # diff: 'replace'/'delete' tag removed lines, 'replace'/'insert' tag added
    # lines. This is sounder than a set difference (which would lose repeated
    # lines and ordering) and avoids parsing unified_diff's +++/--- headers.
    added: list[str] = []
    removed: list[str] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(old_lines[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(new_lines[j1:j2])

    added_count = len(added)
    removed_count = len(removed)
    added_capped = added[:cap]
    removed_capped = removed[:cap]
    truncated = added_count > len(added_capped) or removed_count > len(removed_capped)

    if changed:
        summary = (
            f"changed: +{added_count} / -{removed_count} lines, "
            f"{similarity * 100:.1f}% similar"
        )
    else:
        summary = "no change"

    return SnapshotDiff(
        url=new.url,
        changed=changed,
        is_first_snapshot=False,
        similarity=similarity,
        old_hash=old.content_hash,
        new_hash=new.content_hash,
        old_captured_at=old.captured_at,
        new_captured_at=new.captured_at,
        added_lines=added_capped,
        removed_lines=removed_capped,
        added_count=added_count,
        removed_count=removed_count,
        truncated=truncated,
        summary=summary,
        correlation_id=new.correlation_id,
    )
