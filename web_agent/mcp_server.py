"""MCP (Model Context Protocol) server exposing web_agent as tools for AI clients.

Runs as an MCP server that Claude Desktop, Claude Code, Cursor, and other
MCP-compatible clients can connect to. Exposes 12 tools:

**Single-shot tools**:
- ``web_search`` -- search + extract top results
- ``web_fetch`` -- fetch and extract a single URL (smart binary routing)
- ``web_download`` -- download a file or save a web page
- ``web_screenshot`` -- screenshot a page
- ``web_interact`` -- run a scripted browser action sequence

**High-level recipes**:
- ``web_search_best`` -- search and open the best-ranked result
- ``web_find_and_download`` -- search and download the first matching file
- ``web_research`` -- multi-page research with structured citations
- ``web_fill_form_and_extract`` -- open page, fill search/filter form, extract content

**Browser sessions** -- retain cookies/login across multiple tool calls:
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

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

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
    FormFilterSpec,
    ResearchResult,
    ScreenshotResult,
)

# ---------------------------------------------------------------------------
# Lifespan: initialize the Agent once per MCP session (browser stays warm)
# ---------------------------------------------------------------------------


def _load_mcp_config() -> AppConfig:
    """Load AppConfig for the MCP server.

    Resolution order:
      1. ``WEB_AGENT_CONFIG`` env var pointing at a YAML file -- enables
         operator-supplied allow/deny lists, ``safe_mode``, custom
         ranking profiles, etc., without code changes.
      2. ``AppConfig()`` defaults, which already pick up
         ``WEB_AGENT_*`` env vars (incl. nested ``__`` paths since
         v1.6.5) thanks to pydantic-settings.

    A missing or unreadable YAML file is logged at WARNING and falls
    back to defaults rather than failing the server start -- matches
    the CLI's tolerance for missing config.
    """
    yaml_path = os.environ.get("WEB_AGENT_CONFIG")
    if yaml_path:
        from pathlib import Path

        path = Path(yaml_path)
        if path.exists():
            try:
                logger.info("Loading MCP config from {p}", p=path)
                return AppConfig.from_yaml(path)
            except Exception as exc:
                logger.warning(
                    "Failed to load WEB_AGENT_CONFIG={p}: {e}; using defaults",
                    p=path,
                    e=exc,
                )
        else:
            logger.warning(
                "WEB_AGENT_CONFIG={p} not found; using defaults", p=path
            )
    return AppConfig()


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize the web_agent Agent once and share it across all tool calls."""
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting web_agent MCP server...")
    config = _load_mcp_config()
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
    extract_files: bool = False,
) -> AgentResult:
    """Search the web and extract content from the top results.

    Uses a configurable search provider chain (default: SearXNG ->
    DDGS -> Playwright browser-driven Google + DDG HTML fallback).
    Each result page is fetched and its main content extracted
    (title, description, text, markdown).

    Args:
        query: The search query string. May also be a search-engine SERP
            URL (Google/Bing/DDG/Brave/SearX) -- it will be unwrapped.
        max_results: Maximum number of results to process (default 10, max ~20).
        session_id: Optional persistent browser session for the page fetches.
        extract_files: If True, route PDF/XLSX/DOCX/CSV results through the
            binary extractor inline instead of surfacing them in
            ``download_candidates``. Requires the ``[binary]`` extra.

    Returns:
        AgentResult with search metadata, extracted page contents, structured
        warnings/errors, download_candidates, and per-URL diagnostics.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.search_and_extract(
        query,
        max_results=max_results,
        session_id=session_id,
        extract_files=extract_files,
    )


@mcp.tool()
async def web_fetch(
    ctx: Context,
    url: str,
    session_id: Optional[str] = None,
    binary_probe: bool = True,
) -> ExtractionResult:
    """Fetch a single URL and extract its main content.

    Smart routing (NEW in 1.6.2): URLs with known download extensions
    (.pdf, .xlsx, .docx, .csv, ...) go through the binary extractor.
    With ``binary_probe=True``, extensionless URLs are HEAD-probed for
    Content-Type / Content-Disposition to detect document downloads.
    Otherwise renders JavaScript-heavy pages in a real browser and
    extracts via the three-tier HTML chain.

    Args:
        url: The URL to fetch.
        session_id: Optional persistent browser session.
        binary_probe: When True, send a HEAD request for extensionless
            URLs to detect binary documents served via headers.

    Returns:
        ExtractionResult with title, content, metadata, and extraction
        method used (``trafilatura`` | ``bs4`` | ``raw`` | ``pdf`` |
        ``xlsx`` | ``docx`` | ``csv`` | ``none``).
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.fetch_and_extract(url, session_id=session_id, binary_probe=binary_probe)


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
    return await agent.screenshot(url, path=path, full_page=full_page, session_id=session_id)


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
    prefer_domains: Optional[list[str]] = None,
    domain_profile: Optional[str] = None,
) -> ExtractionResult:
    """Search the web, rank results, and return the extracted content of the top hit.

    Skips the manual "search -> pick result -> fetch" dance: ranks results by
    query overlap + HTTPS bonus + well-known domain bonus + caller-supplied
    domain hints + position tiebreaker, then fetches and extracts the top URL.

    Args:
        query: The search query.
        ranking: Ranking scheme (``default`` | ``overlap`` | ``position``).
        session_id: Optional persistent browser session.
        prefer_domains: Optional caller-supplied host hints (e.g.
            ``["ec.europa.eu"]``); matching results get a strong bonus.
        domain_profile: Optional named ranking profile -- one of
            ``"official_sources" | "docs" | "research" | "news" | "files"``.

    Returns:
        ExtractionResult of the top-ranked URL.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.search_and_open_best_result(
        query,
        ranking=ranking,
        session_id=session_id,
        prefer_domains=prefer_domains,
        domain_profile=domain_profile,
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
    return await agent.find_and_download_file(query, file_types=file_types, session_id=session_id)


@mcp.tool()
async def web_research(
    ctx: Context,
    query: str,
    max_pages: int = 5,
    depth: int = 1,
    session_id: Optional[str] = None,
    prefer_domains: Optional[list[str]] = None,
    domain_profile: Optional[str] = None,
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
        prefer_domains: Optional caller-supplied host hints; matching results
            get a strong ranking bonus.
        domain_profile: Optional named ranking profile -- one of
            ``"official_sources" | "docs" | "research" | "news" | "files"``.

    Returns:
        ResearchResult with citations, summary_pages, budget telemetry,
        warnings/errors (string + structured), download_candidates, and
        per-URL diagnostics.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.web_research(
        query,
        depth=depth,
        max_pages=max_pages,
        session_id=session_id,
        prefer_domains=prefer_domains,
        domain_profile=domain_profile,
    )


@mcp.tool()
async def web_fill_form_and_extract(
    ctx: Context,
    url: str,
    spec: FormFilterSpec,
    session_id: Optional[str] = None,
) -> ExtractionResult:
    """Open a URL, fill a search/filter form, then extract post-submit content.

    Targets dynamic calendar / regulator-filings / event-listing pages where
    content is gated behind a search box and/or filter controls. The caller
    supplies semantic locators in ``spec`` (query input, filters, submit button,
    wait_for); the recipe runs the actions and returns the extracted content.

    Args:
        url: The page URL hosting the form.
        spec: Declarative FormFilterSpec describing the form interaction.
        session_id: Optional persistent session (e.g. for authenticated dashboards).

    Returns:
        ExtractionResult of the post-submit page. ``extraction_method="none"``
        when locators don't resolve or the form times out.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    return await agent.fill_form_and_extract(url, spec, session_id=session_id)


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
# v1.6.6 Feature 3: Tab management
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_list_tabs(ctx: Context, session_id: str) -> dict:
    """List every tab in a browser session.

    Tabs are created either explicitly via ``web_new_tab`` or
    automatically when the page opens a popup / target=_blank link.
    The ``active`` tab is the one ``web_interact`` / ``web_observe``
    target by default.

    Args:
        session_id: The session_id from ``create_browser_session``.

    Returns:
        Dict with ``count`` and ``tabs`` (list of TabInfo dicts).
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    tabs = await agent.list_tabs(session_id)
    return {
        "count": len(tabs),
        "tabs": [t.model_dump(mode="json") for t in tabs],
    }


@mcp.tool()
async def web_current_tab(ctx: Context, session_id: str) -> dict:
    """Return the active tab of a session, or ``{"tab": null}`` if none."""
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    t = await agent.current_tab(session_id)
    return {"tab": t.model_dump(mode="json") if t else None}


@mcp.tool()
async def web_new_tab(
    ctx: Context, session_id: str, url: Optional[str] = None
) -> dict:
    """Open a fresh tab in a session.

    The new tab becomes the active tab. If ``url`` is provided, the tab
    navigates to it. Use ``web_list_tabs`` to see the new tab_id.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    tab_id = await agent.new_tab(url=url, session_id=session_id)
    return {"tab_id": tab_id, "session_id": session_id, "url": url}


