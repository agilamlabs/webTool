"""v1.7.0 Wave 4C: accessibility-tree set-of-marks observe -> act loop.

observe() runs ONE page.evaluate DOM walk and returns a bounded, numbered
list of the actionable elements visible in the viewport on
``ObserveResult.elements`` (each an InteractiveElement: ref / role / name /
tag / enabled / visible / bbox / selector). The model then acts on
"element #N" by passing its ref back as ``LocatorSpec(ref="eN")``, which
resolves the live ``[data-webtool-ref="eN"]`` attribute observe() stamped on
the page -- closing the observe -> act loop and avoiding hallucinated /
brittle CSS targeting.

These tests are OFFLINE: page.evaluate is mocked to return a handcrafted
element list, mirroring tests/test_v166_observe.py. The act-by-ref path is
exercised through the real _resolve_locator + execute_action dispatch with a
MagicMock locator, so a stale / malformed ref fails cleanly via the
established not-found path with no crash.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.browser_actions import BrowserActions, _resolve_locator
from web_agent.config import AppConfig, AutomationConfig, SafetyConfig
from web_agent.exceptions import SelectorNotFoundError
from web_agent.models import (
    ActionStatus,
    ActionType,
    ClickInput,
    InteractiveElement,
    LocatorSpec,
    ObserveResult,
)

# A handcrafted set-of-marks payload, shaped exactly like the enumeration JS
# (_ENUMERATE_INTERACTIVE_JS) returns: {"elements": [...], "truncated": bool}.
_SAMPLE_ELEMENTS = [
    {
        "ref": "e1",
        "role": "link",
        "name": "Home",
        "tag": "a",
        "enabled": True,
        "visible": True,
        "bbox": [10.0, 20.0, 60.0, 18.0],
        "selector": '[data-webtool-ref="e1"]',
    },
    {
        "ref": "e2",
        "role": "textbox",
        "name": "Search",
        "tag": "input",
        "enabled": True,
        "visible": True,
        "bbox": [10.0, 50.0, 200.0, 30.0],
        "selector": '[data-webtool-ref="e2"]',
    },
    {
        "ref": "e3",
        "role": "button",
        "name": "Submit",
        "tag": "button",
        "enabled": False,
        "visible": True,
        "bbox": [220.0, 50.0, 80.0, 30.0],
        "selector": '[data-webtool-ref="e3"]',
    },
]


def _make_page(
    *,
    url: str = "https://example.com/page",
    enum_payload: dict | None = None,
) -> MagicMock:
    """Fake Page covering what observe() touches, plus the set-of-marks
    enumeration evaluate. ``page.evaluate`` dispatches on the script text:
    dims object, innerText string, or the interactive-element walk.
    """
    payload = enum_payload if enum_payload is not None else {
        "elements": _SAMPLE_ELEMENTS,
        "truncated": False,
    }

    def _evaluate(expr, *args):
        # The enumeration JS also references window.innerWidth, so match its
        # unique marker FIRST before the dims branch.
        if "data-webtool-ref" in expr:
            return payload
        if "innerWidth" in expr:
            return {"vw": 1280, "vh": 720, "pw": 1280, "ph": 2400, "sx": 0, "sy": 0, "dpr": 2.0}
        return "the visible page text"

    page = MagicMock()
    type(page).url = property(lambda _self: url)
    page.goto = AsyncMock()
    page.title = AsyncMock(return_value="Example Page")
    page.screenshot = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(side_effect=_evaluate)
    accessibility = MagicMock()
    accessibility.snapshot = AsyncMock(return_value={"role": "WebArea"})
    page.accessibility = accessibility
    page.mouse = MagicMock()
    page.keyboard = MagicMock()
    return page


def _make_browser_manager(page: MagicMock) -> MagicMock:
    class _PageCtx:
        async def __aenter__(self):
            return page

        async def __aexit__(self, *_a):
            return False

    bm = MagicMock()
    bm.new_page = MagicMock(return_value=_PageCtx())
    return bm


def _ba(tmp_path: Path, config: AppConfig | None = None, page: MagicMock | None = None):
    cfg = config or AppConfig(
        base_dir=str(tmp_path),
        automation=AutomationConfig(screenshot_dir=str(tmp_path / "shots")),
    )
    pg = page or _make_page()
    ba = BrowserActions(_make_browser_manager(pg), cfg, sessions=None)
    return ba, pg


# ----------------------------------------------------------------------
# observe() returns the structured set-of-marks elements list
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_returns_structured_elements(tmp_path: Path) -> None:
    ba, _ = _ba(tmp_path)

    obs = await ba.observe(url="https://example.com/page", include_text=False)

    assert isinstance(obs, ObserveResult)
    assert len(obs.elements) == 3
    assert all(isinstance(e, InteractiveElement) for e in obs.elements)
    e1, e2, e3 = obs.elements
    assert e1.ref == "e1" and e1.role == "link" and e1.name == "Home" and e1.tag == "a"
    assert e1.bbox == [10.0, 20.0, 60.0, 18.0]
    assert e2.ref == "e2" and e2.role == "textbox" and e2.name == "Search"
    assert e3.ref == "e3" and e3.enabled is False  # disabled button preserved
    # ref-tagging on by default -> each element carries its resolvable selector
    assert e1.selector == '[data-webtool-ref="e1"]'
    assert obs.elements_truncated is False


@pytest.mark.asyncio
async def test_observe_passes_max_and_tag_flag_to_evaluate(tmp_path: Path) -> None:
    """The enumeration evaluate is parameterised by observe_max_elements and
    observe_tag_refs from AutomationConfig."""
    cfg = AppConfig(
        base_dir=str(tmp_path),
        automation=AutomationConfig(
            screenshot_dir=str(tmp_path / "shots"),
            observe_max_elements=42,
            observe_tag_refs=True,
        ),
    )
    ba, page = _ba(tmp_path, config=cfg)

    await ba.observe(url="https://example.com/page", include_text=False)

    enum_calls = [
        c for c in page.evaluate.await_args_list
        if "data-webtool-ref" in c.args[0] or "querySelectorAll" in c.args[0]
    ]
    assert enum_calls, "enumeration evaluate was never called"
    params = enum_calls[0].args[1]
    assert params == {"max": 42, "tag": True}


@pytest.mark.asyncio
async def test_observe_count_bounded_and_truncation_flagged(tmp_path: Path) -> None:
    """When the JS reports truncated=True, ObserveResult.elements_truncated is
    set (the count is bounded by observe_max_elements; the JS caps it and
    raises the flag)."""
    cfg = AppConfig(
        base_dir=str(tmp_path),
        automation=AutomationConfig(
            screenshot_dir=str(tmp_path / "shots"),
            observe_max_elements=2,
        ),
    )
    # Simulate the JS having capped the list at 2 and flagged truncation.
    payload = {"elements": _SAMPLE_ELEMENTS[:2], "truncated": True}
    page = _make_page(enum_payload=payload)
    ba, _ = _ba(tmp_path, config=cfg, page=page)

    obs = await ba.observe(url="https://example.com/page", include_text=False)

    assert len(obs.elements) == 2
    assert obs.elements_truncated is True


@pytest.mark.asyncio
async def test_observe_empty_page_returns_empty_list(tmp_path: Path) -> None:
    page = _make_page(enum_payload={"elements": [], "truncated": False})
    ba, _ = _ba(tmp_path, page=page)

    obs = await ba.observe(url="https://example.com/page", include_text=False)

    assert obs.elements == []
    assert obs.elements_truncated is False


@pytest.mark.asyncio
async def test_observe_include_elements_false_skips_enumeration(tmp_path: Path) -> None:
    ba, page = _ba(tmp_path)

    obs = await ba.observe(
        url="https://example.com/page", include_text=False, include_elements=False
    )

    assert obs.elements == []
    enum_calls = [
        c for c in page.evaluate.await_args_list
        if "data-webtool-ref" in c.args[0] or "querySelectorAll" in c.args[0]
    ]
    assert not enum_calls, "enumeration must not run when include_elements=False"


@pytest.mark.asyncio
async def test_observe_enumeration_failure_is_best_effort(tmp_path: Path) -> None:
    """A failing enumeration evaluate must not sink observe(): other fields
    still return and elements is empty."""

    def _evaluate(expr, *args):
        if "innerWidth" in expr:
            return {"vw": 1, "vh": 1, "pw": 1, "ph": 1, "sx": 0, "sy": 0, "dpr": 1.0}
        if "querySelectorAll" in expr or "data-webtool-ref" in expr:
            raise RuntimeError("evaluate boom")
        return ""

    page = _make_page()
    page.evaluate = AsyncMock(side_effect=_evaluate)
    ba, _ = _ba(tmp_path, page=page)

    obs = await ba.observe(url="https://example.com/page", include_text=False)

    assert obs.elements == []
    assert obs.viewport_width == 1  # the rest of observe still worked


# ----------------------------------------------------------------------
# act-by-ref: LocatorSpec(ref=...) -> [data-webtool-ref] resolution
# ----------------------------------------------------------------------


def test_resolve_locator_ref_targets_data_attribute() -> None:
    page = MagicMock()
    page.locator = MagicMock(return_value="LOCATOR")
    loc = _resolve_locator(page, LocatorSpec(ref="e3"))
    page.locator.assert_called_once_with('[data-webtool-ref="e3"]')
    assert loc == "LOCATOR"


def test_resolve_locator_ref_takes_priority_over_other_fields() -> None:
    page = MagicMock()
    page.locator = MagicMock(return_value="LOCATOR")
    # ref wins over selector / role / text -- it is the most specific signal.
    _resolve_locator(
        page, LocatorSpec(ref="e7", selector="button.x", role="button", text="Buy")
    )
    page.locator.assert_called_once_with('[data-webtool-ref="e7"]')


@pytest.mark.parametrize("bad", ['e1"] , *', "e", "5", "button", 'e1"]', "e 1", ""])
def test_resolve_locator_rejects_malformed_ref(bad: str) -> None:
    """A ref must be a well-formed eN token; anything else (incl. a CSS-escape
    attempt) is rejected BEFORE reaching page.locator -- no selector injection.
    An empty ref means an empty LocatorSpec (also rejected)."""
    page = MagicMock()
    page.locator = MagicMock()
    with pytest.raises(SelectorNotFoundError):
        _resolve_locator(page, LocatorSpec(ref=bad))
    page.locator.assert_not_called()


@pytest.mark.asyncio
async def test_click_by_ref_resolves_and_succeeds(tmp_path: Path) -> None:
    """A click targeting ref='e3' resolves the right [data-webtool-ref] locator
    and clicks it through the normal _do_click path."""
    ba, _ = _ba(tmp_path)

    clicked_locator = MagicMock()
    clicked_locator.click = AsyncMock()
    page = MagicMock()
    page.locator = MagicMock(return_value=clicked_locator)

    result = await ba.execute_action(page, ClickInput(selector=LocatorSpec(ref="e3")))

    page.locator.assert_called_once_with('[data-webtool-ref="e3"]')
    clicked_locator.click.assert_awaited_once()
    assert result.status == ActionStatus.SUCCESS
    assert result.action == ActionType.CLICK
    # ActionResult.selector echoes the LocatorSpec (ref preserved) for traces.
    assert result.selector is not None and "e3" in result.selector


@pytest.mark.asyncio
async def test_stale_ref_click_fails_cleanly_no_crash(tmp_path: Path) -> None:
    """A stale / unknown ref -> the live [data-webtool-ref] locator matches
    nothing -> loc.click() times out -> execute_action returns a structured
    FAILED/TIMEOUT result instead of raising."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    ba, _ = _ba(tmp_path)

    stale_locator = MagicMock()
    stale_locator.click = AsyncMock(
        side_effect=PlaywrightTimeout("locator resolved to 0 elements")
    )
    page = MagicMock()
    page.locator = MagicMock(return_value=stale_locator)

    result = await ba.execute_action(page, ClickInput(selector=LocatorSpec(ref="e99")))

    assert result.status in (ActionStatus.TIMEOUT, ActionStatus.FAILED)
    assert result.error_message  # carries the underlying reason
    page.locator.assert_called_once_with('[data-webtool-ref="e99"]')


