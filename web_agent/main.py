"""CLI entry point for the web_agent toolkit.

Subcommands: search, fetch, download, interact, screenshot.

Usage::

    python -m web_agent search "Python web scraping" --max-results 5
    python -m web_agent fetch "https://example.com"
    python -m web_agent download "https://example.com/file.pdf"
    python -m web_agent screenshot "https://example.com" --full-page
    python -m web_agent interact "https://example.com" --actions actions.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from loguru import logger
from pydantic import TypeAdapter

from .agent import Agent
from .config import AppConfig
from .models import Action


def _load_config(args: argparse.Namespace) -> AppConfig:
    """Load config from YAML if provided, otherwise use defaults."""
    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            return AppConfig.from_yaml(config_path)
        logger.warning("Config file {p} not found, using defaults", p=config_path)
    return AppConfig()


def setup_logging(level: str) -> None:
    """Configure loguru with the given level and a clean format."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )


# ------------------------------------------------------------------
# Subcommand handlers
# ------------------------------------------------------------------


async def run_search(args: argparse.Namespace) -> None:
    """Execute the search-and-extract pipeline."""
    config = _load_config(args)
    setup_logging(config.log_level)

    async with Agent(config) as agent:
        result = await agent.search_and_extract(args.query, args.max_results)
        output = await agent.save_results(result, args.output)

        print(f"\nResults saved to: {output}")
        print(f"Pages extracted: {len(result.pages)}")
        if result.errors:
            print(f"Errors ({len(result.errors)}):")
            for e in result.errors:
                print(f"  - {e}")


async def run_fetch(args: argparse.Namespace) -> None:
    """Fetch and extract a single URL."""
    config = _load_config(args)
    setup_logging(config.log_level)

    async with Agent(config) as agent:
        result = await agent.fetch_and_extract(args.url)
        print(result.model_dump_json(indent=2))


async def run_download(args: argparse.Namespace) -> None:
    """Download a file from a URL."""
    config = _load_config(args)
    setup_logging(config.log_level)

    async with Agent(config) as agent:
        result = await agent.download(args.url, args.filename)
        print(result.model_dump_json(indent=2))


async def run_interact(args: argparse.Namespace) -> None:
    """Execute a scripted sequence of browser actions."""
    config = _load_config(args)
    setup_logging(config.log_level)

    actions_path = Path(args.actions)
    raw_actions = json.loads(actions_path.read_text(encoding="utf-8"))
    adapter = TypeAdapter(list[Action])
    actions = adapter.validate_python(raw_actions)

    async with Agent(config) as agent:
        result = await agent.interact(args.url, actions, stop_on_error=not args.no_stop_on_error)
        print(result.model_dump_json(indent=2))


async def run_screenshot(args: argparse.Namespace) -> None:
    """Take a screenshot of a URL."""
    config = _load_config(args)
    setup_logging(config.log_level)

    async with Agent(config) as agent:
        result = await agent.screenshot(args.url, path=args.output, full_page=args.full_page)
        print(result.model_dump_json(indent=2))


def run_serve_mcp(args: argparse.Namespace) -> None:
    """Run the MCP server on stdio transport."""
    # Import lazily so users without mcp installed can still use other commands
    from .mcp_server import main as mcp_main

    mcp_main()


async def run_observe(args: argparse.Namespace) -> None:
    """Capture an observe snapshot for a URL: screenshot + dimensions + DPR."""
    config = _load_config(args)
    setup_logging(config.log_level)

    async with Agent(config) as agent:
        obs = await agent.observe(
            args.url,
            include_text=not args.no_text,
            include_aria=args.aria,
        )
        print(obs.model_dump_json(indent=2))


async def run_doctor(args: argparse.Namespace) -> None:
    """Run web_agent self-diagnostic and print the report."""
    # Doctor doesn't need a running Agent or Browser, but it does need
    # a config to know which paths to probe. Build one without starting Agent.
    config = _load_config(args)
    setup_logging(config.log_level)

    from .doctor import format_report_human
    from .doctor import run_doctor as _run_doctor

    report = await _run_doctor(config, quick=args.quick)
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(format_report_human(report))

    # Exit non-zero if anything failed so CI can gate on `web-agent doctor`.
    if report.summary == "unusable":
        raise SystemExit(2)


# ------------------------------------------------------------------
# CLI parser
# ------------------------------------------------------------------


def main() -> None:
    """Parse arguments and dispatch to the appropriate async handler."""
    parser = argparse.ArgumentParser(
        prog="web-agent",
        description="web_agent -- Agentic web search, fetch, download, extraction, and browser automation toolkit",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (optional, uses defaults if not provided)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # search
    sp_search = subparsers.add_parser(
        "search", help="Search the web and extract content from top results"
    )
    sp_search.add_argument("query", help="Search query string")
    sp_search.add_argument("--max-results", type=int, default=None)
    sp_search.add_argument("--output", default=None, help="Output JSON path")

    # fetch
    sp_fetch = subparsers.add_parser("fetch", help="Fetch and extract content from a single URL")
    sp_fetch.add_argument("url", help="URL to fetch")

    # download
    sp_dl = subparsers.add_parser("download", help="Download a file from a URL")
    sp_dl.add_argument("url", help="File URL to download")
    sp_dl.add_argument("--filename", default=None, help="Save as filename")

    # interact
    sp_interact = subparsers.add_parser("interact", help="Run a scripted browser action sequence")
    sp_interact.add_argument("url", help="Starting URL")
    sp_interact.add_argument(
        "--actions", required=True, help="Path to JSON file with action sequence"
    )
    sp_interact.add_argument(
        "--no-stop-on-error",
        action="store_true",
        help="Continue executing actions after a failure",
    )

    # screenshot
    sp_ss = subparsers.add_parser("screenshot", help="Take a screenshot of a URL")
    sp_ss.add_argument("url", help="URL to screenshot")
    sp_ss.add_argument("--output", default=None, help="Output file path")
    sp_ss.add_argument("--full-page", action="store_true", help="Capture full scrollable page")

    # serve-mcp
    subparsers.add_parser(
        "serve-mcp",
        help="Run as an MCP server (stdio transport) for Claude Desktop/Code, Cursor, etc.",
    )

    # v1.6.6: observe (Feature 5)
    sp_observe = subparsers.add_parser(
        "observe", help="Capture a page's screenshot + viewport / scroll / DPR snapshot"
    )
    sp_observe.add_argument("url", help="URL to observe")
    sp_observe.add_argument(
        "--no-text", action="store_true", help="Skip document.body.innerText capture"
    )
    sp_observe.add_argument(
        "--aria", action="store_true", help="Include accessibility snapshot (may be large)"
    )

    # v1.6.6: doctor (Feature 6)
    sp_doctor = subparsers.add_parser(
        "doctor",
        help="Self-diagnostic: Playwright, Chromium, MCP, binary extras, dirs, network",
    )
    sp_doctor.add_argument(
        "--quick", action="store_true", help="Skip the slow chromium launch probe"
    )
    sp_doctor.add_argument(
        "--json", action="store_true", help="Emit DoctorReport as JSON (for CI)"
    )

    args = parser.parse_args()

    # serve-mcp is synchronous (it manages its own event loop internally)
    if args.command == "serve-mcp":
        run_serve_mcp(args)
        return

    handler_map = {
        "search": run_search,
        "fetch": run_fetch,
        "download": run_download,
        "interact": run_interact,
        "screenshot": run_screenshot,
        "observe": run_observe,
        "doctor": run_doctor,
    }
    asyncio.run(handler_map[args.command](args))


if __name__ == "__main__":
    main()