@mcp.tool()
async def web_switch_tab(ctx: Context, session_id: str, tab_id: str) -> dict:
    """Make ``tab_id`` the active tab. Brings it to front when possible."""
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    await agent.switch_tab(tab_id, session_id=session_id)
    return {"switched": True, "tab_id": tab_id, "session_id": session_id}


@mcp.tool()
async def web_close_tab(ctx: Context, session_id: str, tab_id: str) -> dict:
    """Close a tab. If it was the active tab, another becomes active."""
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    await agent.close_tab(tab_id, session_id=session_id)
    return {"closed": True, "tab_id": tab_id, "session_id": session_id}


# ---------------------------------------------------------------------------
# v1.6.6 Feature 4: coordinate-level fallback actions
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_click_xy(
    ctx: Context,
    session_id: str,
    x: float,
    y: float,
    tab_id: Optional[str] = None,
    button: str = "left",
    clicks: int = 1,
    delay: int = 0,
) -> dict:
    """Click at viewport coordinates (CSS pixels) on a session's tab.

    Use after ``web_observe`` returns ``device_pixel_ratio`` so you can
    map screenshot pixels to CSS pixels (Playwright's mouse API expects
    CSS pixels, not device pixels). Bypasses selector resolution.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.click_xy(
        x,
        y,
        session_id=session_id,
        tab_id=tab_id,
        button=button,
        clicks=clicks,
        delay=delay,
    )
    return r.model_dump(mode="json")


@mcp.tool()
async def web_type_text(
    ctx: Context,
    session_id: str,
    text: str,
    tab_id: Optional[str] = None,
    delay: int = 0,
) -> dict:
    """Type ``text`` into whatever currently has keyboard focus.

    Pair with a preceding ``web_click_xy`` to direct keystrokes at the
    right element. Requires a live session.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.type_text(text, session_id=session_id, tab_id=tab_id, delay=delay)
    return r.model_dump(mode="json")