@pytest.mark.asyncio
async def test_malformed_ref_click_fails_cleanly_no_crash(tmp_path: Path) -> None:
    """A malformed ref on a click is rejected by _resolve_locator and surfaced
    as a structured FAILED result (SelectorNotFoundError caught by
    execute_action), never a crash."""
    ba, _ = _ba(tmp_path)
    page = MagicMock()
    page.locator = MagicMock()

    result = await ba.execute_action(
        page, ClickInput(selector=LocatorSpec(ref='e1"] *'))
    )

    assert result.status == ActionStatus.FAILED
    page.locator.assert_not_called()


# ----------------------------------------------------------------------
# Safety gates preserved on the ref path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ref_click_respects_domain_regate_in_sequence(tmp_path: Path) -> None:
    """execute_sequence's leading domain gate blocks the WHOLE sequence when the
    starting URL is a denied host -- every action, ref click included, is
    SKIPPED and never runs. Confirms ref-path clicks sit behind the same
    domain gate as every other action."""
    cfg = AppConfig(
        base_dir=str(tmp_path),
        # IMDS host denied by block_private_ips (default True)
        automation=AutomationConfig(screenshot_dir=str(tmp_path / "shots")),
    )
    clicked_locator = MagicMock()
    clicked_locator.click = AsyncMock()
    page = _make_page()
    page.locator = MagicMock(return_value=clicked_locator)
    ba = BrowserActions(_make_browser_manager(page), cfg, sessions=None)

    seq = await ba.execute_sequence(
        "http://169.254.169.254/latest/meta-data/",
        [ClickInput(selector=LocatorSpec(ref="e3"))],
    )

    assert seq.actions_succeeded == 0
    assert seq.results[0].status == ActionStatus.SKIPPED
    assert "Domain not allowed" in (seq.results[0].error_message or "")
    clicked_locator.click.assert_not_awaited()
    page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_ref_only_click_passes_submit_heuristic_when_allowed(tmp_path: Path) -> None:
    """With allow_form_submit=True (default), a ref-only ClickInput is NOT
    pre-blocked by the advisory submit heuristic (a ref carries no text to
    match) -- the established gate behaviour is preserved: the click runs
    through the normal _do_click path."""
    clicked_locator = MagicMock()
    clicked_locator.click = AsyncMock()
    page = _make_page()
    page.locator = MagicMock(return_value=clicked_locator)
    ba, _ = _ba(tmp_path, page=page)

    seq = await ba.execute_sequence(
        "https://example.com/page",
        [ClickInput(selector=LocatorSpec(ref="e3"))],
    )

    assert seq.actions_succeeded == 1
    clicked_locator.click.assert_awaited_once()
    page.locator.assert_any_call('[data-webtool-ref="e3"]')


