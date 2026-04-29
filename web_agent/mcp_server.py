"""MCP (Model Context Protocol) server exposing web_agent as tools for AI clients.

Runs as an MCP server that Claude Desktop, Claude Code, Cursor, and other
MCP-compatible clients can connect to. Exposes 11 tools:

**Single-shot tools** (existing):
- ``web_search`` -- search + extract top results
- ``web_fetch`` -- fetch and extract a single URL
- ``web_download`` -- download a file or save a web page
- ``web_screenshot`` -- screenshot a page
- ``web_interact`` -- run a scripted browser action sequence

**High-level recipes** (new):
- ``web_search_best`` -- search and open the best-ranked result
- ``web_find_and_download`` -- search and download the first matching file
- ``web_research`` -- multi-page research with structured citations

**Browser sessions** (new) -- retain cookies/login across multiple tool calls:
- ``create_browser_session`` -- start a persistent browser session
- ``close_browser_session`` -- end a session and free its resources
- ``list_browser_sessions`` -- list live sessions

Each tool accepts an optional ``session_id`` to reuse a persistent context.

Usage::

    # Run directly (stdio transport, for MCP client connection):
    python -m web_agent.mcp_server

    # Or via installed script:
    web-agent-mcp

Claude Desktop config (``claude_desktop_config.json``)::

    {
      "mcpServers": {
        "web_agent": {
          "command": "python",
          "args": ["-m", "web_agent.mcp_server"]
        }
      }
    }
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from loguru import logger
from mcp.server.fastmcp import Context, FastMCP
from pydantic import TypeAdapter

from .agent import Agent
from .config import AppConfig
from .models import (
    Action,
    ActionSequenceResult,
    AgentResult,
    DownloadResult,
    ExtractionResult,
    ResearchResult,
    ScreenshotResult,
    SessionInfo,
)


# ---------------------------------------------------------------------------
# Lifespan: initialize the Agent once per MCP session (browser stays warm)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize the web_agent Agent once and share it across all tool calls."""
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting web_agent MCP server...")
    config = AppConfig()
    async with Agent(config) as agent:
        logger.info("web_agent MCP server ready")
        yield {"agent": agent}
    logger.info("web_agent MCP server stopped")


# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "web_agent",
    lifespan=lifespan,
    instructions=(
        "Web search, fetch, download, extraction, browser automation, and research toolkit. "
        "Single-shot: web_search, web_fetch, web_download, web_screenshot, web_interact. "
        "Recipes: web_search_best (top-ranked result), web_find_and_download (file by query), "
        "web_research (multi-page citations). "
        "Browser sessions retain cookies/login across calls -- "
        "create_browser_session, close_browser_session, list_browser_sessions. "
        "All tools accept an optional session_id to reuse a persistent context."
    ),
)


# ---------------------------------------------------------------------------
# Single-shot tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_search(
    ctx: Context,
    query: str,
    max_results: int = 10,
    session_id: Optional[str] = None,
) -> AgentResult:
    """Search the web and extract content from the top results.

    Uses Google with automatic DuckDuckGo fallback. Each result page is
    fetched and its main content extracted (title, description, text).

    Args:
        query: The search query string.
        max_results: Maximum number of results to process (default 10, max ~20).
        session_id: Optional persistent browser session for the page fetches.

    Returns:
        AgentResult with search metadata, extracted page contents, and any errors.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.search_and_extract(
        query, max_results=max_results, session_id=session_id
    )


@mcp.tool()
async def web_fetch(
    ctx: Context,
    url: str,
    session_id: Optional[str] = None,
) -> ExtractionResult:
    """Fetch a single URL and extract its main content.

    Renders JavaScript-heavy pages in a real browser. Extracts title,
    description, author, date, and main text using a three-tier fallback
    (trafilatura -> BeautifulSoup4 -> raw text).

    Args:
        url: The URL to fetch.
        session_id: Optional persistent browser session.

    Returns:
        ExtractionResult with title, content, metadata, and extraction method used.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.fetch_and_extract(url, session_id=session_id)


@mcp.tool()
async def web_download(
    ctx: Context,
    url: str,
    filename: Optional[str] = None,
    session_id: Optional[str] = None,
) -> DownloadResult:
    """Download a file or save a web page from a URL.

    Tries three strategies automatically: httpx streaming (fastest),
    Playwright page save (for 403-blocked or JS-rendered pages), and
    Playwright download event (for JS-triggered file downloads).

    Args:
        url: The file or page URL to download.
        filename: Optional output filename.
        session_id: Optional persistent browser session.

    Returns:
        DownloadResult with the saved file path, size, content type, and status.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.download(url, filename=filename, session_id=session_id)


@mcp.tool()
async def web_screenshot(
    ctx: Context,
    url: str,
    full_page: bool = False,
    path: Optional[str] = None,
    session_id: Optional[str] = None,
) -> ScreenshotResult:
    """Take a screenshot of a web page.

    Args:
        url: The URL to screenshot.
        full_page: If True, capture the full scrollable page; otherwise viewport only.
        path: Optional output file path.
        session_id: Optional persistent browser session.

    Returns:
        ScreenshotResult with the file path and image size.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.screenshot(
        url, path=path, full_page=full_page, session_id=session_id
    )


