"""MCP (Model Context Protocol) server exposing web_agent as tools for AI clients.

Runs as an MCP server that Claude Desktop, Claude Code, Cursor, and other
MCP-compatible clients can connect to. Exposes 5 tools:

- ``web_search``: Search the web and extract content from top results
- ``web_fetch``: Fetch and extract content from a single URL
- ``web_download``: Download a file or save a web page
- ``web_screenshot``: Take a screenshot of a web page
- ``web_interact``: Execute a scripted browser action sequence

The browser is initialized once per MCP session via the lifespan pattern,
so all tool calls within a session share the same warm browser instance.

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
from typing import AsyncIterator

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
    ScreenshotResult,
)


# ---------------------------------------------------------------------------
# Lifespan: initialize the Agent once per MCP session (browser stays warm)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize the web_agent Agent once and share it across all tool calls.

    The browser remains warm for the entire MCP session, so tool calls after
    the first one skip the ~5-10s browser startup cost.
    """
    # Keep MCP stdio transport clean: route logs to stderr only
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
        "Web search, fetch, download, extraction, and browser automation toolkit. "
        "Use web_search for discovering information, web_fetch for reading a specific page, "
        "web_download for files (PDFs, docs, data), web_screenshot for visual captures, "
        "and web_interact for scripted browser automation (clicks, forms, scrolling)."
    ),
)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_search(
    ctx: Context,
    query: str,
    max_results: int = 10,
) -> AgentResult:
    """Search the web and extract content from the top results.

    Uses Google with automatic DuckDuckGo fallback. Each result page is
    fetched and its main content extracted (title, description, text).

    Args:
        query: The search query string.
        max_results: Maximum number of results to process (default 10, max ~20).

    Returns:
        AgentResult with search metadata, extracted page contents, and any errors.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.search_and_extract(query, max_results=max_results)


@mcp.tool()
async def web_fetch(ctx: Context, url: str) -> ExtractionResult:
    """Fetch a single URL and extract its main content.

    Renders JavaScript-heavy pages in a real browser. Extracts title,
    description, author, date, and main text using a three-tier fallback
    (trafilatura -> BeautifulSoup4 -> raw text).

    Args:
        url: The URL to fetch.

    Returns:
        ExtractionResult with title, content, metadata, and extraction method used.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.fetch_and_extract(url)


@mcp.tool()
async def web_download(
    ctx: Context,
    url: str,
    filename: str | None = None,
) -> DownloadResult:
    """Download a file or save a web page from a URL.

    Tries three strategies automatically: httpx streaming (fastest),
    Playwright page save (for 403-blocked or JS-rendered pages), and
    Playwright download event (for JS-triggered file downloads).

    Handles PDFs, Word/Excel documents, CSVs, ZIPs, images, and web pages.

    Args:
        url: The file or page URL to download.
        filename: Optional output filename. Auto-derived from URL if not provided.

    Returns:
        DownloadResult with the saved file path, size, content type, and status.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.download(url, filename=filename)


@mcp.tool()
async def web_screenshot(
    ctx: Context,
    url: str,
    full_page: bool = False,
    path: str | None = None,
) -> ScreenshotResult:
    """Take a screenshot of a web page.

    Args:
        url: The URL to screenshot.
        full_page: If True, capture the full scrollable page; otherwise viewport only.
        path: Optional output file path. Auto-generated in the configured screenshot
            directory if not provided.

    Returns:
        ScreenshotResult with the file path and image size.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.screenshot(url, path=path, full_page=full_page)


@mcp.tool()
async def web_interact(
    ctx: Context,
    url: str,
    actions: list[dict],
    stop_on_error: bool = True,
) -> ActionSequenceResult:
    """Execute a scripted sequence of browser actions on a URL.

    Supports 12 action types: click, type, fill, scroll, screenshot, navigate,
    dialog, hover, select, keyboard, wait, evaluate. Each action is a dict with
    an ``action`` discriminator field and action-specific parameters.

    Example actions::

        [
          {"action": "wait", "target": "selector", "value": "h1"},
          {"action": "fill", "selector": "#search", "value": "query"},
          {"action": "click", "selector": "button[type=submit]"},
          {"action": "screenshot", "full_page": true},
          {"action": "evaluate", "expression": "document.title"}
        ]

    Args:
        url: Starting URL to navigate to.
        actions: Ordered list of action dicts to execute.
        stop_on_error: If True, halt the sequence on first failure and mark
            remaining actions as SKIPPED.

    Returns:
        ActionSequenceResult with per-action results and aggregate counts.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    # Validate and parse the raw action dicts into the discriminated union
    adapter = TypeAdapter(list[Action])
    parsed_actions = adapter.validate_python(actions)
    return await agent.interact(url, parsed_actions, stop_on_error=stop_on_error)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server on stdio transport (for local MCP client connections)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