# ----------------------------------------------------------------------
# Backward compatibility
# ----------------------------------------------------------------------


def test_locatorspec_backward_compat_constructions_still_validate() -> None:
    # Old-style specs (no ref) keep working and are non-empty.
    assert LocatorSpec(selector="button.primary").is_empty() is False
    assert LocatorSpec(role="button", role_name="Submit").is_empty() is False
    assert LocatorSpec(test_id="login").is_empty() is False
    # Truly empty is still empty.
    assert LocatorSpec().is_empty() is True
    # ref-only is a valid (non-empty) locator now.
    assert LocatorSpec(ref="e5").is_empty() is False
    # role_name alone is still NOT a standalone locator (unchanged contract).
    assert LocatorSpec(role_name="Submit").is_empty() is True


def test_observeresult_backward_compat_without_elements() -> None:
    """An ObserveResult built the pre-4C way (no elements) still validates and
    defaults to an empty, non-truncated list."""
    obs = ObserveResult(
        url="https://example.com",
        screenshot_path="/tmp/x.png",
        viewport_width=800,
        viewport_height=600,
        page_width=800,
        page_height=1200,
        scroll_x=0,
        scroll_y=0,
        device_pixel_ratio=1.0,
    )
    assert obs.elements == []
    assert obs.elements_truncated is False
    # Round-trips through JSON unchanged in shape.
    again = ObserveResult.model_validate_json(obs.model_dump_json())
    assert again.elements == []


def test_interactive_element_minimal_construction() -> None:
    """InteractiveElement requires only ref/role/tag; the rest default."""
    el = InteractiveElement(ref="e1", role="button", tag="button")
    assert el.name == ""
    assert el.enabled is True
    assert el.visible is True
    assert el.bbox == []
    assert el.selector is None


def test_observe_default_safety_does_not_block_enumeration(tmp_path: Path) -> None:
    """The set-of-marks enumeration is first-party instrumentation: it runs even
    though safety.allow_js_evaluation defaults to False (same gating as the
    existing dims / innerText / aria observe evaluates)."""
    cfg = AppConfig(
        base_dir=str(tmp_path),
        safety=SafetyConfig(allow_js_evaluation=False),
        automation=AutomationConfig(screenshot_dir=str(tmp_path / "shots")),
    )
    assert cfg.safety.allow_js_evaluation is False  # precondition
    # (Behavioural assertion lives in test_observe_returns_structured_elements,
    #  which uses the default config where allow_js_evaluation is also False
    #  and the elements list is still populated.)