@mcp.tool()
async def web_press_key(
    ctx: Context,
    session_id: str,
    key: str,
    tab_id: Optional[str] = None,
    modifiers: Optional[list[str]] = None,
) -> dict:
    """Press a key (with optional modifiers) at page level.

    Modifiers: ``Shift``, ``Control``, ``Alt``, ``Meta``. The combo
    ``Control+a`` works as either ``key="Control+a"`` or
    ``key="a", modifiers=["Control"]``.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.press_key(
        key, session_id=session_id, tab_id=tab_id, modifiers=modifiers
    )
    return r.model_dump(mode="json")


# ---------------------------------------------------------------------------
# v1.6.6 Feature 5: observe mode
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_observe(
    ctx: Context,
    url: Optional[str] = None,
    session_id: Optional[str] = None,
    tab_id: Optional[str] = None,
    include_text: bool = True,
    include_aria: bool = False,
) -> dict:
    """Capture a page's visual + structural state for observe-act-verify loops.

    Returns a screenshot path, viewport / page dimensions, scroll
    position, device pixel ratio, plus optional visible text and ARIA
    accessibility tree. Use the DPR to map screenshot pixels to the
    CSS pixels that ``web_click_xy`` expects.

    Args:
        url: Open this URL (ephemeral page if no session_id, or
            navigate the session's current tab to it). Optional when
            session_id is given -- omit to observe current state.
        session_id: Live session whose tab to observe.
        tab_id: Specific tab to observe within the session.
        include_text: Capture document.body.innerText (truncated to
            safety.max_chars_per_call). Default True.
        include_aria: Capture page.accessibility.snapshot(). Default
            False (snapshots can be megabytes).
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    obs = await agent.observe(
        url,
        session_id=session_id,
        tab_id=tab_id,
        include_text=include_text,
        include_aria=include_aria,
    )
    return obs.model_dump(mode="json")


# ---------------------------------------------------------------------------
# v1.6.6 Feature 6: doctor
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_doctor(ctx: Context, quick: bool = False) -> dict:
    """Self-diagnostic for the web_agent install.

    Probes Python + web_agent versions, Playwright + Chromium install,
    optional providers (DDGS, SearXNG), MCP, binary extras
    (pypdf/openpyxl/python-docx), directory writability, and basic
    network connectivity. Bypasses SafetyConfig.

    Args:
        quick: Skip the actual chromium launch test (~3-5s).

    Returns:
        DoctorReport as a dict with ``summary`` (healthy /
        usable_with_warnings / unusable) and per-check details.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    report = await agent.doctor(quick=quick)
    return report.model_dump(mode="json")


# ---------------------------------------------------------------------------
# v1.6.6 Feature 2: CDP endpoint
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_get_cdp_endpoint(ctx: Context) -> dict:
    """Return the Chrome DevTools Protocol endpoint of the webTool-launched browser.

    Returns ``{"enabled": false, "endpoint": null}`` when
    ``browser.cdp_enabled=False``. External CDP tools (chrome://inspect,
    custom debuggers, browser-use, playwright-inspector) can connect to
    the returned ws:// URL. webTool itself never attaches to other
    endpoints -- this is the only browser it controls.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    cfg = agent._config.browser
    endpoint = agent.get_cdp_endpoint()
    return {
        "enabled": cfg.cdp_enabled,
        "endpoint": endpoint,
        "host": cfg.cdp_host,
        "port": cfg.cdp_port,
    }


# ---------------------------------------------------------------------------
# v1.6.7: Domain Skills (Features 1+2+3)
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_domain_skills(ctx: Context) -> dict:
    """List every domain skill registered with the Agent's SkillRegistry.

    Skills come from three tiers (priority: project > workspace >
    builtin). Returns both runnable bundled skills (those with a
    Python runner) and informational-only user markdown skills.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    skills = agent.list_domain_skills()
    return {
        "count": len(skills),
        "skills": [s.model_dump(mode="json") for s in skills],
    }


@mcp.tool()
async def get_domain_skill(ctx: Context, url: str, name: str) -> dict:
    """Get the parsed skill for a given (URL host, name) tuple.

    Skills are looked up by host suffix match against ``url``. When
    multiple skills match (e.g. nested domains), the most-specific
    one (longest registered domain) is returned. Returns
    ``{"skill": null}`` when no match.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    matches = agent.get_domain_skills(url)
    # Filter by name
    chosen = None
    for s in matches:
        if s.name == name and (chosen is None or len(s.domain) > len(chosen.domain)):
            chosen = s
    return {"skill": chosen.model_dump(mode="json") if chosen else None}


@mcp.tool()
async def apply_domain_skill(
    ctx: Context,
    url: str,
    name: str,
    inputs: Optional[dict] = None,
) -> dict:
    """Run a bundled domain skill against ``url`` with ``inputs``.

    Only bundled (Python-backed) skills are dispatchable. User markdown
    skills are informational; this tool raises ``SkillNotRunnableError``
    for them -- callers should use ``get_domain_skill`` to read the
    instructions and act on them with the standard Agent primitives.

    Inputs are validated against the skill's frontmatter schema before
    the runner is invoked.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    result = await agent.apply_domain_skill(url, name, inputs or {})
    return result.model_dump(mode="json")


# ---------------------------------------------------------------------------
# v1.6.7: Interaction Library (Feature 5)
# ---------------------------------------------------------------------------


@mcp.tool()
async def web_handle_dialog(
    ctx: Context,
    session_id: str,
    action: str = "accept",
    prompt_text: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> dict:
    """Pre-arm the next browser dialog (alert/confirm/prompt) handler.

    Subsequent dialogs on the session's tab will receive ``action``
    (``accept`` or ``dismiss``) automatically. For prompt dialogs,
    ``prompt_text`` is the response.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.handle_dialog(
        action, prompt_text=prompt_text, session_id=session_id, tab_id=tab_id
    )
    return r.model_dump(mode="json")


@mcp.tool()
async def web_select_dropdown(
    ctx: Context,
    session_id: str,
    selector: str,
    value: Optional[str] = None,
    label: Optional[str] = None,
    index: Optional[int] = None,
    tab_id: Optional[str] = None,
) -> dict:
    """Select an option from a ``<select>`` element. Pass exactly one of
    value / label / index."""
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.select_dropdown(
        selector,
        session_id=session_id,
        tab_id=tab_id,
        value=value,
        label=label,
        index=index,
    )
    return r.model_dump(mode="json")


@mcp.tool()
async def web_upload_file(
    ctx: Context,
    session_id: str,
    selector: str,
    paths: list[str],
    tab_id: Optional[str] = None,
) -> dict:
    """Upload one or more files to a file input element.

    Paths default to those under ``download.download_dir``; set
    ``safety.allow_upload_outside_download_dir=True`` to widen.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.upload_file(
        selector, paths, session_id=session_id, tab_id=tab_id
    )
    return r.model_dump(mode="json")


@mcp.tool()
async def web_drag_and_drop(
    ctx: Context,
    session_id: str,
    source: str,
    target: str,
    tab_id: Optional[str] = None,
) -> dict:
    """Drag an element from ``source`` and drop on ``target``."""
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.drag_and_drop(
        source, target, session_id=session_id, tab_id=tab_id
    )
    return r.model_dump(mode="json")


@mcp.tool()
async def web_scroll_until_text(
    ctx: Context,
    session_id: str,
    text: str,
    tab_id: Optional[str] = None,
    max_scrolls: int = 10,
    scroll_step: int = 800,
) -> dict:
    """Scroll the session's tab until ``text`` is visible (or
    ``max_scrolls`` is reached). Useful for infinite-scroll feeds."""
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.scroll_until_text(
        text,
        session_id=session_id,
        tab_id=tab_id,
        max_scrolls=max_scrolls,
        scroll_step=scroll_step,
    )
    return r.model_dump(mode="json")


@mcp.tool()
async def web_click_inside_iframe(
    ctx: Context,
    session_id: str,
    iframe_selector: str,
    inner_selector: str,
    tab_id: Optional[str] = None,
) -> dict:
    """Click a button inside a same-origin iframe via Playwright's
    frame_locator."""
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.click_inside_iframe(
        iframe_selector, inner_selector, session_id=session_id, tab_id=tab_id
    )
    return r.model_dump(mode="json")


@mcp.tool()
async def web_click_shadow_dom(
    ctx: Context,
    session_id: str,
    host_selector: str,
    inner_selector: str,
    tab_id: Optional[str] = None,
) -> dict:
    """Click an element inside a shadow DOM tree via the pierce
    combinator (``host >> inner``)."""
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.click_shadow_dom(
        host_selector, inner_selector, session_id=session_id, tab_id=tab_id
    )
    return r.model_dump(mode="json")


@mcp.tool()
async def web_print_page_as_pdf(
    ctx: Context,
    url: Optional[str] = None,
    output_path: Optional[str] = None,
    session_id: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> dict:
    """Render the current page (or ``url``) as PDF via Chromium's
    ``page.pdf()``. Output path defaults to ``automation.screenshot_dir``.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.print_page_as_pdf(
        url=url,
        output_path=output_path,
        session_id=session_id,
        tab_id=tab_id,
    )
    return r.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server on stdio transport (for local MCP client connections)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
