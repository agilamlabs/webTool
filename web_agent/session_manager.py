"""Persistent named BrowserContext sessions for multi-call state retention.

A 'session' is a long-lived Playwright BrowserContext (in contrast to the
ephemeral context yielded by :meth:`BrowserManager.new_context`).
Sessions retain cookies, localStorage, and origin tokens across multiple
Agent method calls until explicitly closed.

Persistent sessions intentionally bypass the BrowserManager semaphore
because they are explicit user resources -- the user knows when they're
created and closed.

Example::

    async with Agent() as agent:
        sid = await agent.create_session(name="my-login")
        # First call: log in
        await agent.interact("https://app.example.com/login", [
            FillInput(selector="#user", value="me"),
            FillInput(selector="#pass", value="secret"),
            ClickInput(selector="button[type=submit]"),
        ], session_id=sid)

        # Subsequent call reuses cookies:
        result = await agent.fetch_and_extract(
            "https://app.example.com/dashboard", session_id=sid
        )
        await agent.close_session(sid)
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from playwright.async_api import BrowserContext

from .browser_manager import BrowserManager
from .config import AppConfig
from .models import SessionInfo


class SessionManager:
    """Tracks named persistent BrowserContext objects for an Agent.

    Args:
        bm: Shared BrowserManager owning the Chromium instance.
        config: Application configuration.
    """

    def __init__(self, bm: BrowserManager, config: AppConfig) -> None:
        self._bm = bm
        self._config = config
        self._sessions: dict[str, BrowserContext] = {}
        self._info: dict[str, SessionInfo] = {}
        self._lock = asyncio.Lock()

    async def create(self, name: Optional[str] = None) -> str:
        """Create a new persistent session and return its ``session_id``.

        Args:
            name: Optional human-friendly label. If provided, the session_id
                will be ``f"{name}-{token}"``; otherwise a random token alone.

        Returns:
            The session_id string. Use it for ``session_id`` parameters on
            subsequent Agent method calls.
        """
        token = secrets.token_urlsafe(8)
        session_id = f"{name}-{token}" if name else token

        # Hold the lock across the entire creation: build context, probe UA,
        # register in dict. Prevents the visible-but-unowned context that
        # would result from a partial creation racing with close_all().
        async with self._lock:
            ctx = await self._bm.create_persistent_context(block_resources=False)
            ua = None
            try:
                ua_str = await ctx.new_page()
                ua = await ua_str.evaluate("() => navigator.userAgent")
                await ua_str.close()
            except Exception:
                ua = None

            info = SessionInfo(session_id=session_id, name=name, user_agent=ua)
            self._sessions[session_id] = ctx
            self._info[session_id] = info

        logger.info(
            "Session created: {sid} (name={name})",
            sid=session_id,
            name=name,
        )
        return session_id

    async def close(self, session_id: str) -> None:
        """Close and forget a session.

        Raises:
            KeyError: If session_id is not known.
        """
        async with self._lock:
            ctx = self._sessions.pop(session_id, None)
            self._info.pop(session_id, None)

        if ctx is None:
            raise KeyError(f"Unknown session_id: {session_id!r}")

        try:
            await ctx.close()
        except Exception as exc:
            logger.warning("Error closing session {sid}: {e}", sid=session_id, e=exc)

        logger.info("Session closed: {sid}", sid=session_id)

    async def close_all(self) -> None:
        """Close every live session (called from Agent.__aexit__).

        KeyError (session was already closed manually) is logged at DEBUG
        rather than WARNING so normal teardown stays quiet.
        """
        async with self._lock:
            sids = list(self._sessions.keys())

        for sid in sids:
            try:
                await self.close(sid)
            except KeyError:
                logger.debug("Session {sid} already closed", sid=sid)
            except Exception as exc:
                logger.warning("Error closing session {sid}: {e}", sid=sid, e=exc)

    def get(self, session_id: str) -> BrowserContext:
        """Return the live BrowserContext for ``session_id``.

        Raises:
            KeyError: If session_id is not known.
        """
        ctx = self._sessions.get(session_id)
        if ctx is None:
            raise KeyError(f"Unknown session_id: {session_id!r}")
        return ctx

    def list(self) -> list[SessionInfo]:
        """Return SessionInfo snapshots for all live sessions."""
        return list(self._info.values())

    def touch(self, session_id: str) -> None:
        """Update last_used_at and increment page_count on a session.

        Silent no-op if session_id is unknown (caller should have checked).
        """
        info = self._info.get(session_id)
        if info is None:
            return
        info.last_used_at = datetime.now(timezone.utc)
        info.page_count += 1
