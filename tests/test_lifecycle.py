"""v1.7.0 production-lifecycle hardening tests.

Covers the Wave 1C surface, all offline (AsyncMock / MagicMock / fake
clocks -- no real Chromium launch):

* stealth refactor: per-context ``_apply_stealth`` gating + the
  ``_stealth_launch_args`` CLI-arg mirror that replaced the
  ``Stealth.use_async`` hooked launch;
* crash recovery: ``disconnected`` -> ``_crashed`` flag, ``is_alive``,
  and ``ensure_running`` bounded auto-relaunch;
* session hygiene: idle reaper (``_idle_expired`` / ``_reap_idle_sync``),
  the ``session_max_count`` cap, and ``_session_is_dead`` liveness;
* profile sweep: orphaned ephemeral ``run-*`` dir removal;
* doctor: the quick-mode Chromium-executable probe that used to
  false-"healthy" when ``playwright install chromium`` was never run.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent import doctor
from web_agent.browser_manager import BrowserManager
from web_agent.config import AppConfig, BrowserConfig
from web_agent.exceptions import BrowserError
from web_agent.models import SessionInfo
from web_agent.session_manager import SessionManager


def _bm(**browser_overrides) -> BrowserManager:
    return BrowserManager(AppConfig(browser=BrowserConfig(**browser_overrides)))


# ----------------------------------------------------------------------
# Stealth refactor: per-context application + launch-arg mirror
# ----------------------------------------------------------------------


class TestStealthRefactor:
    @pytest.mark.asyncio
    async def test_apply_stealth_calls_apply_when_enabled(self) -> None:
        bm = _bm(stealth_enabled=True)
        bm._stealth = MagicMock()
        bm._stealth.apply_stealth_async = AsyncMock()
        ctx = MagicMock()

        await bm._apply_stealth(ctx)

        bm._stealth.apply_stealth_async.assert_awaited_once_with(ctx)

    @pytest.mark.asyncio
    async def test_apply_stealth_skipped_when_disabled(self) -> None:
        bm = _bm(stealth_enabled=False)
        bm._stealth = MagicMock()
        bm._stealth.apply_stealth_async = AsyncMock()

        await bm._apply_stealth(MagicMock())

        bm._stealth.apply_stealth_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_stealth_swallows_errors(self) -> None:
        """A stealth-application failure must degrade to a working context,
        not propagate (the context still functions sans evasions)."""
        bm = _bm(stealth_enabled=True)
        bm._stealth = MagicMock()
        bm._stealth.apply_stealth_async = AsyncMock(side_effect=RuntimeError("boom"))

        # Must not raise.
        await bm._apply_stealth(MagicMock())

    def test_stealth_launch_args_adds_cli_evasions(self) -> None:
        bm = _bm(stealth_enabled=True)
        bm._stealth = SimpleNamespace(
            init_scripts_only=False,
            navigator_webdriver=True,
            navigator_languages=True,
            navigator_languages_override=("en-US", "en"),
        )
        out = bm._stealth_launch_args(["--disable-gpu"])
        assert "--disable-gpu" in out
        assert "--disable-blink-features=AutomationControlled" in out
        assert "--accept-lang=en-US,en" in out

    def test_stealth_launch_args_respects_init_scripts_only(self) -> None:
        bm = _bm(stealth_enabled=True)
        bm._stealth = SimpleNamespace(
            init_scripts_only=True,
            navigator_webdriver=True,
            navigator_languages=True,
            navigator_languages_override=("en-US", "en"),
        )
        base = ["--disable-gpu"]
        assert bm._stealth_launch_args(base) == base

    def test_stealth_launch_args_no_blink_flag_when_webdriver_off(self) -> None:
        bm = _bm(stealth_enabled=True)
        bm._stealth = SimpleNamespace(
            init_scripts_only=False,
            navigator_webdriver=False,
            navigator_languages=False,
            navigator_languages_override=("en-US", "en"),
        )
        out = bm._stealth_launch_args(["--disable-gpu"])
        assert all("--disable-blink-features" not in a for a in out)
        assert all(not a.startswith("--accept-lang") for a in out)


# ----------------------------------------------------------------------
# Crash recovery
# ----------------------------------------------------------------------


class TestCrashRecovery:
    def test_disconnect_flags_crashed_when_live(self) -> None:
        bm = _bm()
        bm._started = True
        bm._stopping = False
        bm._crashed = False

        bm._on_browser_disconnected()

        assert bm._crashed is True

    def test_disconnect_noop_during_intentional_stop(self) -> None:
        bm = _bm()
        bm._started = True
        bm._stopping = True
        bm._crashed = False

        bm._on_browser_disconnected()

        assert bm._crashed is False

    def test_disconnect_noop_before_start(self) -> None:
        bm = _bm()
        bm._started = False
        bm._crashed = False

        bm._on_browser_disconnected()

        assert bm._crashed is False

    def test_persistent_context_close_flags_crashed(self) -> None:
        bm = _bm()
        bm._started = True
        bm._stopping = False
        bm._crashed = False

        bm._on_persistent_context_closed()

        assert bm._crashed is True

    def test_is_alive_false_when_crashed(self) -> None:
        bm = _bm()
        bm._started = True
        bm._crashed = True
        assert bm.is_alive() is False

    def test_is_alive_true_when_browser_connected(self) -> None:
        bm = _bm()
        bm._started = True
        bm._crashed = False
        bm._browser = MagicMock()
        bm._browser.is_connected = MagicMock(return_value=True)
        assert bm.is_alive() is True

    def test_is_alive_false_before_start(self) -> None:
        bm = _bm()
        assert bm.is_alive() is False

    @pytest.mark.asyncio
    async def test_ensure_running_relaunches_after_crash(self) -> None:
        bm = _bm(auto_relaunch=True)
        bm._started = True
        bm._crashed = True
        bm.stop = AsyncMock()
        bm.start = AsyncMock()

        await bm.ensure_running()

        bm.start.assert_awaited_once()
        bm.stop.assert_awaited()

    @pytest.mark.asyncio
    async def test_ensure_running_noop_when_alive(self) -> None:
        bm = _bm(auto_relaunch=True)
        bm._started = True
        bm._crashed = False
        bm._browser = MagicMock()
        bm._browser.is_connected = MagicMock(return_value=True)
        bm.start = AsyncMock()

        await bm.ensure_running()

        bm.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ensure_running_raises_when_relaunch_disabled(self) -> None:
        bm = _bm(auto_relaunch=False)
        bm._started = True
        bm._crashed = True
        bm.start = AsyncMock()

        with pytest.raises(BrowserError, match="auto_relaunch"):
            await bm.ensure_running()
        bm.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ensure_running_raises_after_exhausting_attempts(self) -> None:
        bm = _bm(auto_relaunch=True, relaunch_max_attempts=2, relaunch_backoff_base_s=0.1)
        bm._started = True
        bm._crashed = True
        bm.stop = AsyncMock()
        bm.start = AsyncMock(side_effect=RuntimeError("launch keeps failing"))

        with pytest.raises(BrowserError, match="relaunch attempt"):
            await bm.ensure_running()
        assert bm.start.await_count == 2


# ----------------------------------------------------------------------
# Session hygiene: idle reaper + cap + liveness
# ----------------------------------------------------------------------


def _sm(bm, **browser_overrides) -> SessionManager:
    return SessionManager(bm, AppConfig(browser=BrowserConfig(**browser_overrides)))


class TestSessionHygiene:
    def test_idle_expired_selects_only_stale_sessions(self) -> None:
        sm = _sm(MagicMock(), session_idle_ttl_s=10.0)
        now = [105.0]
        sm._clock = lambda: now[0]
        sm._last_used = {"stale": 50.0, "fresh": 100.0}

        assert sm._idle_expired() == ["stale"]

    def test_idle_expired_disabled_when_ttl_zero(self) -> None:
        sm = _sm(MagicMock(), session_idle_ttl_s=0.0)
        sm._clock = lambda: 10_000.0
        sm._last_used = {"ancient": 1.0}

        assert sm._idle_expired() == []

    def test_reap_idle_sync_parks_context_and_drops_registries(self) -> None:
        sm = _sm(MagicMock(), session_idle_ttl_s=10.0)
        now = [100.0]
        sm._clock = lambda: now[0]
        ctx = MagicMock()
        sm._sessions["s1"] = ctx
        sm._info["s1"] = SessionInfo(session_id="s1")
        sm._tabs["s1"] = MagicMock()
        sm._last_used["s1"] = 100.0
        sm._session_generation["s1"] = 0

        now[0] = 200.0  # 100s idle > 10s ttl
        sm._reap_idle_sync()

        assert "s1" not in sm._sessions
        assert "s1" not in sm._info
        assert "s1" not in sm._last_used
        assert ctx in sm._pending_close

    def test_reap_idle_sync_keeps_fresh_session(self) -> None:
        sm = _sm(MagicMock(), session_idle_ttl_s=10_000.0)
        now = [100.0]
        sm._clock = lambda: now[0]
        sm._sessions["s1"] = MagicMock()
        sm._last_used["s1"] = 100.0

        now[0] = 105.0  # 5s idle, well under ttl
        sm._reap_idle_sync()

        assert "s1" in sm._sessions

    @pytest.mark.asyncio
    async def test_create_enforces_session_cap(self) -> None:
        bm = MagicMock()
        bm.create_persistent_context = AsyncMock()
        sm = _sm(bm, session_max_count=1)
        sm._sessions["already-here"] = MagicMock()  # at cap

        with pytest.raises(BrowserError, match="Session limit reached"):
            await sm.create()
        # Cap is enforced before a new context is built.
        bm.create_persistent_context.assert_not_awaited()

    def test_session_is_dead_when_browser_not_alive(self) -> None:
        bm = SimpleNamespace(is_alive=lambda: False, generation=0)
        sm = SessionManager(bm, AppConfig())
        ctx = MagicMock()
        ctx.pages = []
        assert sm._session_is_dead("s", ctx) is True

    def test_session_is_dead_on_generation_mismatch(self) -> None:
        bm = SimpleNamespace(is_alive=lambda: True, generation=5)
        sm = SessionManager(bm, AppConfig())
        sm._session_generation["s"] = 3  # created on an earlier browser
        ctx = MagicMock()
        ctx.pages = []
        assert sm._session_is_dead("s", ctx) is True

    def test_session_is_dead_when_all_pages_closed(self) -> None:
        bm = SimpleNamespace(is_alive=lambda: True, generation=5)
        sm = SessionManager(bm, AppConfig())
        sm._session_generation["s"] = 5
        page = MagicMock()
        page.is_closed = MagicMock(return_value=True)
        ctx = MagicMock()
        ctx.pages = [page]
        assert sm._session_is_dead("s", ctx) is True

    def test_session_alive_when_browser_live_and_page_open(self) -> None:
        bm = SimpleNamespace(is_alive=lambda: True, generation=5)
        sm = SessionManager(bm, AppConfig())
        sm._session_generation["s"] = 5
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        ctx = MagicMock()
        ctx.pages = [page]
        assert sm._session_is_dead("s", ctx) is False


# ----------------------------------------------------------------------
# Orphan ephemeral-profile sweep
# ----------------------------------------------------------------------


def _profiles_root(tmp_path: Path) -> Path:
    root = tmp_path / ".webtool" / "browser-profiles"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_profile(root: Path, name: str, *, age_h: float) -> Path:
    import os
    import time

    d = root / name
    d.mkdir()
    (d / "Default").mkdir()
    old = time.time() - age_h * 3600.0
    os.utime(d, (old, old))
    os.utime(d / "Default", (old, old))
    return d


class TestProfileSweep:
    @pytest.mark.asyncio
    async def test_sweep_removes_old_orphan_keeps_live(self, tmp_path: Path) -> None:
        root = _profiles_root(tmp_path)
        old = _make_profile(root, "run-old", age_h=48.0)
        live = _make_profile(root, "run-live", age_h=48.0)

        bm = BrowserManager(
            AppConfig(base_dir=str(tmp_path), browser=BrowserConfig(profile_sweep_max_age_h=24.0))
        )
        bm._effective_profile_dir = live  # the live profile must be skipped

        await bm._sweep_orphan_profiles()

        assert not old.exists(), "stale orphan should be swept"
        assert live.exists(), "live profile must never be swept"

    @pytest.mark.asyncio
    async def test_sweep_disabled_is_noop(self, tmp_path: Path) -> None:
        root = _profiles_root(tmp_path)
        old = _make_profile(root, "run-old", age_h=999.0)

        bm = BrowserManager(
            AppConfig(base_dir=str(tmp_path), browser=BrowserConfig(profile_sweep_max_age_h=0.0))
        )
        await bm._sweep_orphan_profiles()

        assert old.exists(), "sweep disabled (max_age_h=0) must not remove anything"

    @pytest.mark.asyncio
    async def test_sweep_ignores_non_run_dirs(self, tmp_path: Path) -> None:
        root = _profiles_root(tmp_path)
        foreign = _make_profile(root, "named-profile", age_h=999.0)

        bm = BrowserManager(
            AppConfig(base_dir=str(tmp_path), browser=BrowserConfig(profile_sweep_max_age_h=24.0))
        )
        await bm._sweep_orphan_profiles()

        assert foreign.exists(), "only run-* ephemeral dirs are sweep candidates"


# ----------------------------------------------------------------------
# Doctor: the quick-mode Chromium-executable probe
# ----------------------------------------------------------------------


class TestDoctorChromiumProbe:
    @pytest.mark.asyncio
    async def test_fails_with_hint_when_unresolvable(self, monkeypatch) -> None:
        async def _boom() -> Path:
            raise RuntimeError("driver missing")

        monkeypatch.setattr(doctor, "_resolve_chromium_executable", _boom)
        check = await doctor._check_chromium_installed(AppConfig())

        assert check.status == "fail"
        assert "playwright install chromium" in check.message

    @pytest.mark.asyncio
    async def test_fails_when_executable_missing(self, monkeypatch, tmp_path: Path) -> None:
        missing = tmp_path / "chrome-linux" / "chrome"

        async def _resolve() -> Path:
            return missing

        monkeypatch.setattr(doctor, "_resolve_chromium_executable", _resolve)
        check = await doctor._check_chromium_installed(AppConfig())

        assert check.status == "fail"
        assert "playwright install chromium" in check.message

    @pytest.mark.asyncio
    async def test_ok_when_executable_present(self, monkeypatch, tmp_path: Path) -> None:
        exe = tmp_path / "chrome"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")

        async def _resolve() -> Path:
            return exe

        monkeypatch.setattr(doctor, "_resolve_chromium_executable", _resolve)
        check = await doctor._check_chromium_installed(AppConfig())

        assert check.status == "ok"