@mcp.tool()
async def web_interact(
    ctx: Context,
    url: str,
    actions: list[dict],
    stop_on_error: bool = True,
    session_id: Optional[str] = None,
) -> ActionSequenceResult:
    """Execute a scripted sequence of browser actions on a URL.

    Supports 12 action types: click, type, fill, scroll, screenshot, navigate,
    dialog, hover, select, keyboard, wait, evaluate. Each action is a dict with
    an ``action`` discriminator and action-specific parameters.

    Selectors can be either a CSS string or a semantic LocatorSpec dict::

        # CSS selector:
        {"action": "click", "selector": "button#submit"}

        # Semantic locator (role + accessible name):
        {"action": "click", "selector": {"role": "button", "role_name": "Submit"}}

        # Semantic locator (label):
        {"action": "fill", "selector": {"label": "Email"}, "value": "me@example.com"}

    Example sequence::

        [
          {"action": "wait", "target": "selector", "value": "h1"},
          {"action": "fill", "selector": "#search", "value": "query"},
          {"action": "click", "selector": "button[type=submit]"},
          {"action": "screenshot", "full_page": true},
          {"action": "evaluate", "expression": "document.title"}
        ]

    Args:
        url: Starting URL.
        actions: Ordered list of action dicts.
        stop_on_error: Halt sequence on first failure (skip remaining).
        session_id: Optional persistent browser session.

    Returns:
        ActionSequenceResult with per-action results and aggregate counts.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    adapter = TypeAdapter(list[Action])
    parsed_actions = adapter.validate_python(actions)
    return await agent.interact(
        url, parsed_actions, stop_on_error=stop_on_error, session_id=session_id
    )


# ---------------------------------------------------------------------------
# High-level recipes
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_search_best(
    ctx: Context,
    query: str,
    ranking: str = "default",
    session_id: Optional[str] = None,
) -> ExtractionResult:
    """Search the web, rank results, and return the extracted content of the top hit.

    Skips the manual "search -> pick result -> fetch" dance: ranks results by
    query overlap + HTTPS bonus + well-known domain bonus + position tiebreaker,
    then fetches and extracts the top URL.

    Args:
        query: The search query.
        ranking: Ranking scheme (``default`` | ``overlap`` | ``position``).
        session_id: Optional persistent browser session.

    Returns:
        ExtractionResult of the top-ranked URL.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.search_and_open_best_result(
        query, ranking=ranking, session_id=session_id
    )


@mcp.tool()
async def web_find_and_download(
    ctx: Context,
    query: str,
    file_types: Optional[list[str]] = None,
    session_id: Optional[str] = None,
) -> DownloadResult:
    """Search the web for a file matching ``query`` and download the first hit.

    Looks for direct file URLs (PDFs, XLSX, ZIPs, etc.) in the search results.
    Returns an error result if no matching file URL is found.

    Args:
        query: The search query (e.g. "Tesla 10-K 2024 PDF").
        file_types: Allowed extensions (e.g. ``["pdf", "xlsx"]``). Default ``["pdf"]``.
        session_id: Optional persistent browser session for the download.

    Returns:
        DownloadResult with file path, size, and status.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.find_and_download_file(
        query, file_types=file_types, session_id=session_id
    )


@mcp.tool()
async def web_research(
    ctx: Context,
    query: str,
    max_pages: int = 5,
    depth: int = 1,
    session_id: Optional[str] = None,
) -> ResearchResult:
    """Multi-page research recipe: search + parallel fetch+extract top N pages, build citations.

    Useful for "research X" or "summarize the latest on Y" tasks. Returns
    structured Citation objects (URL, title, snippet, relevance_score) plus
    full ExtractionResult per page for downstream summarization.

    Args:
        query: The research question or topic.
        max_pages: Maximum number of pages to fetch and extract.
        depth: Reserved for future expansion (only depth=1 supported in v1).
        session_id: Optional persistent browser session.

    Returns:
        ResearchResult with citations, summary_pages, budget telemetry, errors.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.web_research(
        query, depth=depth, max_pages=max_pages, session_id=session_id
    )


# ---------------------------------------------------------------------------
# Browser session management
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_browser_session(
    ctx: Context,
    name: Optional[str] = None,
) -> dict:
    """Create a persistent browser session that retains cookies/localStorage across calls.

    Pass the returned ``session_id`` as the ``session_id`` parameter to
    web_fetch / web_interact / web_download / web_screenshot / web_search /
    web_research / web_search_best / web_find_and_download to reuse the same
    logged-in context.

    Sessions persist until ``close_browser_session`` is called or the MCP
    server shuts down.

    Args:
        name: Optional human-friendly label (default: random token).

    Returns:
        Dict with ``session_id`` (the value to pass to other tools).
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    sid = await agent.create_session(name=name)
    return {"session_id": sid, "name": name}


@mcp.tool()
async def close_browser_session(ctx: Context, session_id: str) -> dict:
    """Close a persistent browser session and free its resources.

    Args:
        session_id: The session_id returned by create_browser_session.

    Returns:
        Dict with ``closed: True`` on success.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    await agent.close_session(session_id)
    return {"closed": True, "session_id": session_id}


@mcp.tool()
async def list_browser_sessions(ctx: Context) -> dict:
    """List all live browser sessions for the current MCP server.

    Returns:
        Dict with ``count`` and ``sessions`` (list of SessionInfo as dicts:
        session_id, name, created_at, last_used_at, page_count, user_agent).
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    sessions = agent.list_sessions()
    return {
        "count": len(sessions),
        "sessions": [s.model_dump(mode="json") for s in sessions],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server on stdio transport (for local MCP client connections)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
