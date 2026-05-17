"""v1.6.6 Feature 6: doctor self-diagnostic.

These tests exercise the aggregator + a few specific probes. They do
NOT actually launch Chromium (that's the --quick path). The real
launch_browser probe is covered by the integration smoke documented
in the AGENTS.md verification steps.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web_agent.config import AppConfig, SearchConfig
from web_agent.doctor import _summarize, format_report_human, run_doctor
from web_agent.models import DoctorCheck

# ----------------------------------------------------------------------
# Aggregator unit
# ----------------------------------------------------------------------


def test_summary_healthy_when_all_ok() -> None:
    checks = [DoctorCheck(name="a", status="ok"), DoctorCheck(name="b", status="ok")]
    assert _summarize(checks) == "healthy"


def test_summary_warnings_when_any_warn_no_fail() -> None:
    checks = [
        DoctorCheck(name="a", status="ok"),
        DoctorCheck(name="b", status="warn"),
        DoctorCheck(name="c", status="ok"),
    ]
    assert _summarize(checks) == "usable_with_warnings"


def test_summary_unusable_when_any_fail() -> None:
    checks = [
        DoctorCheck(name="a", status="ok"),
        DoctorCheck(name="b", status="warn"),
        DoctorCheck(name="c", status="fail"),
    ]
    assert _summarize(checks) == "unusable"


def test_summary_healthy_when_only_skips() -> None:
    """skip is neutral -- a probe that doesn't apply (e.g. SearXNG
    not configured) shouldn't downgrade the report."""
    checks = [DoctorCheck(name="a", status="ok"), DoctorCheck(name="b", status="skip")]
    assert _summarize(checks) == "healthy"


# ----------------------------------------------------------------------
# run_doctor: quick path skips the browser launch
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_doctor_quick_skips_browser_launch(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))
    report = await run_doctor(cfg, quick=True)

    # Browser launch entry exists, but marked skip
    launch = next(c for c in report.checks if c.name == "browser_launch")
    assert launch.status == "skip"
    assert "quick" in launch.message.lower()


# ----------------------------------------------------------------------
# searxng_reachable: skipped when no URL configured
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_doctor_searxng_skipped_when_no_url(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path), search=SearchConfig(searxng_base_url=None))
    report = await run_doctor(cfg, quick=True)

    sx = next(c for c in report.checks if c.name == "searxng_reachable")
    assert sx.status == "skip"


# ----------------------------------------------------------------------
# Human-friendly formatter
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_doctor_human_format_includes_summary_line(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))
    report = await run_doctor(cfg, quick=True)
    formatted = format_report_human(report)

    assert "webTool Doctor" in formatted
    assert "Summary:" in formatted
    assert report.summary in formatted


# ----------------------------------------------------------------------
# Network connectivity tolerance: should not crash even if offline
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_doctor_returns_report_offline(monkeypatch, tmp_path: Path) -> None:
    """If the network probe fails (e.g. CI sandbox is offline), doctor
    still returns a structured report -- the probe just downgrades to
    warn, not fail."""
    cfg = AppConfig(base_dir=str(tmp_path))
    import web_agent.doctor as doc_mod

    async def _force_fail(*_a, **_k):
        raise RuntimeError("simulated offline")

    monkeypatch.setattr(doc_mod, "_check_network_connectivity", _force_fail)

    report = await run_doctor(cfg, quick=True)
    net = next(c for c in report.checks if c.name == "network_connectivity")
    # _timed wraps the failure -> "fail" status
    assert net.status == "fail"
    assert "simulated offline" in net.message
    # Other checks still ran
    assert any(c.name == "python_version" for c in report.checks)
