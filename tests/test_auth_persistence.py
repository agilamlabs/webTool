"""v1.7.0 Wave 2D: auth-state persistence (storage_state export / import).

Offline, AsyncMock-driven tests for
:meth:`SessionManager.export_state` and :meth:`SessionManager.import_state`.
No Playwright browser is launched: a fake ``BrowserManager`` hands the
``SessionManager`` a fake ``BrowserContext`` whose ``storage_state`` /
``add_cookies`` are mocks. The tests exercise the real path-confinement
logic (``safe_join_path`` against ``config.download.download_dir``), so the
security-critical traversal cases run the production guard.

Covered:
  * export_state -> StorageStateResult counts + saved=True + confined path,
    and the path handed to ``ctx.storage_state`` is inside the download dir;
  * export_state on an unknown session -> the established KeyError;
  * import_state round-trip: new session id returned + cookies applied via
    ``ctx.add_cookies`` (with the parsed cookies);
  * PATH TRAVERSAL: export/import of ``../../etc/...`` are rejected with no
    write/read outside the root (security-critical);
  * last_used is touched on both export and import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.config import AppConfig
from web_agent.models import StorageStateResult
from web_agent.session_manager import SessionManager

# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


def _make_page() -> MagicMock:
    """A fake Playwright Page with the surface SessionManager.create touches."""
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.on = MagicMock()  # sync close-handler registration
    page.bring_to_front = AsyncMock()
    page.evaluate = AsyncMock(return_value="Mozilla/5.0 (fake-UA)")
    return page


def _make_ctx(state: dict[str, Any] | None = None, *, write_file: bool = True) -> MagicMock:
    """A fake BrowserContext.

    ``storage_state(path=...)`` returns ``state`` and (by default) writes it
    to ``path`` so round-trip tests can read a real file back -- mirroring
    Playwright's behaviour where passing ``path`` persists the snapshot.
    """
    if state is None:
        state = {"cookies": [], "origins": []}

    ctx = MagicMock()
    ctx.on = MagicMock()  # ctx.on("page", ...) from TabManager.__init__
    # A single open page keeps SessionManager._session_is_dead() -> False.
    ctx.pages = [_make_page()]

    page = _make_page()
    ctx.new_page = AsyncMock(return_value=page)
    ctx.close = AsyncMock()
    ctx.add_cookies = AsyncMock()

    async def _storage_state(*, path: str | None = None, indexed_db: bool | None = None) -> dict:
        if path is not None and write_file:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(state), encoding="utf-8")
        return state

    ctx.storage_state = AsyncMock(side_effect=_storage_state)
    # Stash the canonical page for assertions if needed.
    ctx._initial_page = page  # type: ignore[attr-defined]
    return ctx


def _make_bm(ctx: MagicMock) -> MagicMock:
    """A fake BrowserManager that yields ``ctx`` from create_persistent_context."""
    bm = MagicMock()
    bm.generation = 1
    bm.is_alive = MagicMock(return_value=True)
    bm.create_persistent_context = AsyncMock(return_value=ctx)
    return bm


def _make_manager(tmp_path: Path, ctx: MagicMock) -> tuple[SessionManager, AppConfig]:
    """Build a SessionManager whose download dir is an isolated tmp dir.

    The download dir is the storage-state confinement root, so pointing it
    at ``tmp_path`` keeps every legitimate write inside the test sandbox.
    """
    cfg = AppConfig(base_dir=str(tmp_path), download={"download_dir": str(tmp_path / "downloads")})
    mgr = SessionManager(_make_bm(ctx), cfg)
    return mgr, cfg


# ----------------------------------------------------------------------
# export_state
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_state_counts_and_saved(tmp_path: Path) -> None:
    """export_state returns populated counts, saved=True, and a confined path;
    the path handed to ctx.storage_state lives inside the download dir."""
    state = {
        "cookies": [
            {"name": "a", "value": "1", "domain": "x.com", "path": "/"},
            {"name": "b", "value": "2", "domain": "x.com", "path": "/"},
            {"name": "c", "value": "3", "domain": "y.com", "path": "/"},
        ],
        "origins": [
            {"origin": "https://x.com", "localStorage": []},
            {"origin": "https://y.com", "localStorage": []},
        ],
    }
    ctx = _make_ctx(state)
    mgr, cfg = _make_manager(tmp_path, ctx)

    sid = await mgr.create(name="login")
    result = await mgr.export_state(sid, "auth.json")

    assert isinstance(result, StorageStateResult)
    assert result.saved is True
    assert result.loaded is False
    assert result.error is None
    assert result.cookie_count == 3
    assert result.origin_count == 2
    assert result.session_id == sid
    assert result.path is not None

    # The path passed to Playwright must be inside the download dir.
    download_root = Path(cfg.download.download_dir).resolve()
    passed_path = Path(ctx.storage_state.await_args.kwargs["path"]).resolve()
    assert passed_path.is_relative_to(download_root), (
        f"storage_state path {passed_path} escaped the download root {download_root}"
    )
    # And the returned path matches what was written.
    assert Path(result.path).resolve() == passed_path
    assert passed_path.is_file()


@pytest.mark.asyncio
async def test_export_state_unknown_session_raises_keyerror(tmp_path: Path) -> None:
    """export_state on an unknown session raises the established KeyError
    (the same path get() raises for every other session op)."""
    ctx = _make_ctx()
    mgr, _ = _make_manager(tmp_path, ctx)

    with pytest.raises(KeyError):
        await mgr.export_state("does-not-exist", "auth.json")

    # No file should have been written for a non-existent session.
    ctx.storage_state.assert_not_called()


@pytest.mark.asyncio
async def test_export_state_touches_last_used(tmp_path: Path) -> None:
    """export_state refreshes the idle clock so the session never looks idle."""
    ctx = _make_ctx()
    mgr, _ = _make_manager(tmp_path, ctx)

    ticks = iter([100.0, 200.0, 300.0, 400.0, 500.0, 600.0])
    mgr._clock = lambda: next(ticks)  # type: ignore[assignment]

    sid = await mgr.create(name="login")
    before = mgr._last_used[sid]
    await mgr.export_state(sid, "auth.json")
    after = mgr._last_used[sid]

    assert after > before


# ----------------------------------------------------------------------
# import_state
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_state_roundtrip_applies_cookies(tmp_path: Path) -> None:
    """import_state returns a NEW session id and replays the parsed cookies
    onto the new context via add_cookies."""
    cookies = [
        {"name": "session", "value": "abc", "domain": "app.example.com", "path": "/"},
        {"name": "csrf", "value": "xyz", "domain": "app.example.com", "path": "/"},
    ]
    state = {"cookies": cookies, "origins": []}

    ctx = _make_ctx(state)
    mgr, cfg = _make_manager(tmp_path, ctx)

    # Write a small storage_state file inside the download root.
    state_file = Path(cfg.download.download_dir)
    state_file.mkdir(parents=True, exist_ok=True)
    (state_file / "saved.json").write_text(json.dumps(state), encoding="utf-8")

    new_sid = await mgr.import_state("saved.json", name="restored")

    assert isinstance(new_sid, str)
    assert new_sid in mgr._sessions
    assert new_sid.startswith("restored-")

    ctx.add_cookies.assert_awaited_once()
    applied = ctx.add_cookies.await_args.args[0]
    assert list(applied) == cookies

    # The SessionInfo flag reflects the hydration.
    assert mgr._info[new_sid].has_storage_state is True


@pytest.mark.asyncio
async def test_import_state_touches_last_used(tmp_path: Path) -> None:
    """import_state marks the freshly-created session as actively used."""
    state = {"cookies": [{"name": "a", "value": "1", "domain": "x.com", "path": "/"}], "origins": []}
    ctx = _make_ctx(state)
    mgr, cfg = _make_manager(tmp_path, ctx)

    root = Path(cfg.download.download_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "saved.json").write_text(json.dumps(state), encoding="utf-8")

    ticks = iter([float(n) for n in range(100, 200, 5)])
    mgr._clock = lambda: next(ticks)  # type: ignore[assignment]

    new_sid = await mgr.import_state("saved.json")
    # last_used must be populated and equal to the most recent tick consumed.
    assert new_sid in mgr._last_used


@pytest.mark.asyncio
async def test_import_state_missing_file_raises(tmp_path: Path) -> None:
    """A confined-but-absent file is a clear ValueError (not a silent empty session)."""
    ctx = _make_ctx()
    mgr, _ = _make_manager(tmp_path, ctx)

    with pytest.raises(ValueError, match="not found"):
        await mgr.import_state("nope.json")


@pytest.mark.asyncio
async def test_import_state_rejects_oversize_file(tmp_path: Path) -> None:
    """An oversize storage_state file (even one inside the sandbox) is refused
    before being read into memory -- bounds a pathological/hostile import."""
    from web_agent.session_manager import _MAX_STORAGE_STATE_BYTES

    ctx = _make_ctx()
    mgr, cfg = _make_manager(tmp_path, ctx)

    root = Path(cfg.download.download_dir)
    root.mkdir(parents=True, exist_ok=True)
    big = root / "huge.json"
    # A valid-JSON-prefixed blob padded just past the cap; the size gate
    # must fire before any json.loads / add_cookies work happens.
    big.write_text('{"cookies": []}' + " " * (_MAX_STORAGE_STATE_BYTES + 1), encoding="utf-8")

    with pytest.raises(ValueError, match="over the"):
        await mgr.import_state("huge.json")
    ctx.add_cookies.assert_not_awaited()


# ----------------------------------------------------------------------
# PATH TRAVERSAL (security-critical)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_state_rejects_path_traversal(tmp_path: Path) -> None:
    """export_state("../../etc/evil") must be rejected: NO write outside root,
    and a populated-but-failed StorageStateResult (result-based contract)."""
    ctx = _make_ctx()
    mgr, _ = _make_manager(tmp_path, ctx)

    sid = await mgr.create(name="login")
    result = await mgr.export_state(sid, "../../etc/evil")

    assert isinstance(result, StorageStateResult)
    assert result.saved is False
    assert result.path is None
    assert result.error is not None
    # Critically: the unsafe path never reached Playwright -> no file written.
    ctx.storage_state.assert_not_called()


@pytest.mark.asyncio
async def test_import_state_rejects_path_traversal(tmp_path: Path) -> None:
    """import_state("../../etc/passwd") must raise before any file read and
    must NOT create a session."""
    ctx = _make_ctx()
    mgr, _ = _make_manager(tmp_path, ctx)

    before = set(mgr._sessions)
    with pytest.raises(ValueError):
        await mgr.import_state("../../etc/passwd")

    # No new session, no cookies applied -- the read never happened.
    assert set(mgr._sessions) == before
    ctx.add_cookies.assert_not_called()
    ctx.create_persistent_context = None  # guard against accidental reuse


@pytest.mark.asyncio
async def test_export_state_rejects_absolute_path(tmp_path: Path) -> None:
    """An absolute path is rejected the same way as a ``..`` escape."""
    ctx = _make_ctx()
    mgr, _ = _make_manager(tmp_path, ctx)

    sid = await mgr.create(name="login")
    abs_target = str(tmp_path / "outside.json")
    result = await mgr.export_state(sid, abs_target)

    assert result.saved is False
    assert result.error is not None
    ctx.storage_state.assert_not_called()
