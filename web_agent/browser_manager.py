"""Browser lifecycle management with stealth, user-agent rotation, and semaphore-bounded context pool."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

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
from .utils import get_random_user_agent, safe_join_path

if TYPE_CHECKING:  # pragma: no cover -- avoid runtime import cycle
    from .network_collector import NetworkCollector


class BrowserManager:
    """Manages a single Chromium browser instance with stealth anti-detection
    and a semaphore-bounded pool of browser contexts."""

    def __init__(
        self,
        config: AppConfig,
        network_collector: Optional[NetworkCollector] = None,
    ) -> None:
        self._config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._stealth = Stealth()
        self._semaphore = asyncio.Semaphore(config.browser.max_contexts)
        self._started = False
        # Stealth-wrapped async_playwright() context manager. Typed as Any
        # because Stealth.use_async() returns its own internal CM class
        # whose protocol mypy doesn't see -- runtime always exposes
        # __aenter__/__aexit__.
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

    async def start(self) -> None:
        """Launch the Chromium browser. Idempotent under concurrent calls.

        v1.6.6: when ``browser.isolation_mode=True`` the launch happens
        against a webTool-owned ``--user-data-dir`` (ephemeral tempdir or
        named profile). When ``browser.cdp_enabled=True`` (which requires
        isolation_mode), the launch also passes ``--remote-debugging-port``
        and discovers the resolved endpoint from ``DevToolsActivePort``.

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
                try:
                    self._pw_cm = self._stealth.use_async(async_playwright())
                    self._playwright = await self._pw_cm.__aenter__()
                    assert bcfg.remote_cdp_url is not None  # validator guarantees
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        bcfg.remote_cdp_url
                    )
                    self._is_remote_cdp = True
                    self._started = True
                    logger.info(
                        "Connected to remote CDP browser: {u}",
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

            # Build the args list dynamically. The first four are the
            # v1.6.5 defaults; isolation + CDP append after.
            args: list[str] = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ]
            if bcfg.isolation_mode and self._effective_profile_dir is not None:
                args.append(f"--user-data-dir={self._effective_profile_dir}")
            if bcfg.cdp_enabled:
                args.append(f"--remote-debugging-port={bcfg.cdp_port}")
                args.append(f"--remote-debugging-address={bcfg.cdp_host}")

            try:
                # Stealth wraps async_playwright() and auto-injects anti-detection
                # scripts into every context and page created through it.
                self._pw_cm = self._stealth.use_async(async_playwright())
                self._playwright = await self._pw_cm.__aenter__()

                self._browser = await self._playwright.chromium.launch(
                    headless=bcfg.headless,
                    slow_mo=bcfg.slow_mo,
                    args=args,
                )
                self._started = True
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
                if bcfg.cdp_enabled and self._effective_profile_dir is not None:
                    await self._discover_cdp_endpoint(self._effective_profile_dir)
                    if self._cdp_endpoint:
                        logger.info("CDP endpoint: {ep}", ep=self._cdp_endpoint)
            except Exception as exc:
                # Roll back partial state and re-raise as BrowserError so callers
                # can `except BrowserError` reliably.
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
        """
        async with self._lifecycle_lock:
            if self._browser:
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
            self._is_remote_cdp = False
            self._started = False
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

    async def _build_context(
        self,
        user_agent: str | None = None,
        block_resources: bool | None = None,
    ) -> BrowserContext:
        """Internal: build a BrowserContext with stealth + UA rotation + resource blocking.

        Returns a context the caller is responsible for closing.
        Used by both ``new_context`` (semaphore-bounded, auto-closed) and
        ``create_persistent_context`` (no semaphore, caller-managed).
        """
        if not self._browser:
            raise RuntimeError("BrowserManager not started. Call start() first.")

        ua = user_agent or get_random_user_agent()
        ctx = await self._browser.new_context(
            user_agent=ua,
            viewport={
                "width": self._config.browser.viewport_width,
                "height": self._config.browser.viewport_height,
            },
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
            bypass_csp=False,
        )
        ctx.set_default_timeout(self._config.browser.default_timeout)
        ctx.set_default_navigation_timeout(self._config.browser.navigation_timeout)

        should_block = block_resources if block_resources is not None else True
        blocked = self._config.browser.block_resources
        if should_block and blocked:

            async def _block_resources(route: Route) -> None:
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
