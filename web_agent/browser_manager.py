"""Browser lifecycle management with stealth, user-agent rotation, and semaphore-bounded context pool."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from playwright_stealth import Stealth

from .config import AppConfig
from .metrics import MetricsRegistry, get_metrics
from .ownership import OwnershipToken
from .utils import get_random_user_agent, locale_os_family, safe_join_path

if TYPE_CHECKING:  # pragma: no cover -- avoid runtime import cycle
    from .network_collector import NetworkCollector


def _resolve_user_agent(bcfg: Any) -> str | None:
    """v1.6.9: pick a UA string per ``BrowserConfig.user_agent_mode``.

    Returns ``None`` for ``"playwright_default"`` so Playwright picks
    its bundled UA; the random pool runner otherwise; the explicit
    string when mode is ``"explicit"`` (validator guarantees the
    string is set).

    v1.7.0 (Wave 2F) fingerprint coherence: when ``coherent_fingerprint``
    is on AND the mode is ``"random"``, the rotated UA is pinned to the OS
    family implied by ``locale`` (via :func:`locale_os_family`). This is
    the coherence GUARANTEE -- the launched context never advertises a UA
    whose OS contradicts the locale/platform of the same context. With
    coherence off (or an unmappable locale) the full cross-OS pool is
    used, restoring pre-v1.7.0 rotation. ``"explicit"`` /
    ``"playwright_default"`` are untouched: the UA is operator-pinned.

    ``bcfg`` is typed as ``Any`` because importing ``BrowserConfig``
    here would create a cycle (config.py -> browser_manager.py -> ...).
    """
    mode = getattr(bcfg, "user_agent_mode", "random")
    if mode == "explicit":
        ua = bcfg.user_agent
        return ua if isinstance(ua, str) else None
    if mode == "playwright_default":
        return None
    os_family: str | None = None
    if getattr(bcfg, "coherent_fingerprint", True):
        os_family = locale_os_family(getattr(bcfg, "locale", "en-US"))
    return get_random_user_agent(os_family)


def _should_disable_chromium_sandbox(cfg_value: Optional[bool]) -> bool:
    """v1.6.9: decide whether to pass ``--no-sandbox`` to Chromium.

    Behaviour:
      * ``cfg_value=True``  -> always disable (pass --no-sandbox).
      * ``cfg_value=False`` -> never disable (keep Chromium sandbox).
      * ``cfg_value=None``  -> auto-detect:
          - CI env var ``CI`` truthy (``true``/``1``/``yes``) -> disable
          - CI env var ``GITHUB_ACTIONS`` truthy -> disable
          - Container marker ``/.dockerenv`` present -> disable
          - otherwise -> keep sandbox

    Local-dev default (sandbox kept) is a deliberate hardening change in
    v1.6.9: prior versions always passed ``--no-sandbox`` which weakened
    Chromium's per-tab isolation against renderer exploits.
    """
    if cfg_value is not None:
        return cfg_value
    if os.environ.get("CI", "").strip().lower() in {"true", "1", "yes"}:
        return True
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() in {"true", "1"}:
        return True
    with suppress(OSError):
        if Path("/.dockerenv").exists():
            return True
    return False


class _NoCloseContextProxy:
    """v1.6.9: forwarding wrapper around a persistent BrowserContext
    that turns ``close()`` into a no-op.

    Why this exists
    ---------------
    Named persistent profiles use ``chromium.launch_persistent_context``
    which returns a single shared ``BrowserContext``. The rest of the
    codebase (``WebFetcher``, ``BrowserActions``, ``SessionManager``)
    assumes the contexts returned by ``BrowserManager._build_context``
    are caller-owned and calls ``await ctx.close()`` on cleanup. Closing
    the persistent context would terminate every other session sharing
    it, defeating the whole point of the persistent profile.

    This proxy forwards every attribute access to the real context
    (``__getattr__``) and overrides ``close()`` to do nothing. The
    persistent context is closed exactly once, from ``BrowserManager.stop()``.

    Identity / equality
    -------------------
    All proxies wrapping the same persistent context compare equal via
    the underlying context, so caller-side dicts keyed by context
    behave as if they always saw one context.
    """

    # v1.6.16 BR-2: include __weakref__ so the proxy can be used as a
    # WeakKeyDictionary key (a bare __slots__ class is unweakreferenceable).
    __slots__ = ("__weakref__", "_ctx")

    def __init__(self, ctx: BrowserContext) -> None:
        object.__setattr__(self, "_ctx", ctx)

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only called when normal attribute lookup fails,
        # which is the case for the wrapped context's attributes.
        return getattr(self._ctx, name)

    async def close(self, reason: Any = None) -> None:
        """No-op: the persistent context is closed once, from BrowserManager.stop()."""
        logger.debug("_NoCloseContextProxy.close() no-op (persistent profile)")

    # v1.6.9 review (C-2): Python looks up dunders on the class, not the
    # instance, so __getattr__ does NOT forward __aenter__ / __aexit__.
    # A caller doing ``async with ctx:`` against this proxy would get an
    # ``AttributeError: object does not support the asynchronous context
    # manager protocol``. Define them explicitly so the proxy honors the
    # BrowserContext async-CM protocol -- __aexit__ stays no-op (mirrors
    # close()) since the persistent context is owned by BrowserManager.
    async def __aenter__(self) -> _NoCloseContextProxy:
        return self

    async def __aexit__(
        self,
        exc_type: Any = None,
        exc: Any = None,
        tb: Any = None,
    ) -> None:
        # Mirrors close(): no-op. Closing happens once from BrowserManager.stop().
        return None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _NoCloseContextProxy):
            return self._ctx is other._ctx
        return self._ctx is other

    def __hash__(self) -> int:
        return id(self._ctx)


class BrowserManager:
    """Manages a single Chromium browser instance with stealth anti-detection
    and a semaphore-bounded pool of browser contexts."""

    def __init__(
        self,
        config: AppConfig,
        network_collector: Optional[NetworkCollector] = None,
        metrics: Optional[MetricsRegistry] = None,
    ) -> None:
        self._config = config
        # v1.7.0 (Wave 4A): observability registry. Defaults to a shared
        # no-op registry when the Agent doesn't pass one, so the launch /
        # crash / relaunch increments cost nothing and existing call sites
        # are unaffected.
        self._metrics = get_metrics(metrics)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        # v1.7.0: stealth is applied explicitly per-context via
        # ``Stealth.apply_stealth_async`` (see ``_apply_stealth``) instead
        # of routing the whole Playwright lifecycle through
        # ``Stealth.use_async``'s hooked launch wrapper. The hook offered
        # no control over launch dispatch and its CLI-arg patching is
        # mirrored by ``_stealth_launch_args``.
        self._stealth = Stealth()
        self._semaphore = asyncio.Semaphore(config.browser.max_contexts)
        self._started = False
        # Raw async_playwright() context manager (v1.7.0: no longer the
        # Stealth.use_async wrapper). Kept as Any to avoid depending on
        # the private PlaywrightContextManager type.
        self._pw_cm: Any = None
        # Serialize start/stop to prevent the "two concurrent start()" race
        # that would launch two browsers and leak the first.
        self._lifecycle_lock = asyncio.Lock()
        # v1.6.6 isolation/CDP state -- resolved lazily in start() so a
        # failed config never leaks tempdirs.
        self._effective_profile_dir: Path | None = None
        # True iff start() created the profile dir (ephemeral tempdir);
        # named profiles are user-owned so cleanup-on-exit is a no-op for them.
        self._owned_profile_dir: bool = False
        self._cdp_endpoint: str | None = None
        self._cdp_port_resolved: int | None = None
        # v1.6.8: shared NetworkCollector attached to every Page opened
        # via new_page() (the ephemeral path). Sessions get the same
        # collector via TabManager. None when the Agent didn't pass one
        # in (older test scaffolding).
        self._network_collector = network_collector
        # v1.6.8: True after a successful connect_over_cdp -- changes
        # stop() to disconnect-without-killing-process.
        self._is_remote_cdp: bool = False
        # v1.6.9: ownership token issued into the active profile dir on
        # isolated launches. None for non-isolated launches and for
        # remote_cdp (where we did not own the launch).
        self._issued_token: str | None = None
        # v1.6.9: when isolation_mode + profile_mode=="named", we launch
        # via chromium.launch_persistent_context which returns a
        # BrowserContext (NOT a Browser). The persistent context IS the
        # one and only context that shares profile state -- you cannot
        # create additional contexts that see the same cookies. All
        # callers therefore share this single context via
        # _PersistentContextRef (no-op close so per-call cleanup leaves
        # it alive across the Agent lifetime).
        self._persistent_context: BrowserContext | None = None
        # v1.7.0: ephemeral isolation also launches via
        # launch_persistent_context (Playwright >= 1.5x rejects a
        # --user-data-dir CLI arg on chromium.launch outright -- see
        # start()). The root context is held ONLY so Chromium keeps its
        # state inside the webTool-owned tempdir; callers never see it.
        # Per-session isolation is preserved: _build_context creates
        # fresh incognito contexts via root.browser.new_context().
        self._ephemeral_root_context: BrowserContext | None = None
        # v1.7.0 crash recovery state. _crashed flips when Playwright
        # reports the browser/persistent-context gone outside an
        # intentional stop(); _stopping suppresses the flip during
        # stop(); _relaunch_lock serializes ensure_running() waiters so
        # only one of them performs the relaunch; _generation counts
        # successful start()s so SessionManager can detect sessions
        # belonging to a previous (dead) browser instance.
        self._crashed: bool = False
        self._stopping: bool = False
        self._relaunch_lock = asyncio.Lock()
        self._generation: int = 0

    def _resolve_profile_dir(self) -> Path:
        """Resolve the effective user-data-dir for isolation_mode.

        Ephemeral: ``tempfile.mkdtemp`` under ``base_dir/.webtool/browser-profiles/``.
        Named: ``safe_join_path(base_dir, profile_dir)`` -- the existing util
        already rejects absolute traversal across platforms (v1.6.4 fix).

        Returns the absolute Path. Marks ``_owned_profile_dir`` so cleanup
        only removes ephemeral profiles.
        """
        bcfg = self._config.browser
        base_root = Path(self._config.base_dir).resolve() / ".webtool" / "browser-profiles"
        base_root.mkdir(parents=True, exist_ok=True)

        if bcfg.profile_mode == "ephemeral":
            # In ephemeral mode, `profile_dir` is ignored -- the contract is
            # "auto-generated tempdir, deleted on exit." Setting profile_dir
            # AND ephemeral is a misconfiguration (use mode="named" for
            # persistent profiles); warn loudly and use the tempdir anyway.
            if bcfg.profile_dir:
                logger.warning(
                    "browser.profile_dir={p!r} is ignored when profile_mode='ephemeral'. "
                    "Use profile_mode='named' for a persistent profile.",
                    p=bcfg.profile_dir,
                )
            tmp = Path(tempfile.mkdtemp(prefix="run-", dir=str(base_root)))
            self._owned_profile_dir = True
            return tmp

        # profile_mode == "named"
        # Validator guarantees profile_dir is set when mode is "named".
        assert bcfg.profile_dir is not None
        resolved = safe_join_path(Path(self._config.base_dir).resolve(), bcfg.profile_dir)
        resolved.mkdir(parents=True, exist_ok=True)
        self._owned_profile_dir = False
        return resolved

    async def _discover_cdp_endpoint(self, profile_dir: Path) -> None:
        """After launch with --remote-debugging-port=0, Chromium writes
        ``DevToolsActivePort`` into the user-data-dir. The file contains
        two lines: ``<port>\\n/devtools/browser/<uuid>``. Poll briefly
        for it (Chromium writes it post-launch but before our control
        flow returns; usually <100ms).
        """
        port_file = profile_dir / "DevToolsActivePort"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0  # 5s budget
        while loop.time() < deadline:
            if port_file.exists():
                try:
                    content = port_file.read_text(encoding="utf-8").strip().splitlines()
                    if len(content) >= 2 and content[0].isdigit():
                        port = int(content[0])
                        ws_path = content[1]
                        self._cdp_port_resolved = port
                        self._cdp_endpoint = f"ws://{self._config.browser.cdp_host}:{port}{ws_path}"
                        return
                except Exception as exc:
                    logger.debug("DevToolsActivePort parse error: {e}", e=exc)
                    # fall through and keep polling
            await asyncio.sleep(0.05)
        logger.warning(
            "Could not discover CDP endpoint via DevToolsActivePort within 5s; "
            "cdp_enabled is True but get_cdp_endpoint() will return None."
        )

    def get_cdp_endpoint(self) -> str | None:
        """Return the CDP WebSocket endpoint of the webTool-launched browser.

        Format: ``ws://host:port/devtools/browser/<uuid>``. Returns ``None``
        when ``cdp_enabled=False`` or before ``start()`` has completed.

        External CDP tools (chrome://inspect, custom debuggers, browser-use,
        playwright-inspector) can connect to this endpoint. webTool never
        attaches to other endpoints -- this is the only one it controls.
        """
        return self._cdp_endpoint

    @property
    def generation(self) -> int:
        """v1.7.0: monotonically increasing count of successful ``start()``s.

        :class:`SessionManager` snapshots this at session-create time; a
        mismatch on later access means the session's BrowserContext
        belongs to a previous (crashed / relaunched) browser instance.
        """
        return self._generation

    def is_alive(self) -> bool:
        """v1.7.0: True when a started browser connection is believed live.

        False before ``start()``, after ``stop()``, after a detected
        crash (``disconnected`` / persistent-context ``close`` events),
        or when Playwright reports the Browser handle disconnected.
        """
        if not self._started or self._crashed:
            return False
        if self._browser is not None:
            try:
                return bool(self._browser.is_connected())
            except Exception:
                return False
        # Named persistent profiles on Playwright versions where
        # ctx.browser is None: rely on the close-event-driven flag.
        return self._persistent_context is not None

    def _on_browser_disconnected(self, _browser: Any = None) -> None:
        """Playwright ``disconnected`` callback: flag an unexpected death."""
        if self._stopping or not self._started:
            return
        self._crashed = True
        # v1.7.0 (Wave 4A): count the unexpected death (crash / OOM-kill /
        # external terminate). Fires once per disconnect; the relaunch
        # outcome is counted separately in ensure_running.
        self._metrics.incr("browser_crash")
        logger.warning(
            "Chromium disconnected unexpectedly (crash, OOM-kill, or external "
            "terminate). auto_relaunch={ar}: {hint}",
            ar=self._config.browser.auto_relaunch,
            hint=(
                "next browser acquisition will relaunch"
                if self._config.browser.auto_relaunch
                else "subsequent calls will fail until the Agent is restarted"
            ),
        )

    def _on_persistent_context_closed(self, _context: Any = None) -> None:
        """Persistent-context ``close`` callback (named-profile path)."""
        # Same semantics as a browser disconnect: outside stop(), the
        # persistent context closing means the browser died under us.
        self._on_browser_disconnected(None)

    def _wire_crash_handlers(self) -> None:
        """Attach disconnect/close listeners after a successful start()."""
        if self._browser is not None:
            self._browser.on("disconnected", self._on_browser_disconnected)
        if self._persistent_context is not None:
            self._persistent_context.on("close", self._on_persistent_context_closed)

    async def ensure_running(self) -> None:
        """v1.7.0 health gate used by the public context-acquisition paths.

        No-op when the browser was never started (downstream raises the
        established "not started" error) or when it is alive. When the
        browser crashed or was closed externally:

        * ``browser.auto_relaunch=False`` -> immediate
          :class:`~web_agent.exceptions.BrowserError` with operator guidance.
        * ``auto_relaunch=True`` -> bounded relaunch attempts
          (``relaunch_max_attempts``) with exponential backoff
          (``relaunch_backoff_base_s * 2**(attempt-1)``); exhaustion raises
          ``BrowserError``.

        Safe under concurrency: all waiters funnel through one lock and
        only the first performs the relaunch. For ``backend='remote_cdp'``
        a "relaunch" is a re-connect to the same endpoint (webTool does
        not own the remote process).
        """
        from .exceptions import BrowserError

        if not self._started or self.is_alive():
            return

        bcfg = self._config.browser
        if not bcfg.auto_relaunch:
            raise BrowserError(
                "Browser is no longer running (crashed or closed externally) "
                "and browser.auto_relaunch=False. Restart the Agent, or set "
                "browser.auto_relaunch=True to recover automatically."
            )

        async with self._relaunch_lock:
            # Another waiter may have completed the relaunch while we
            # queued on the lock.
            if self._started and self.is_alive():
                return
            attempts = bcfg.relaunch_max_attempts
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                logger.warning(
                    "Relaunching crashed browser (attempt {a}/{n})",
                    a=attempt,
                    n=attempts,
                )
                # stop() tears down dead handles, removes the orphaned
                # ephemeral profile, and resets _started/_crashed so
                # start() runs a clean launch.
                with suppress(Exception):
                    await self.stop()
                try:
                    await self.start()
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "Relaunch attempt {a}/{n} failed: {e}",
                        a=attempt,
                        n=attempts,
                        e=exc,
                    )
                    if attempt < attempts:
                        await asyncio.sleep(bcfg.relaunch_backoff_base_s * (2 ** (attempt - 1)))
                    continue
                logger.warning(
                    "Browser relaunched successfully (attempt {a}/{n}, generation {g})",
                    a=attempt,
                    n=attempts,
                    g=self._generation,
                )
                # v1.7.0 (Wave 4A): a relaunch sequence succeeded.
                self._metrics.incr("browser_relaunch", result="ok")
                return
            # v1.7.0 (Wave 4A): all relaunch attempts exhausted.
            self._metrics.incr("browser_relaunch", result="failed")
            raise BrowserError(
                f"Browser crashed and {attempts} relaunch attempt(s) failed "
                f"(browser.relaunch_max_attempts={attempts}). Last error: {last_exc}. "
                "Check that Chromium is installed (python -m playwright install "
                "chromium), that the host has free memory, and the logs above; "
                "then restart the Agent."
            ) from last_exc

    def _stealth_launch_args(self, base_args: list[str]) -> list[str]:
        """v1.7.0: mirror the CLI-arg patches playwright-stealth's hooked
        launch used to apply (``Stealth._kwargs_with_patched_cli_arg``):
        ``--disable-blink-features=AutomationControlled`` when the
        ``navigator_webdriver`` evasion is on, and ``--accept-lang=...``
        when ``navigator_languages`` is on. Returns a new list; honors
        ``Stealth(init_scripts_only=True)`` by returning the args as-is.
        """
        st = self._stealth
        if getattr(st, "init_scripts_only", False):
            return list(base_args)
        args = list(base_args)
        if getattr(st, "navigator_webdriver", True) and not any(
            a.startswith("--disable-blink-features=") for a in args
        ):
            args.append("--disable-blink-features=AutomationControlled")
        if getattr(st, "navigator_languages", True) and not any(
            a.startswith("--accept-lang=") for a in args
        ):
            langs = ",".join(getattr(st, "navigator_languages_override", ("en-US", "en")))
            args.append(f"--accept-lang={langs}")
        return args

    async def _apply_stealth(self, ctx: BrowserContext) -> None:
        """v1.7.0: apply stealth init scripts to a context (best-effort).

        Replaces the implicit per-context application the
        ``Stealth.use_async`` hook performed on ``browser.new_context``.
        Gated on ``browser.stealth_enabled``; a failure degrades to a
        WARNING (the context still works, just without evasions).
        """
        if not self._config.browser.stealth_enabled:
            return
        try:
            await self._stealth.apply_stealth_async(ctx)
        except Exception as exc:
            logger.warning("Failed to apply stealth init scripts to context: {e}", e=exc)

    async def _sweep_orphan_profiles(self) -> None:
        """v1.7.0: best-effort removal of orphaned ephemeral profile dirs.

        Crashed runs (or ``cleanup_on_exit=False`` configurations that
        later flipped) leave ``run-*`` tempdirs under
        ``<base_dir>/.webtool/browser-profiles`` forever. On start(),
        remove the ones whose newest content is older than
        ``browser.profile_sweep_max_age_h`` hours.

        Conservative by design (see ownership.py for the ownership
        model):

        * only ``run-*`` dirs are candidates -- named profiles and
          foreign dirs are never touched;
        * the live profile of THIS process is skipped;
        * age is the NEWEST mtime among the dir and its immediate
          children, so a profile being actively written by another live
          webTool process (Chromium touches its SQLite stores
          constantly) never looks old;
        * any doubt (stat/list failure) skips the dir with a DEBUG log.
        """
        max_age_h = self._config.browser.profile_sweep_max_age_h
        if max_age_h <= 0:
            return
        base_root = Path(self._config.base_dir).resolve() / ".webtool" / "browser-profiles"
        try:
            if not base_root.is_dir():
                return
            cutoff = time.time() - max_age_h * 3600.0
            live = (
                self._effective_profile_dir.resolve()
                if self._effective_profile_dir is not None
                else None
            )
            for entry in base_root.iterdir():
                try:
                    if not entry.is_dir() or not entry.name.startswith("run-"):
                        continue
                    if live is not None and entry.resolve() == live:
                        continue
                    newest = entry.stat().st_mtime
                    for child in entry.iterdir():
                        with suppress(OSError):
                            newest = max(newest, child.stat().st_mtime)
                    if newest > cutoff:
                        logger.debug(
                            "Profile sweep: skipping {p} (recent activity, "
                            "possibly owned by a live process)",
                            p=entry,
                        )
                        continue
                    logger.info(
                        "Profile sweep: removing orphaned ephemeral profile {p} "
                        "(idle > {h}h)",
                        p=entry,
                        h=max_age_h,
                    )
                    await self._cleanup_profile_dir(entry, retries=2)
                except OSError as exc:
                    logger.debug("Profile sweep: skipping {p}: {e}", p=entry, e=exc)
        except Exception as exc:
            logger.debug("Profile sweep aborted: {e}", e=exc)

    async def start(self) -> None:
        """Launch the Chromium browser. Idempotent under concurrent calls.

        v1.6.6: when ``browser.isolation_mode=True`` the launch happens
        against a webTool-owned user-data-dir (ephemeral tempdir or
        named profile). When ``browser.cdp_enabled=True`` (which requires
        isolation_mode), the launch also passes ``--remote-debugging-port``
        and discovers the resolved endpoint from ``DevToolsActivePort``.

        v1.7.0: ALL isolated launches (ephemeral and named) dispatch to
        ``chromium.launch_persistent_context(user_data_dir=...)``.
        Playwright >= 1.5x rejects a ``--user-data-dir`` CLI arg on
        ``chromium.launch`` pre-spawn ("Pass user_data_dir parameter to
        'browser_type.launch_persistent_context'..."), which broke
        ephemeral isolation at runtime. The ephemeral root context is
        held internally only -- callers still get fresh incognito
        contexts from ``_build_context`` via ``root.browser``, so
        per-session isolation is unchanged. The Playwright lifecycle is
        raw ``async_playwright()``; stealth evasions are applied
        per-context (``_apply_stealth``) instead of via
        ``Stealth.use_async``'s hooked launch.

        Raises:
            BrowserError: If Playwright fails to launch Chromium.
        """
        from .exceptions import BrowserError

        async with self._lifecycle_lock:
            if self._started:
                return

            bcfg = self._config.browser

            # v1.6.8 remote_cdp short-circuit: skip profile resolution and
            # launch args entirely -- we're connecting to an already-running
            # browser, not launching one. The config validator has already
            # confirmed remote_cdp_url is a loopback ws:// endpoint.
            if bcfg.backend == "remote_cdp":
                # v1.6.9: verify the ownership token BEFORE opening a
                # CDP connection. The validator guarantees both fields
                # are set when backend='remote_cdp'.
                assert bcfg.remote_cdp_profile_dir is not None
                assert bcfg.remote_cdp_ownership_token is not None
                profile_path = Path(bcfg.remote_cdp_profile_dir)
                if not OwnershipToken.verify(profile_path, bcfg.remote_cdp_ownership_token):
                    raise BrowserError(
                        f"Ownership token verification failed for {profile_path}. "
                        "The remote browser is not webTool-owned, or the profile "
                        "directory was modified after launch. remote_cdp refuses "
                        "to attach without a valid token (v1.6.9)."
                    )
                try:
                    self._pw_cm = async_playwright()
                    self._playwright = await self._pw_cm.__aenter__()
                    assert bcfg.remote_cdp_url is not None  # validator guarantees
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        bcfg.remote_cdp_url
                    )
                    self._is_remote_cdp = True
                    self._crashed = False
                    self._generation += 1
                    self._started = True
                    # v1.7.0 (Wave 4A): count the (remote) browser bring-up.
                    self._metrics.incr("browser_launch")
                    # v1.7.0: a remote disconnect is surfaced like a crash;
                    # ensure_running() then attempts a re-connect.
                    self._wire_crash_handlers()
                    logger.info(
                        "Connected to remote CDP browser: {u} (ownership verified)",
                        u=bcfg.remote_cdp_url,
                    )
                    return
                except Exception as exc:
                    if self._pw_cm is not None:
                        with suppress(Exception):
                            await self._pw_cm.__aexit__(None, None, None)
                    self._pw_cm = None
                    self._playwright = None
                    self._browser = None
                    self._is_remote_cdp = False
                    raise BrowserError(
                        f"Failed to connect to remote CDP at {bcfg.remote_cdp_url}: {exc}"
                    ) from exc

            # Resolve profile dir BEFORE launch so a failure to create it
            # surfaces as ConfigError-like behavior, not BrowserError.
            if bcfg.isolation_mode:
                self._effective_profile_dir = self._resolve_profile_dir()

            # Build the args list dynamically. v1.6.9: --no-sandbox is
            # now opt-in / auto-detected (CI + container heuristic).
            # Local dev defaults to keeping Chromium's sandbox enabled.
            args: list[str] = [
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ]
            if _should_disable_chromium_sandbox(bcfg.disable_chromium_sandbox):
                args.insert(0, "--no-sandbox")
            # v1.7.0: stealth's CLI-side evasions (previously injected by
            # the Stealth.use_async hooked launch) are added explicitly.
            if bcfg.stealth_enabled:
                args = self._stealth_launch_args(args)
            # v1.7.0: BOTH isolation flavors dispatch to
            # launch_persistent_context -- Playwright >= 1.5x rejects the
            # --user-data-dir CLI arg on chromium.launch pre-spawn, so
            # the v1.6.6 "CLI flag for ephemeral" approach is dead. The
            # user_data_dir is always passed as an explicit kwarg.
            use_persistent_named = (
                bcfg.isolation_mode
                and bcfg.profile_mode == "named"
                and self._effective_profile_dir is not None
            )
            use_persistent_ephemeral = (
                bcfg.isolation_mode
                and not use_persistent_named
                and self._effective_profile_dir is not None
            )
            if bcfg.cdp_enabled:
                args.append(f"--remote-debugging-port={bcfg.cdp_port}")
                args.append(f"--remote-debugging-address={bcfg.cdp_host}")

            # v1.7.0 (Wave 2F): outbound proxy. ``playwright_proxy()`` returns
            # the ``proxy=`` dict (server + optional username/password/bypass)
            # or None when no proxy is configured. We splat it into a kwargs
            # dict so the ``proxy`` key is OMITTED ENTIRELY when inactive --
            # never ``proxy=None`` vs absent inconsistently. Applied to BOTH
            # launch_persistent_context (named + ephemeral) and chromium.launch.
            proxy_dict = self._config.proxy.playwright_proxy()
            proxy_kwargs: dict[str, Any] = {"proxy": proxy_dict} if proxy_dict is not None else {}
            if proxy_dict is not None:
                logger.info(
                    "Routing browser egress through proxy: {server}",
                    server=proxy_dict.get("server"),
                )

            try:
                # v1.7.0: raw Playwright lifecycle. Stealth evasions are
                # applied per-context in _build_context / on the named
                # persistent context below.
                self._pw_cm = async_playwright()
                self._playwright = await self._pw_cm.__aenter__()

                if use_persistent_named:
                    # v1.6.9: named profile uses launch_persistent_context
                    # which returns a BrowserContext directly. Cookies /
                    # localStorage persist across runs because Chromium
                    # writes them into user_data_dir. All callers share
                    # this single context (Playwright limitation: you
                    # cannot create additional contexts that share state
                    # with a persistent profile context).
                    assert self._effective_profile_dir is not None
                    self._persistent_context = (
                        await self._playwright.chromium.launch_persistent_context(
                            user_data_dir=str(self._effective_profile_dir),
                            headless=bcfg.headless,
                            slow_mo=bcfg.slow_mo,
                            args=args,
                            viewport={
                                "width": bcfg.viewport_width,
                                "height": bcfg.viewport_height,
                            },
                            user_agent=_resolve_user_agent(bcfg),
                            locale=bcfg.locale,
                            timezone_id=bcfg.timezone_id,
                            java_script_enabled=True,
                            bypass_csp=False,
                            **proxy_kwargs,
                        )
                    )
                    self._persistent_context.set_default_timeout(bcfg.default_timeout)
                    self._persistent_context.set_default_navigation_timeout(bcfg.navigation_timeout)
                    # v1.7.0: the Stealth.use_async hook never covered
                    # launch_persistent_context (named profiles ran with NO
                    # evasions pre-v1.7.0). Apply explicitly -- shared by
                    # every session on this profile.
                    await self._apply_stealth(self._persistent_context)
                    # Apply resource blocking once on the persistent
                    # context (per-call routing would accumulate).
                    blocked = bcfg.block_resources
                    if blocked:

                        async def _block_resources(route: Route) -> None:
                            # v1.6.16 BR-3: on context/page teardown the route
                            # may already be gone; abort()/continue_() then raise
                            # into Playwright's dispatcher. A routing decision on
                            # a dying page is moot -- suppress.
                            with suppress(Exception):
                                if route.request.resource_type in blocked:
                                    await route.abort()
                                else:
                                    await route.continue_()

                        await self._persistent_context.route("**/*", _block_resources)
                    # The persistent context exposes its parent browser
                    # via .browser for callers that still need a Browser
                    # ref (e.g. close paths). On some Playwright versions
                    # this is None when launched via persistent context;
                    # we keep _browser=None and route everything through
                    # _persistent_context.
                    self._browser = self._persistent_context.browser
                elif use_persistent_ephemeral:
                    # v1.7.0: ephemeral isolation. The root context exists
                    # only so Chromium anchors its state (and
                    # DevToolsActivePort) inside the webTool-owned tempdir;
                    # webTool never opens pages in it. Callers receive
                    # fresh incognito contexts from _build_context via
                    # root.browser.new_context(), preserving the
                    # per-session isolation contract of ephemeral mode.
                    assert self._effective_profile_dir is not None
                    self._ephemeral_root_context = (
                        await self._playwright.chromium.launch_persistent_context(
                            user_data_dir=str(self._effective_profile_dir),
                            headless=bcfg.headless,
                            slow_mo=bcfg.slow_mo,
                            args=args,
                            **proxy_kwargs,
                        )
                    )
                    self._browser = self._ephemeral_root_context.browser
                    if self._browser is None:
                        raise BrowserError(
                            "launch_persistent_context returned a context with "
                            "no .browser handle; ephemeral isolation requires "
                            "it to create per-session contexts. Upgrade "
                            "Playwright (>=1.55 exposes it) or use "
                            "profile_mode='named'."
                        )
                else:
                    self._browser = await self._playwright.chromium.launch(
                        headless=bcfg.headless,
                        slow_mo=bcfg.slow_mo,
                        args=args,
                        **proxy_kwargs,
                    )
                self._crashed = False
                self._generation += 1
                self._started = True
                # v1.7.0 (Wave 4A): count the local browser bring-up. Placed
                # after _started flips so a launch that threw (caught below
                # and rolled back) is never counted as a successful launch.
                self._metrics.incr("browser_launch")
                self._wire_crash_handlers()
                logger.info(
                    "Browser launched (headless={h}, isolation={iso}, cdp={cdp})",
                    h=bcfg.headless,
                    iso=bcfg.isolation_mode,
                    cdp=bcfg.cdp_enabled,
                )
                if bcfg.isolation_mode:
                    logger.info(
                        "Isolation profile: {p} (mode={mode}, owned={owned})",
                        p=self._effective_profile_dir,
                        mode=bcfg.profile_mode,
                        owned=self._owned_profile_dir,
                    )
                    # v1.6.9: write an ownership token under the profile dir
                    # so remote_cdp callers can prove this browser was
                    # launched by webTool. Issued for both ephemeral and
                    # named profiles -- named profiles overwrite per launch.
                    # v1.6.9 review (C-1): we previously suppress(OSError)'d
                    # silently here, which left `_issued_token=None` with no
                    # operator-visible signal. A token-issuance failure
                    # breaks the remote_cdp ownership chain -- callers calling
                    # `get_ownership_token()` get None and can't attach.
                    # Surface the failure as a WARNING so the operator can
                    # diagnose (read-only fs / permission / disk full).
                    if self._effective_profile_dir is not None:
                        try:
                            self._issued_token = OwnershipToken.issue(self._effective_profile_dir)
                            logger.debug(
                                "Ownership token issued at {p}/{f}",
                                p=self._effective_profile_dir,
                                f=OwnershipToken.FILENAME,
                            )
                        except OSError as exc:
                            logger.warning(
                                "Failed to write ownership token at {p}/{f}: {e}. "
                                "remote_cdp siblings cannot attach to this browser.",
                                p=self._effective_profile_dir,
                                f=OwnershipToken.FILENAME,
                                e=exc,
                            )
                if bcfg.cdp_enabled and self._effective_profile_dir is not None:
                    await self._discover_cdp_endpoint(self._effective_profile_dir)
                    if self._cdp_endpoint:
                        logger.info("CDP endpoint: {ep}", ep=self._cdp_endpoint)
                # v1.7.0: best-effort removal of orphaned ephemeral
                # profiles left behind by crashed runs. Never raises.
                await self._sweep_orphan_profiles()
            except Exception as exc:
                # Roll back partial state and re-raise as BrowserError so callers
                # can `except BrowserError` reliably.
                self._started = False
                if self._persistent_context is not None:
                    with suppress(Exception):
                        await self._persistent_context.close()
                    self._persistent_context = None
                if self._ephemeral_root_context is not None:
                    with suppress(Exception):
                        await self._ephemeral_root_context.close()
                    self._ephemeral_root_context = None
                if self._pw_cm is not None:
                    with suppress(Exception):
                        await self._pw_cm.__aexit__(None, None, None)
                self._pw_cm = None
                self._playwright = None
                self._browser = None
                # Clean up the ephemeral profile we just created -- failed
                # launches must not leak tempdirs.
                if self._owned_profile_dir and self._effective_profile_dir is not None:
                    shutil.rmtree(self._effective_profile_dir, ignore_errors=True)
                self._effective_profile_dir = None
                self._owned_profile_dir = False
                self._cdp_endpoint = None
                self._cdp_port_resolved = None
                self._issued_token = None
                raise BrowserError(f"Failed to launch Chromium: {exc}") from exc

    async def _cleanup_profile_dir(self, profile_dir: Path, *, retries: int = 5) -> None:
        """Remove an ephemeral profile dir with a Windows-aware retry.

        On Windows, ``await self._browser.close()`` returns when Playwright's
        connection drops, NOT when the chromium.exe OS process actually
        exits. Chromium holds exclusive locks on SQLite databases inside
        the user-data-dir (Cookies, History, etc.). A naive
        ``shutil.rmtree`` racing the OS-process exit fails with
        PermissionError on those locked files and ``ignore_errors=True``
        would silently leave a partial profile on disk.

        Retry with exponential-ish backoff (200ms / 400ms / 600ms / ...).
        If all retries fail, log a clear warning so the operator can clean
        up manually.
        """
        for attempt in range(retries):
            try:
                shutil.rmtree(profile_dir)
                logger.info("Ephemeral profile removed: {p}", p=profile_dir)
                return
            except FileNotFoundError:
                return  # already gone
            except PermissionError:
                if attempt < retries - 1:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
            except Exception as exc:
                logger.warning(
                    "Error removing ephemeral profile {p}: {e}",
                    p=profile_dir,
                    e=exc,
                )
                return
        logger.warning(
            "Ephemeral profile {p} could not be removed after {n} retries "
            "(likely Windows file-locks held by exiting chromium.exe); "
            "manual cleanup may be required.",
            p=profile_dir,
            n=retries,
        )

    async def stop(self) -> None:
        """Close the browser and Playwright. Idempotent under concurrent calls.

        v1.6.8: when the backend is ``remote_cdp``, ``browser.close()``
        disconnects the Playwright client without killing the remote
        Chromium process (per Playwright docs). This is the intended
        behaviour for cloud-browser / browser-farm callers.

        v1.6.9: named persistent profiles close their single
        ``BrowserContext`` (which in turn terminates the underlying
        Chromium) before the regular browser path runs.

        v1.7.0: the ephemeral-isolation root context is closed the same
        way (it owns the Chromium process), and the ``_stopping`` flag
        suppresses the crash detector while we tear down intentionally.
        """
        async with self._lifecycle_lock:
            self._stopping = True
            if self._persistent_context is not None:
                # v1.6.9: launch_persistent_context owns the chromium
                # process. Closing the persistent context terminates it
                # AND flushes profile data to disk -- which is exactly
                # the named-profile contract.
                try:
                    await self._persistent_context.close()
                except Exception as exc:
                    logger.warning("Error closing persistent context: {e}", e=exc)
                self._persistent_context = None
                # Underlying browser ref (if any) is owned by the
                # persistent context we just closed -- do NOT call
                # close() on it again or we'll race on a dead handle.
                self._browser = None
            elif self._ephemeral_root_context is not None:
                # v1.7.0: same ownership contract as the named path --
                # closing the root context terminates Chromium.
                try:
                    await self._ephemeral_root_context.close()
                except Exception as exc:
                    logger.warning("Error closing ephemeral root context: {e}", e=exc)
                self._ephemeral_root_context = None
                self._browser = None
            elif self._browser:
                try:
                    # close() on a connect_over_cdp browser is a disconnect.
                    # On a locally-launched browser it terminates the process.
                    await self._browser.close()
                except Exception as exc:
                    logger.warning(
                        "Error {action}: {e}",
                        action="disconnecting remote CDP"
                        if self._is_remote_cdp
                        else "closing browser",
                        e=exc,
                    )
                self._browser = None
            if self._pw_cm:
                try:
                    await self._pw_cm.__aexit__(None, None, None)
                except Exception as exc:
                    logger.warning("Error closing Playwright: {e}", e=exc)
                self._playwright = None
                self._pw_cm = None
            # v1.6.6: clean up the ephemeral profile we created in start()
            # (only when WE created it -- named profiles are user-owned).
            # remote_cdp never owns a profile so this branch is a no-op for it.
            if (
                self._owned_profile_dir
                and self._config.browser.cleanup_on_exit
                and self._effective_profile_dir is not None
            ):
                await self._cleanup_profile_dir(self._effective_profile_dir)
            self._effective_profile_dir = None
            self._owned_profile_dir = False
            self._cdp_endpoint = None
            self._cdp_port_resolved = None
            self._issued_token = None
            self._is_remote_cdp = False
            self._started = False
            # v1.7.0: reset crash-recovery flags so a later start() (or
            # an ensure_running relaunch) begins from a clean slate.
            self._crashed = False
            self._stopping = False
            self._ephemeral_root_context = None
            logger.info("Browser closed")

    def get_remote_cdp_url(self) -> str | None:
        """v1.6.8: return the remote_cdp ws:// URL we connected to.

        Returns the configured ``BrowserConfig.remote_cdp_url`` when the
        backend is ``remote_cdp`` AND ``start()`` succeeded. Returns None
        for the ``playwright`` and ``cdp_owned`` backends, and before
        ``start()`` completes.
        """
        if self._is_remote_cdp:
            return self._config.browser.remote_cdp_url
        return None

    def get_ownership_token(self) -> str | None:
        """v1.6.9: return the ownership token issued at launch.

        Set only after a successful isolated launch (ephemeral or named).
        Returns None for non-isolated launches and for ``remote_cdp``.
        Callers wanting to spin up a sibling ``remote_cdp`` Agent against
        the same browser pass this token via
        ``BrowserConfig.remote_cdp_ownership_token`` plus
        ``remote_cdp_profile_dir`` pointing at ``get_effective_profile_dir()``.
        """
        return self._issued_token

    def get_effective_profile_dir(self) -> Path | None:
        """v1.6.9: return the resolved profile dir for an isolated launch.

        Pair with :meth:`get_ownership_token` to construct a sibling
        ``remote_cdp`` config. Returns None when ``isolation_mode=False``,
        before ``start()``, or when the backend is ``remote_cdp`` (we
        don't own the remote profile).
        """
        return self._effective_profile_dir

    async def _build_context(
        self,
        user_agent: str | None = None,
        block_resources: bool | None = None,
    ) -> BrowserContext:
        """Internal: build a BrowserContext with stealth + UA rotation + resource blocking.

        Returns a context the caller is responsible for closing.
        Used by both ``new_context`` (semaphore-bounded, auto-closed) and
        ``create_persistent_context`` (no semaphore, caller-managed).

        v1.6.9: when running under a named persistent profile, this
        method returns the *single* shared persistent context wrapped in
        a no-close proxy. The wrapper forwards all calls except
        ``close()`` (which becomes a no-op) so per-caller cleanup logic
        keeps working unchanged. ``user_agent`` and ``block_resources``
        args are ignored in this mode -- those settings were applied
        once when the persistent context was launched (Playwright does
        not let us re-configure them post-launch).
        """
        if self._persistent_context is not None:
            if user_agent is not None or block_resources is not None:
                logger.debug(
                    "Named persistent profile: ignoring per-call user_agent / "
                    "block_resources overrides (settings are pinned at launch)."
                )
            # _NoCloseContextProxy quacks like BrowserContext via __getattr__
            # forwarding. mypy can't see the protocol so cast it down.
            # Stealth was applied ONCE to the underlying persistent
            # context at launch (v1.7.0) -- do not re-apply per proxy.
            return cast(BrowserContext, _NoCloseContextProxy(self._persistent_context))

        if self._crashed:
            # v1.7.0: callers normally pass through ensure_running()
            # first; this guard catches direct callers and the window
            # where a crash lands mid-acquisition.
            from .exceptions import BrowserError

            raise BrowserError(
                "Browser connection lost (crash detected). Retry the call -- "
                "auto_relaunch recovers on the next acquisition -- or restart "
                "the Agent if browser.auto_relaunch=False."
            )
        if not self._browser:
            raise RuntimeError("BrowserManager not started. Call start() first.")

        bcfg = self._config.browser
        # v1.6.9: locale / timezone_id / user_agent now read from config
        # (previously hardcoded). Explicit user_agent kwarg still wins
        # for callers that need a one-off override (search-engine
        # rotation in particular).
        ua = user_agent if user_agent is not None else _resolve_user_agent(bcfg)
        ctx = await self._browser.new_context(
            user_agent=ua,
            viewport={
                "width": bcfg.viewport_width,
                "height": bcfg.viewport_height,
            },
            locale=bcfg.locale,
            timezone_id=bcfg.timezone_id,
            java_script_enabled=True,
            bypass_csp=False,
        )
        # v1.7.0: stealth evasions applied explicitly per context. This
        # covers every flavor that reaches this point: ephemeral
        # isolation, non-isolated launches, and contexts created on a
        # remote_cdp-attached browser (which the Stealth.use_async hook
        # also used to cover via its hooked new_context).
        await self._apply_stealth(ctx)
        ctx.set_default_timeout(self._config.browser.default_timeout)
        ctx.set_default_navigation_timeout(self._config.browser.navigation_timeout)

        should_block = block_resources if block_resources is not None else True
        blocked = self._config.browser.block_resources
        if should_block and blocked:

            async def _block_resources(route: Route) -> None:
                # v1.6.16 BR-3: on context/page teardown the route may already
                # be gone; abort()/continue_() then raise into Playwright's
                # dispatcher. A routing decision on a dying page is moot --
                # suppress. Mirrors the persistent-context twin.
                with suppress(Exception):
                    if route.request.resource_type in blocked:
                        await route.abort()
                    else:
                        await route.continue_()

            await ctx.route("**/*", _block_resources)

        return ctx

    async def create_persistent_context(
        self,
        user_agent: str | None = None,
        block_resources: bool | None = None,
    ) -> BrowserContext:
        """Create a non-managed BrowserContext that the caller is responsible for closing.

        Used by :class:`SessionManager`. Bypasses the concurrency semaphore --
        sessions are explicit user-managed resources and shouldn't block
        ephemeral context allocation.

        Args:
            user_agent: Override the random user-agent.
            block_resources: Whether to block images/fonts/CSS/media.
                Defaults to False since interactive sessions usually need
                full styling. Pass ``True`` to opt in.

        Returns:
            A BrowserContext. Caller must call ``await ctx.close()`` when done.
        """
        # Default for sessions is to NOT block resources (interactive use)
        if block_resources is None:
            block_resources = False
        # v1.7.0: crash-recovery gate -- relaunches a dead browser (or
        # raises BrowserError) before building the session context.
        await self.ensure_running()
        ctx = await self._build_context(user_agent, block_resources)
        logger.debug("Persistent session context created")
        return ctx

    @asynccontextmanager
    async def new_context(
        self,
        user_agent: str | None = None,
        block_resources: bool | None = None,
    ) -> AsyncGenerator[BrowserContext, None]:
        """Acquire a semaphore-bounded browser context with stealth + user-agent rotation.

        Args:
            user_agent: Override the random user-agent. ``None`` picks a random one.
            block_resources: Whether to block images/fonts/CSS/media.
                ``None`` reads from config (default: True for fetch speed).
                ``False`` disables blocking (needed for automation/interaction).
        """
        async with self._semaphore:
            # v1.7.0: crash-recovery gate -- relaunches a dead browser
            # (or raises BrowserError) before building the context.
            await self.ensure_running()
            ctx = await self._build_context(user_agent, block_resources)
            try:
                yield ctx
            finally:
                await ctx.close()
                logger.debug("Context closed")

    @asynccontextmanager
    async def new_page(
        self,
        user_agent: str | None = None,
        block_resources: bool | None = None,
    ) -> AsyncGenerator[Page, None]:
        """Convenience: acquire context -> open page -> yield -> cleanup."""
        async with self.new_context(user_agent=user_agent, block_resources=block_resources) as ctx:
            page = await ctx.new_page()
            # v1.6.8: attach network capture (no-op when both diagnostic
            # switches are off). Done here so ephemeral fetches benefit
            # from network/api/download observability when enabled.
            if self._network_collector is not None:
                self._network_collector.attach(page)
            try:
                yield page
            finally:
                await page.close()
