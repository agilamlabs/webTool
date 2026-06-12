"""Deep-review (post-v1.6.16) models.py-cluster regression tests.

  * ``delay`` fields on TypeInput / ClickXYInput / TypeTextInput are bounded
    (ge=0, le=5000) -- they feed UNTIMED Playwright keyboard.type / mouse.click
    calls, so an unbounded value pinned the shared Playwright connection.
  * ``FormFilterSpec.wait_timeout_ms`` has ge=1 so the Playwright
    "0 == wait forever" sentinel is unreachable.
  * ``LocatorSpec.is_empty()`` no longer counts ``role_name`` (only a filter
    for ``role``), matching the resolver; the resolver gives a precise error
    for a role_name-only spec instead of the misleading "empty".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pydantic
import pytest
from web_agent.exceptions import SelectorNotFoundError
from web_agent.models import (
    ClickXYInput,
    FormFilterSpec,
    LocatorSpec,
    TypeInput,
    TypeTextInput,
)


class TestDelayBounds:
    @pytest.mark.parametrize("delay", [10**9, -1, 5001])
    def test_typeinput_delay_out_of_range_rejected(self, delay: int) -> None:
        with pytest.raises(pydantic.ValidationError):
            TypeInput(selector="#x", text="a", delay=delay)

    @pytest.mark.parametrize("delay", [10**9, -1, 5001])
    def test_clickxy_delay_out_of_range_rejected(self, delay: int) -> None:
        with pytest.raises(pydantic.ValidationError):
            ClickXYInput(x=1, y=1, delay=delay)

    @pytest.mark.parametrize("delay", [10**9, -1, 5001])
    def test_typetext_delay_out_of_range_rejected(self, delay: int) -> None:
        with pytest.raises(pydantic.ValidationError):
            TypeTextInput(text="a", delay=delay)

    @pytest.mark.parametrize("delay", [0, 100, 5000])
    def test_in_range_delays_accepted(self, delay: int) -> None:
        assert TypeInput(selector="#x", text="a", delay=delay).delay == delay
        assert ClickXYInput(x=1, y=1, delay=delay).delay == delay
        assert TypeTextInput(text="a", delay=delay).delay == delay


class TestWaitTimeoutMsBound:
    def test_zero_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FormFilterSpec(url="https://x.example/f", fields={}, wait_timeout_ms=0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FormFilterSpec(url="https://x.example/f", fields={}, wait_timeout_ms=-1)

    def test_default_and_valid_accepted(self) -> None:
        assert FormFilterSpec(url="https://x.example/f", fields={}).wait_timeout_ms == 15000
        assert (
            FormFilterSpec(url="https://x.example/f", fields={}, wait_timeout_ms=1).wait_timeout_ms
            == 1
        )


class TestLocatorSpecContract:
    def test_role_name_only_is_empty(self) -> None:
        assert LocatorSpec(role_name="Submit").is_empty() is True

    def test_role_with_name_not_empty(self) -> None:
        assert LocatorSpec(role="button", role_name="Submit").is_empty() is False

    def test_resolver_gives_precise_error_for_role_name_only(self) -> None:
        from web_agent.browser_actions import _resolve_locator

        with pytest.raises(SelectorNotFoundError) as exc:
            _resolve_locator(MagicMock(), LocatorSpec(role_name="Submit"))
        # Precise message -- NOT the misleading "LocatorSpec is empty".
        assert "role_name" in str(exc.value)
        assert "without role" in str(exc.value)

    def test_resolver_still_reports_truly_empty_spec(self) -> None:
        from web_agent.browser_actions import _resolve_locator

        with pytest.raises(SelectorNotFoundError) as exc:
            _resolve_locator(MagicMock(), LocatorSpec())
        assert "empty" in str(exc.value).lower()
