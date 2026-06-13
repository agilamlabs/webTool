"""v1.6.6 self-diagnostic / doctor command.

Module-level async ``_check_*`` functions. Each wraps its work in a 5s
``asyncio.wait_for`` and catches Exception so the run never crashes.

The aggregator :func:`run_doctor` collects all probe results into a
:class:`DoctorReport` with a top-level ``summary`` of healthy /
usable_with_warnings / unusable.

Doctor bypasses :class:`SafetyConfig` and ``Agent._call_scope`` audit by
design -- it's a capability self-check, not a regular agent operation.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import platform
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .config import AppConfig
from .models import DoctorCheck, DoctorReport

# Per-check time budget. The total run is at most ~14 * 5s = 70s but in
# practice most checks complete in <100ms. The slow probe is the actual
# chromium launch (~3-5s) which is opt-out via ``quick=True``.
_PER_CHECK_TIMEOUT_S = 5.0


async def _timed(
    name: str,
    fn: Callable[..., Awaitable[DoctorCheck]],
    *args: Any,
    **kwargs: Any,
) -> DoctorCheck:
    """Helper: run ``fn`` with a per-check timeout, catch any exception,
    and produce a DoctorCheck."""
    start = time.perf_counter()
    try:
        check: DoctorCheck = await asyncio.wait_for(
            fn(*args, **kwargs), timeout=_PER_CHECK_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        check = DoctorCheck(
            name=name,
            status="fail",
            message=f"Probe exceeded {_PER_CHECK_TIMEOUT_S:.0f}s timeout",
        )
    except Exception as exc:
        check = DoctorCheck(name=name, status="fail", message=f"Probe raised: {exc}")
    check.duration_ms = (time.perf_counter() - start) * 1000
    return check


# ----------------------------------------------------------------------
# Individual probes
# ----------------------------------------------------------------------


async def _check_python_version(_cfg: AppConfig) -> DoctorCheck:
    v = sys.version_info
    if v >= (3, 10):
        return DoctorCheck(
            name="python_version",
            status="ok",
            message=f"Python {v.major}.{v.minor}.{v.micro}",
        )
    return DoctorCheck(
        name="python_version",
        status="fail",
        message=f"Python {v.major}.{v.minor}.{v.micro} is below the supported 3.10",
    )


async def _check_package_version(_cfg: AppConfig) -> DoctorCheck:
    try:
        from . import __version__
    except Exception as exc:  # pragma: no cover -- import_error edge
        return DoctorCheck(
            name="package_version",
            status="fail",
            message=f"web_agent.__version__ unavailable: {exc}",
        )
    return DoctorCheck(name="package_version", status="ok", message=f"web_agent {__version__}")


async def _check_playwright_import(_cfg: AppConfig) -> DoctorCheck:
    try:
        import playwright

        version = getattr(playwright, "__version__", "unknown")
    except Exception as exc:
        return DoctorCheck(
            name="playwright_import",
            status="fail",
            message=f"playwright import failed: {exc}",
        )
    return DoctorCheck(name="playwright_import", status="ok", message=f"playwright {version}")


async def _resolve_chromium_executable() -> Path:
    """Resolve the Chromium browser executable Playwright would launch,
    WITHOUT launching a browser.

    Starts the Playwright driver (the node sidecar, typically a few
    hundred ms) and reads ``BrowserType.executable_path`` -- the
    authoritative answer across Playwright versions and
    ``PLAYWRIGHT_BROWSERS_PATH`` overrides. Module-level so tests can
    monkeypatch the probe.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        return Path(pw.chromium.executable_path)


async def _check_chromium_installed(_cfg: AppConfig) -> DoctorCheck:
    """Verify the Chromium BROWSER executable exists -- without launching.

    v1.7.0: the previous probe only checked Playwright's bundled node
    driver, which exists whenever the ``playwright`` wheel is installed
    -- so ``doctor --quick`` reported healthy on machines where
    ``playwright install chromium`` was never run. The probe now
    resolves ``chromium.executable_path`` through the driver (no
    browser launch) and stats the file. Shared by quick and full modes;
    full mode additionally performs the real launch probe.
    """
    try:
        exe = await _resolve_chromium_executable()
    except Exception as exc:
        return DoctorCheck(
            name="chromium_installed",
            status="fail",
            message=(
                f"Cannot resolve Chromium executable: {exc}. "
                "Run: python -m playwright install chromium"
            ),
        )
    if str(exe) and exe.exists():
        return DoctorCheck(
            name="chromium_installed", status="ok", message=f"Chromium executable: {exe}"
        )
    return DoctorCheck(
        name="chromium_installed",
        status="fail",
        message=(
            f"Chromium executable not found at {exe}. "
            "Run: python -m playwright install chromium"
        ),
    )


