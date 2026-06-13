"""Offline tests for the snapshot/diff change-monitoring core.

Covers ``web_agent.monitoring``: content normalization, hashing, the
path-confined ``SnapshotStore`` (atomic save/load, corrupt-file tolerance, and
the path-traversal guard), and the pure ``diff_snapshots`` function (first
snapshot, unchanged, changed, and truncation).

Pure logic + ``tmp_path`` filesystem only -- no Playwright, browser, or network.
``PageSnapshot`` instances are built directly with ``content`` + ``content_hash``
set via the module's own ``normalize_content`` / ``content_hash`` helpers,
mirroring how the integrator will populate them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_agent.models import PageSnapshot
from web_agent.monitoring import (
    SnapshotStore,
    content_hash,
    diff_snapshots,
    normalize_content,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_snapshot(
    raw: str,
    *,
    url: str = "https://example.com/page",
    label: str | None = None,
    captured_at: str | None = "2026-06-14T12:00:00+00:00",
    correlation_id: str | None = "corr-123",
) -> PageSnapshot:
    """Build a PageSnapshot from RAW content the way the integrator will.

    Normalizes the content and derives the hash so ``content`` / ``content_hash``
    are always mutually consistent.
    """
    normalized = normalize_content(raw)
    return PageSnapshot(
        url=url,
        label=label,
        captured_at=captured_at,
        title="Example",
        content=normalized,
        content_hash=content_hash(normalized),
        content_length=len(normalized),
        extraction_method="trafilatura",
        correlation_id=correlation_id,
    )


# ----------------------------------------------------------------------
# normalize_content
# ----------------------------------------------------------------------


def test_normalize_unifies_crlf_and_cr() -> None:
    assert normalize_content("a\r\nb\rc") == "a\nb\nc"


def test_normalize_strips_trailing_whitespace_per_line() -> None:
    assert normalize_content("a   \nb\t\n  c  ") == "a\nb\n  c"


def test_normalize_preserves_leading_whitespace_and_case() -> None:
    # Inner content and case are untouched; only TRAILING whitespace is stripped.
    assert normalize_content("  Indented Line\nMixedCase") == "  Indented Line\nMixedCase"


def test_normalize_collapses_three_or_more_blank_lines_to_one() -> None:
    # 4 blank lines between a and b -> exactly one blank line.
    assert normalize_content("a\n\n\n\n\nb") == "a\n\nb"


def test_normalize_preserves_single_and_double_blank_runs() -> None:
    # A run of 1 or 2 blanks is NOT collapsed; only 3+ is.
    assert normalize_content("a\n\nb") == "a\n\nb"  # 1 blank line, preserved
    assert normalize_content("a\n\n\nb") == "a\n\n\nb"  # 2 blank lines, preserved
    assert normalize_content("a\n\n\n\nb") == "a\n\nb"  # 3 blank lines -> 1


def test_normalize_strips_leading_and_trailing_blank_lines() -> None:
    assert normalize_content("\n\n  \nhello\nworld\n\n\n") == "hello\nworld"


def test_normalize_empty_and_whitespace_only_become_empty() -> None:
    assert normalize_content("") == ""
    assert normalize_content("   \n\t\n  \r\n") == ""


# ----------------------------------------------------------------------
# content_hash
# ----------------------------------------------------------------------


def test_content_hash_is_deterministic_and_stable() -> None:
    # Known SHA-256 of the UTF-8 bytes of "hello".
    expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert content_hash("hello") == expected
    assert content_hash("hello") == content_hash("hello")


def test_content_hash_identical_content_identical_hash() -> None:
    text = "line one\nline two\n  indented"
    assert content_hash(text) == content_hash(text)


def test_content_hash_differs_when_content_differs() -> None:
    assert content_hash("alpha") != content_hash("beta")
    # Even a one-character change flips the digest.
    assert content_hash("alpha") != content_hash("alphb")


def test_content_hash_is_64_char_hex() -> None:
    digest = content_hash("anything")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


# ----------------------------------------------------------------------
# SnapshotStore: round-trip, missing, corrupt, exists
# ----------------------------------------------------------------------


def test_store_does_not_create_dir_on_construction(tmp_path: Path) -> None:
    target = tmp_path / "snaps"
    SnapshotStore(target)
    assert not target.exists()  # lazily created on first save only


def test_store_save_load_round_trip(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    snap = _make_snapshot("hello\nworld")

    path = store.save(snap, "homepage")
    assert path.is_file()
    assert path.suffix == ".json"

    loaded = store.load("homepage")
    assert loaded is not None
    assert loaded == snap
    assert loaded.content == "hello\nworld"
    assert loaded.content_hash == snap.content_hash


def test_store_save_creates_dir_lazily(tmp_path: Path) -> None:
    snap_dir = tmp_path / "lazy" / "nested"
    store = SnapshotStore(snap_dir)
    assert not snap_dir.exists()
    store.save(_make_snapshot("x"), "k")
    assert snap_dir.is_dir()


def test_store_load_missing_returns_none(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    assert store.load("never-saved") is None


def test_store_load_corrupt_file_returns_none(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    # Write garbage straight to the path the store would use.
    path = store.path_for("broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is { not valid json", encoding="utf-8")

    # Must NOT raise -- a corrupt baseline degrades to "no baseline".
    assert store.load("broken") is None


def test_store_load_valid_json_wrong_schema_returns_none(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    path = store.path_for("wrongschema")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON, but missing the required ``url`` field -> ValidationError.
    path.write_text('{"not": "a snapshot"}', encoding="utf-8")
    assert store.load("wrongschema") is None


def test_store_exists_reflects_save(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    assert store.exists("k") is False
    store.save(_make_snapshot("x"), "k")
    assert store.exists("k") is True


def test_store_overwrite_replaces_previous(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    store.save(_make_snapshot("first"), "k")
    store.save(_make_snapshot("second"), "k")
    loaded = store.load("k")
    assert loaded is not None
    assert loaded.content == "second"


def test_store_save_leaves_no_temp_files(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snaps"
    store = SnapshotStore(snap_dir)
    store.save(_make_snapshot("x"), "clean")
    # Only the final .json should remain -- no leftover .tmp scratch files.
    entries = list(snap_dir.iterdir())
    assert len(entries) == 1
    assert entries[0].suffix == ".json"


# ----------------------------------------------------------------------
# SnapshotStore: path-traversal guard + label sanitization
# ----------------------------------------------------------------------


def _assert_inside(path: Path, base: Path) -> None:
    resolved = path.resolve()
    base_resolved = base.resolve()
    # Raises ValueError if ``resolved`` is not within ``base_resolved``.
    resolved.relative_to(base_resolved)


def test_store_traversal_label_stays_confined(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snaps"
    store = SnapshotStore(snap_dir)
    snap = _make_snapshot("confined")

    # A classic traversal label must EITHER raise ValueError OR be sanitized to
    # a safe name inside the snapshot dir -- never escape.
    try:
        path = store.save(snap, "../../etc/passwd")
    except ValueError:
        pass  # acceptable: rejected outright
    else:
        _assert_inside(path, snap_dir)
        # The literal escape target must not have been created.
        assert not (tmp_path.parent / "etc" / "passwd").exists()
        # And the file must be loadable back through the same (sanitized) label.
        assert store.load("../../etc/passwd") == snap


def test_store_slashed_label_confined(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snaps"
    store = SnapshotStore(snap_dir)
    path = store.save(_make_snapshot("y"), "a/b/c")
    _assert_inside(path, snap_dir)
    # No nested a/b/ directory tree was created -- separators were sanitized.
    assert not (snap_dir / "a").exists()


def test_store_backslash_label_confined(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snaps"
    store = SnapshotStore(snap_dir)
    path = store.save(_make_snapshot("y"), "..\\..\\windows\\system32")
    _assert_inside(path, snap_dir)


def test_store_empty_label_raises(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    with pytest.raises(ValueError):
        store.path_for("")
    with pytest.raises(ValueError):
        store.save(_make_snapshot("x"), "   ")


def test_store_dot_only_label_raises(tmp_path: Path) -> None:
    # A label that sanitizes away to nothing (only dots/separators) must raise,
    # not silently produce a hidden/empty filename.
    store = SnapshotStore(tmp_path / "snaps")
    with pytest.raises(ValueError):
        store.path_for("..")
    with pytest.raises(ValueError):
        store.path_for("/")


def test_store_label_sanitization_keeps_safe_chars(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snaps"
    store = SnapshotStore(snap_dir)
    path = store.path_for("My Page_v1.2-final")
    _assert_inside(path, snap_dir)
    # Space -> underscore; safe chars (._-) preserved.
    assert path.name == "My_Page_v1.2-final.json"


def test_store_distinct_safe_labels_distinct_files(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    p1 = store.path_for("alpha")
    p2 = store.path_for("beta")
    assert p1 != p2


# ----------------------------------------------------------------------
# diff_snapshots: first snapshot
# ----------------------------------------------------------------------


def test_diff_first_snapshot() -> None:
    new = _make_snapshot("line a\nline b\nline c")
    diff = diff_snapshots(None, new)

    assert diff.is_first_snapshot is True
    assert diff.changed is True
    assert diff.similarity == 0.0
    assert diff.old_hash is None
    assert diff.new_hash == new.content_hash
    assert diff.old_captured_at is None
    assert diff.new_captured_at == new.captured_at
    assert diff.added_lines == ["line a", "line b", "line c"]
    assert diff.removed_lines == []
    assert diff.added_count == 3
    assert diff.removed_count == 0
    assert diff.truncated is False
    assert "first snapshot" in diff.summary
    assert "3 lines" in diff.summary
    assert diff.correlation_id == new.correlation_id


def test_diff_first_snapshot_truncates() -> None:
    raw = "\n".join(f"row {i}" for i in range(10))
    new = _make_snapshot(raw)
    diff = diff_snapshots(None, new, max_lines=4)

    assert diff.is_first_snapshot is True
    assert len(diff.added_lines) == 4
    assert diff.added_count == 10  # full total, not the capped length
    assert diff.truncated is True


# ----------------------------------------------------------------------
# diff_snapshots: unchanged
# ----------------------------------------------------------------------


def test_diff_unchanged() -> None:
    old = _make_snapshot("same\ncontent", captured_at="2026-06-13T00:00:00+00:00")
    new = _make_snapshot("same\ncontent", captured_at="2026-06-14T00:00:00+00:00")
    diff = diff_snapshots(old, new)

    assert diff.changed is False
    assert diff.is_first_snapshot is False
    assert diff.similarity == 1.0
    assert diff.added_lines == []
    assert diff.removed_lines == []
    assert diff.added_count == 0
    assert diff.removed_count == 0
    assert diff.truncated is False
    assert diff.summary == "no change"
    assert diff.old_hash == old.content_hash
    assert diff.new_hash == new.content_hash
    # Hash equal but timestamps still carried through from each snapshot.
    assert diff.old_captured_at == "2026-06-13T00:00:00+00:00"
    assert diff.new_captured_at == "2026-06-14T00:00:00+00:00"


# ----------------------------------------------------------------------
# diff_snapshots: changed
# ----------------------------------------------------------------------


def test_diff_changed_reports_added_and_removed() -> None:
    old = _make_snapshot("alpha\nbeta\ngamma")
    new = _make_snapshot("alpha\ndelta\ngamma\nepsilon")
    diff = diff_snapshots(old, new)

    assert diff.changed is True
    assert diff.is_first_snapshot is False
    # 'beta' removed; 'delta' and 'epsilon' added; 'alpha'/'gamma' unchanged.
    assert "beta" in diff.removed_lines
    assert "delta" in diff.added_lines
    assert "epsilon" in diff.added_lines
    assert "alpha" not in diff.added_lines
    assert "alpha" not in diff.removed_lines
    assert diff.added_count == 2
    assert diff.removed_count == 1
    assert 0.0 < diff.similarity < 1.0
    assert diff.truncated is False
    assert diff.summary.startswith("changed: +2 / -1 lines")
    assert "% similar" in diff.summary


def test_diff_changed_similarity_strictly_between_zero_and_one() -> None:
    old = _make_snapshot("the quick brown fox jumps")
    new = _make_snapshot("the quick red fox leaps")
    diff = diff_snapshots(old, new)
    assert 0.0 < diff.similarity < 1.0


def test_diff_changed_preserves_duplicate_lines() -> None:
    # A set-difference approach would lose the second 'dup'; the opcode-based
    # diff must surface both removals.
    old = _make_snapshot("dup\ndup\nkeep")
    new = _make_snapshot("keep")
    diff = diff_snapshots(old, new)
    assert diff.removed_count == 2
    assert diff.removed_lines.count("dup") == 2


# ----------------------------------------------------------------------
# diff_snapshots: truncation on a changed diff
# ----------------------------------------------------------------------


def test_diff_changed_truncates_lists_but_keeps_full_counts() -> None:
    # Baseline empty; new content adds 250 lines. With max_lines=200 the
    # added_lines list caps at 200 while added_count stays 250.
    old = _make_snapshot("")
    new = _make_snapshot("\n".join(f"item {i}" for i in range(250)))
    diff = diff_snapshots(old, new, max_lines=200)

    assert diff.changed is True
    assert len(diff.added_lines) == 200
    assert diff.added_count == 250
    assert diff.truncated is True
    # Counts in the summary reflect the FULL totals, not the capped list length.
    assert "+250" in diff.summary


def test_diff_removed_truncates_independently() -> None:
    old = _make_snapshot("\n".join(f"old {i}" for i in range(300)))
    new = _make_snapshot("only this")
    diff = diff_snapshots(old, new, max_lines=50)

    assert diff.changed is True
    assert len(diff.removed_lines) == 50
    assert diff.removed_count == 300
    assert diff.truncated is True


def test_diff_default_max_lines_is_200() -> None:
    old = _make_snapshot("")
    new = _make_snapshot("\n".join(f"x{i}" for i in range(205)))
    diff = diff_snapshots(old, new)  # default max_lines
    assert len(diff.added_lines) == 200
    assert diff.added_count == 205
    assert diff.truncated is True
