"""Browser lifecycle management with stealth, user-agent rotation, and semaphore-bounded context pool."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

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
from .utils import get_random_user_agent


class BrowserManager:
    """Manages a single Chromium browser instance with stealth anti-detection
    and a semaphore-bounded pool of browser contexts."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._stealth = Stealth()
        self._semaphore = asyncio.Semaphore(config.browser.max_contexts)
        self._started = False
        self._pw_cm: object = None  # stealth context manager

    async def start(self) -> None:
        """Launch the Chromium browser. Call once at application start."""
        if self._started:
            return

        # Stealth wraps async_playwright() and auto-injects anti-detection
        # scripts into every context and page created through it.
        self._pw_cm = self._stealth.use_async(async_playwright())
        self._playwright = await self._pw_cm.__aenter__()  # type: ignore[union-attr]

        launch_args = {
            "headless": self._config.browser.headless,
            "slow_mo": self._config.browser.slow_mo,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ],
        }
        self._browser = await self._playwright.chromium.launch(**launch_args)
        self._started = True
        logger.info(
            "Browser launched (headless={h})", h=self._config.browser.headless
        )

    async def stop(self) -> None:
        """Close the browser and Playwright. Call once at application shutdown."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw_cm:
            await self._pw_cm.__aexit__(None, None, None)  # type: ignore[union-attr]
            self._playwright = None
            self._pw_cm = None
        self._started = False
        logger.info("Browser closed")

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
        async with self.new_context(
            user_agent=user_agent, block_resources=block_resources
        ) as ctx:
            page = await ctx.new_page()
            try:
                yield page
            finally:
                await page.close()