async def _check_browser_launch(_cfg: AppConfig) -> DoctorCheck:
    """Actually launch headless Chromium and close it.

    Slow probe (~3-5s cold). Opt-out via ``quick=True``.
    """
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            await browser.close()
    except Exception as exc:
        return DoctorCheck(
            name="browser_launch",
            status="fail",
            message=f"chromium.launch failed: {exc}",
        )
    return DoctorCheck(
        name="browser_launch", status="ok", message="headless chromium launched + closed"
    )


async def _check_ddgs_import(_cfg: AppConfig) -> DoctorCheck:
    try:
        importlib.import_module("ddgs")
    except Exception as exc:
        return DoctorCheck(
            name="ddgs_import",
            status="warn",
            message=f"ddgs not available: {exc} (DuckDuckGo provider will be skipped)",
        )
    return DoctorCheck(name="ddgs_import", status="ok", message="ddgs available")


async def _check_searxng_reachable(cfg: AppConfig) -> DoctorCheck:
    base = cfg.search.searxng_base_url
    if not base:
        return DoctorCheck(
            name="searxng_reachable",
            status="skip",
            message="No searxng_base_url configured",
        )
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(base)
            if r.status_code < 500:
                return DoctorCheck(
                    name="searxng_reachable",
                    status="ok",
                    message=f"{base} -> HTTP {r.status_code}",
                )
            return DoctorCheck(
                name="searxng_reachable",
                status="warn",
                message=f"{base} -> HTTP {r.status_code} (server error)",
            )
    except Exception as exc:
        return DoctorCheck(
            name="searxng_reachable",
            status="warn",
            message=f"{base} unreachable: {exc}",
        )


async def _check_mcp_import(_cfg: AppConfig) -> DoctorCheck:
    try:
        importlib.import_module("mcp.server.fastmcp")
    except Exception as exc:
        return DoctorCheck(
            name="mcp_import",
            status="warn",
            message=f"mcp.server.fastmcp not available: {exc} (MCP server cannot start)",
        )
    return DoctorCheck(name="mcp_import", status="ok", message="FastMCP importable")


async def _check_binary_extra(name: str, module: str) -> DoctorCheck:
    """Helper for the three binary-extraction extras."""
    try:
        importlib.import_module(module)
    except Exception as exc:
        return DoctorCheck(
            name=f"binary_extra_{name}",
            status="warn",
            message=f"{module} not available: {exc} ({name} extraction disabled)",
        )
    return DoctorCheck(name=f"binary_extra_{name}", status="ok", message=f"{module} importable")


async def _check_dir_writable(name: str, path: str) -> DoctorCheck:
    """Write + delete a probe file. Detects permission and existence issues."""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        probe = Path(path) / f".doctor-probe-{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        return DoctorCheck(
            name=f"dir_writable_{name}",
            status="fail",
            message=f"{path}: {exc}",
        )
    return DoctorCheck(name=f"dir_writable_{name}", status="ok", message=f"{path} is writable")


