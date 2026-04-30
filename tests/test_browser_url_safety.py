"""Unit tests for BrowserActions URL safety (v1.6.0).

These tests do NOT launch a real browser. They mock Playwright's
``Page`` so we can exercise the pre-check + post-redirect re-check +
per-action drift detection paths without network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.browser_actions import BrowserActions
from web_agent.config import AppConfig
from web_agent.models import (
    ActionStatus,
    EvaluateInput,
    NavigateDirection,
    NavigateInput,
)


def _make_actions(safety: dict | None = None) -> BrowserActions:
    """Build a BrowserActions instance with a mocked BrowserManager."""
    config = AppConfig(
        safety=safety or {},
        # JS evaluation is needed for some drift tests
    )
    return BrowserActions(browser_manager=MagicMock(), config=config)


class TestNavigateGotoPrecheck:
    """_do_navigate must validate ``action.url`` BEFORE calling page.goto."""

    @pytest.mark.asyncio
    async def test_blocks_denied_domain_before_network(self) -> None:
        actions = _make_actions(safety={"denied_domains": ["evil.example.com"]})
        page = MagicMock()
        page.goto = AsyncMock()

        result = await actions._do_navigate(
            page,
            NavigateInput(
                navigate_action=NavigateDirection.GOTO,
                url="https://evil.example.com/page",
            ),
        )

        assert result.status == ActionStatus.FAILED
        assert "not allowed by SafetyConfig" in (result.error_message or "")
        # Critical: page.goto must NEVER have been awaited
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocks_private_ip_aws_imds(self) -> None:
        # block_private_ips defaults to True
        actions = _make_actions()
        page = MagicMock()
        page.goto = AsyncMock()

        result = await actions._do_navigate(
            page,
            NavigateInput(
                navigate_action=NavigateDirection.GOTO,
                url="http://169.254.169.254/latest/meta-data/",
            ),
        )

        assert result.status == ActionStatus.FAILED
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocks_private_rfc1918(self) -> None:
        actions = _make_actions()
        page = MagicMock()
        page.goto = AsyncMock()

        result = await actions._do_navigate(
            page,
            NavigateInput(
                navigate_action=NavigateDirection.GOTO,
                url="http://10.0.0.1/admin",
            ),
        )

        assert result.status == ActionStatus.FAILED
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_public_url(self) -> None:
        actions = _make_actions()
        page = MagicMock()
        page.url = "https://example.com/"
        page.goto = AsyncMock(return_value=None)

        result = await actions._do_navigate(
            page,
            NavigateInput(
                navigate_action=NavigateDirection.GOTO,
                url="https://example.com/",
            ),
        )

        assert result.status == ActionStatus.SUCCESS
        page.goto.assert_awaited_once()


class TestNavigatePostRedirectRecheck:
    """After page.goto / go_back / go_forward / reload, page.url may have
    drifted to a different host. Re-validate against the policy."""

    @pytest.mark.asyncio
    async def test_blocks_redirect_to_denied_domain(self) -> None:
        actions = _make_actions(safety={"denied_domains": ["evil.example.com"]})
        page = MagicMock()
        page.goto = AsyncMock(return_value=None)
        # Simulate: pre-check OK, but server redirected to evil after goto
        page.url = "https://evil.example.com/landed"

        result = await actions._do_navigate(
            page,
            NavigateInput(
                navigate_action=NavigateDirection.GOTO,
                url="https://safe.example.com/redirect-me",
            ),
        )

        assert result.status == ActionStatus.FAILED
        assert "redirect" in (result.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_blocks_back_button_to_private_ip(self) -> None:
        actions = _make_actions()
        page = MagicMock()
        page.go_back = AsyncMock(return_value=None)
        # Simulate: history navigation lands on an internal address
        page.url = "http://10.0.0.5/admin"

        result = await actions._do_navigate(
            page,
            NavigateInput(navigate_action=NavigateDirection.BACK),
        )

        assert result.status == ActionStatus.FAILED
        page.go_back.assert_awaited_once()


class TestExecuteSequenceURLDrift:
    """When an in-sequence action causes a navigation (link click, form
    submit, JS-driven nav), page.url drift should be caught and abort
    the sequence regardless of stop_on_error."""

    @pytest.mark.asyncio
    async def test_drift_after_evaluate_aborts_sequence(self) -> None:
        actions = _make_actions(
            safety={
                "denied_domains": ["evil.example.com"],
                "allow_js_evaluation": True,
            }
        )
        # Stub the page acquisition + navigation
        page = MagicMock()
        page.url = "https://example.com/start"  # initial URL after goto
        page.goto = AsyncMock(return_value=None)
        page.on = MagicMock()
        page.evaluate = AsyncMock(return_value=42)

        ctx_mgr = MagicMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=page)
        ctx_mgr.__aexit__ = AsyncMock(return_value=None)
        actions._bm.new_page = MagicMock(return_value=ctx_mgr)

        # The EvaluateInput "succeeds" but afterwards we mutate page.url
        # to simulate a JS-driven location change to a disallowed host.
        async def _evaluate(*_args, **_kwargs):
            page.url = "https://evil.example.com/now-here"
            return 42

        page.evaluate.side_effect = _evaluate

        seq = [
            EvaluateInput(expression="window.location='evil'"),
            EvaluateInput(expression="42"),  # should be SKIPPED
        ]

        result = await actions.execute_sequence("https://example.com/start", seq)

        # First action: marked FAILED (was SUCCESS but URL drifted)
        assert result.results[0].status == ActionStatus.FAILED
        assert "drifted" in (result.results[0].error_message or "").lower()
        # Second action: SKIPPED with same drift message
        assert result.results[1].status == ActionStatus.SKIPPED
        assert result.actions_succeeded == 0
        assert result.actions_failed >= 1

    @pytest.mark.asyncio
    async def test_initial_goto_redirect_blocks_all_actions(self) -> None:
        actions = _make_actions(
            safety={
                "denied_domains": ["evil.example.com"],
                # allow JS so we get past the pre-flight EvaluateInput gate
                # and the test exercises the redirect-block path proper
                "allow_js_evaluation": True,
            }
        )
        page = MagicMock()
        page.goto = AsyncMock(return_value=None)
        # Initial goto redirects to denied
        page.url = "https://evil.example.com/landed"

        ctx_mgr = MagicMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=page)
        ctx_mgr.__aexit__ = AsyncMock(return_value=None)
        actions._bm.new_page = MagicMock(return_value=ctx_mgr)

        seq = [
            EvaluateInput(expression="1+1"),
            EvaluateInput(expression="2+2"),
        ]

        result = await actions.execute_sequence("https://safe.example.com/redirect", seq)

        # Every action SKIPPED with the redirect-blocked reason
        assert result.actions_total == len(seq)
        assert result.actions_failed == len(seq)
        assert all(r.status == ActionStatus.SKIPPED for r in result.results)
        assert all("disallowed domain" in (r.error_message or "").lower() for r in result.results)


class TestTakeScreenshotPostRedirect:
    """take_screenshot must re-check page.url after the goto so a
    whitelisted host can't redirect to a private IP and leak a screenshot."""

    @pytest.mark.asyncio
    async def test_blocks_redirect_to_private_ip(self) -> None:
        actions = _make_actions()
        page = MagicMock()
        page.goto = AsyncMock(return_value=None)
        page.url = "http://192.168.1.1/admin"  # redirected to RFC1918
        page.screenshot = AsyncMock()

        ctx_mgr = MagicMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=page)
        ctx_mgr.__aexit__ = AsyncMock(return_value=None)
        actions._bm.new_page = MagicMock(return_value=ctx_mgr)

        result = await actions.take_screenshot("https://safe.example.com/")

        assert result.status == ActionStatus.FAILED
        assert "disallowed domain" in (result.error_message or "").lower()
        # Critical: screenshot was NEVER taken (would have leaked the page)
        page.screenshot.assert_not_called()