async def _check_network_connectivity(_cfg: AppConfig) -> DoctorCheck:
    """Single GET to a stable, well-known host."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get("https://www.example.com/")
            if r.status_code == 200:
                return DoctorCheck(
                    name="network_connectivity",
                    status="ok",
                    message="https://www.example.com/ reachable (200)",
                )
            return DoctorCheck(
                name="network_connectivity",
                status="warn",
                message=f"https://www.example.com/ returned HTTP {r.status_code}",
            )
    except Exception as exc:
        return DoctorCheck(
            name="network_connectivity",
            status="warn",
            message=f"outbound HTTPS failed: {exc}",
        )


async def _check_robots_rate_sanity(cfg: AppConfig) -> DoctorCheck:
    safety = cfg.safety
    issues: list[str] = []
    rps = getattr(safety, "rate_limit_per_host_rps", 0)
    if rps < 0:
        issues.append(f"rate_limit_per_host_rps={rps} is negative")
    ua = getattr(safety, "robots_user_agent", "")
    if isinstance(ua, str) and not ua.strip():
        issues.append("robots_user_agent is empty")
    if issues:
        return DoctorCheck(
            name="robots_rate_sanity",
            status="warn",
            message="; ".join(issues),
        )
    return DoctorCheck(
        name="robots_rate_sanity",
        status="ok",
        message="rate-limit + robots config look sane",
    )


async def _check_config_file_parse(_cfg: AppConfig) -> DoctorCheck:
    yaml_path = os.environ.get("WEB_AGENT_CONFIG")
    if not yaml_path:
        return DoctorCheck(
            name="config_file_parse",
            status="skip",
            message="No WEB_AGENT_CONFIG env var set",
        )
    p = Path(yaml_path)
    if not p.exists():
        return DoctorCheck(
            name="config_file_parse",
            status="fail",
            message=f"WEB_AGENT_CONFIG={p} does not exist",
        )
    try:
        AppConfig.from_yaml(p)
    except Exception as exc:
        return DoctorCheck(
            name="config_file_parse",
            status="fail",
            message=f"{p}: {exc}",
        )
    return DoctorCheck(
        name="config_file_parse",
        status="ok",
        message=f"{p} parses cleanly",
    )


# ----------------------------------------------------------------------
# Aggregator
# ----------------------------------------------------------------------


def _summarize(checks: list[DoctorCheck]) -> str:
    if any(c.status == "fail" for c in checks):
        return "unusable"
    if any(c.status == "warn" for c in checks):
        return "usable_with_warnings"
    return "healthy"


async def run_doctor(config: AppConfig, *, quick: bool = False) -> DoctorReport:
    """Run every probe and return a :class:`DoctorReport`.

    Args:
        config: The AppConfig to probe against (used for searxng URL,
            directory paths, and YAML location).
        quick: When True, skips ``_check_browser_launch`` (the slow
            probe). The driver-on-disk check still runs, so a missing
            Chromium is still surfaced -- just without the launch cost.

    The function never raises; every internal failure becomes a
    ``fail``-status check in the report. Total wall-clock time is bounded
    by the per-check timeout (5s) summed across enabled probes.
    """
    from . import __version__

    start = time.perf_counter()
    checks: list[DoctorCheck] = []

    checks.append(await _timed("python_version", _check_python_version, config))
    checks.append(await _timed("package_version", _check_package_version, config))
    checks.append(await _timed("playwright_import", _check_playwright_import, config))
    checks.append(await _timed("chromium_installed", _check_chromium_installed, config))

    if quick:
        checks.append(
            DoctorCheck(
                name="browser_launch",
                status="skip",
                message="Skipped (quick=True)",
            )
        )
    else:
        checks.append(await _timed("browser_launch", _check_browser_launch, config))

    checks.append(await _timed("ddgs_import", _check_ddgs_import, config))
    checks.append(await _timed("searxng_reachable", _check_searxng_reachable, config))
    checks.append(await _timed("mcp_import", _check_mcp_import, config))
    checks.append(await _timed("binary_extra_pypdf", _check_binary_extra, "pypdf", "pypdf"))
    checks.append(
        await _timed("binary_extra_openpyxl", _check_binary_extra, "openpyxl", "openpyxl")
    )
    checks.append(
        await _timed("binary_extra_python_docx", _check_binary_extra, "python_docx", "docx")
    )
    checks.append(
        await _timed(
            "dir_writable_downloads",
            _check_dir_writable,
            "downloads",
            config.download.download_dir,
        )
    )
    checks.append(
        await _timed(
            "dir_writable_screenshots",
            _check_dir_writable,
            "screenshots",
            config.automation.screenshot_dir,
        )
    )
    checks.append(
        await _timed("dir_writable_debug", _check_dir_writable, "debug", config.debug.debug_dir)
    )
    checks.append(await _timed("network_connectivity", _check_network_connectivity, config))
    checks.append(await _timed("robots_rate_sanity", _check_robots_rate_sanity, config))
    checks.append(await _timed("config_file_parse", _check_config_file_parse, config))

    total_ms = (time.perf_counter() - start) * 1000
    summary = _summarize(checks)
    py = sys.version_info
    return DoctorReport(
        summary=summary,  # type: ignore[arg-type]
        web_agent_version=__version__,
        python_version=f"{py.major}.{py.minor}.{py.micro}",
        platform=platform.platform(),
        checks=checks,
        total_duration_ms=total_ms,
    )


def format_report_human(report: DoctorReport) -> str:
    """Format a DoctorReport for human-friendly terminal output."""
    sym = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[SKIP]"}
    lines: list[str] = []
    lines.append(f"webTool Doctor -- {report.platform}")
    lines.append(f"Python {report.python_version}  web_agent {report.web_agent_version}")
    lines.append("")
    for c in report.checks:
        lines.append(f"{sym.get(c.status, '[?]'):7} {c.name:32}  {c.message}")
    lines.append("")
    lines.append(f"Summary: {report.summary}   ({report.total_duration_ms:.0f} ms total)")
    return "\n".join(lines)


__all__ = ["format_report_human", "run_doctor"]
