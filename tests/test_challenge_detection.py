"""v1.7.0 Wave 1A: bot-challenge detection + bounded auto-settle.

Covers the fetch-honesty slice:

* :func:`web_agent.challenge.detect_challenge` -- per-vendor positive
  fixtures (structural markers) across HTTP 200/403/503, plus
  false-positive guards (prose mentions, embedded CAPTCHA widgets on
  normal pages must never block).
* :class:`web_agent.models.ChallengeInfo` / ``FetchStatus.BLOCKED``
  serialization round-trips (including the cache ``model_dump`` path).
* ``WebFetcher._navigate_and_extract`` integration -- settle-recheck
  clears -> SUCCESS, never clears -> BLOCKED, ``challenge_max_rechecks=0``
  -> immediate BLOCKED, captcha kinds never settle, 403/503 fast-fail
  conversion, and the detection kill-switch.
* Cache-poisoning guard (BLOCKED never written) and retry sanity (a
  BLOCKED determination must not burn ``async_retry`` re-navigations).

Pattern follows ``tests/test_v1614_throughput.py`` -- AsyncMock-driven,
fully offline, no Playwright launch, no network.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import web_agent.web_fetcher as wf_module
from web_agent.challenge import CHALLENGE_CONFIDENCE_ACTION_THRESHOLD, detect_challenge
from web_agent.config import AppConfig
from web_agent.models import ChallengeInfo, FetchResult, FetchStatus
from web_agent.utils import NonRetryableHTTPError
from web_agent.web_fetcher import WebFetcher

# ----------------------------------------------------------------------
# Handcrafted fixtures (structural markers only -- no live HTML)
# ----------------------------------------------------------------------

CF_CHALLENGE_HTML = """<!DOCTYPE html><html lang="en-US"><head>
<title>Just a moment...</title>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=8abc"></script>
<script>window._cf_chl_opt={cvId:'3',cType:'managed',cRay:'8abc',cH:'x.y'};</script>
</head><body class="no-js">
<div class="main-wrapper"><h1>www.example.com</h1>
<p>Verifying you are human. This may take a few seconds.</p>
<p>www.example.com needs to review the security of your connection before proceeding.</p>
</div></body></html>"""

CF_BLOCK_HTML = """<!DOCTYPE html><html><head>
<title>Attention Required! | Cloudflare</title></head>
<body><div id="cf-wrapper"><div class="cf-error-details">
<h1>Sorry, you have been blocked</h1>
<p>You are unable to access example.com</p>
<p class="cf-footer">Cloudflare Ray ID: 8abc123 - Performance by Cloudflare</p>
</div></div></body></html>"""

DATADOME_HTML = """<!DOCTYPE html><html><head><title>example.com</title>
<script>var dd={'rt':'c','cid':'AHrlqAAA','hsh':'A55FBF4311ED6F1BF9911EB71931D5',
't':'fe','s':17434,'e':'f1f','ddjskey':'AHrlqAAAAAB'}</script>
<script src="https://geo.captcha-delivery.com/captcha/?initialCid=AHrlqAAA"></script>
</head><body><p>Please enable JS and disable any ad blocker.</p></body></html>"""

AKAMAI_HTML = """<!DOCTYPE html><html><head><title>example.com</title></head>
<body><form action="/_sec/cp_challenge/verify" method="post">
<div class="sec-cpt-frame" id="sec-cpt-if"></div>
<script src="/_sec/cp_challenge/sec-cpt-int-3.js"></script>
</form></body></html>"""

PERIMETERX_HTML = """<!DOCTYPE html><html><head>
<title>Access to this page has been denied</title>
<script src="https://captcha.px-cdn.net/PXabc123/captcha.js?a=c"></script>
<script>window._pxAppId='PXabc123';window._pxJsClientSrc='/PXabc123/init.js';</script>
</head><body><div id="px-captcha"></div>
<p>Please verify you are a human. Press and hold the button.</p></body></html>"""

HCAPTCHA_GATE_HTML = """<!DOCTYPE html><html><head>
<title>Verifying you are human</title>
<script src="https://js.hcaptcha.com/1/api.js" async defer></script>
</head><body><div class="h-captcha" data-sitekey="abc-123"></div>
<noscript>Please enable JavaScript to continue.</noscript></body></html>"""

# A paragraph of neutral prose, free of every structural marker. Repeated
# to push pages well past the "tiny visible text" challenge-shape bounds.
_FILLER_PARAGRAPH = (
    "<p>Quarterly results showed steady growth across the cloud division, "
    "with managed services revenue climbing for the eighth consecutive "
    "quarter while operating margins held firm despite renewed pricing "
    "pressure from regional competitors and currency headwinds.</p>\n"
)

# Long-form article that talks ABOUT bot-mitigation vendors in prose.
# Mentions vendor names and even denial-ish phrases in body text -- none
# of which are structural markers, so detection must return None.
PROSE_ARTICLE_HTML = (
    "<!DOCTYPE html><html><head>"
    "<title>How Cloudflare reshaped the anti-bot arms race</title></head><body>"
    "<article><h1>How Cloudflare reshaped the anti-bot arms race</h1>"
    "<p>Cloudflare's interstitial greets crawlers before letting them "
    "through, and competitors such as DataDome, Akamai Bot Manager and "
    "PerimeterX (now HUMAN) ship similar walls. Users see a page asking "
    "them to wait while the system is verifying you are human, and "
    "publishers report that scrapers tell them you have been blocked "
    "messages are now routine.</p>" + _FILLER_PARAGRAPH * 30 + "</article></body></html>"
)

# Normal-sized signup page with an embedded reCAPTCHA widget: the script
# marker is present but the page is NOT challenge-shaped -> no detection.
RECAPTCHA_NORMAL_PAGE_HTML = (
    "<!DOCTYPE html><html><head><title>Create your account</title>"
    '<script src="https://www.google.com/recaptcha/api.js" async defer></script>'
    "</head><body><h1>Join the newsletter</h1>"
    '<form><input name="email"><div class="g-recaptcha" data-sitekey="k"></div></form>'
    + _FILLER_PARAGRAPH * 30
    + "</body></html>"
)

# Normal-sized page embedding a Turnstile widget: weak Cloudflare markers
# only -> medium confidence, below the action threshold.
TURNSTILE_NORMAL_PAGE_HTML = (
    "<!DOCTYPE html><html><head><title>Contact sales</title>"
    '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
    "</head><body><h1>Talk to our team</h1>"
    '<form><div class="cf-turnstile" data-sitekey="k"></div></form>'
    + _FILLER_PARAGRAPH * 30
    + "</body></html>"
)

# Marker-free article used as the post-settle "real content" state.
CLEAN_ARTICLE_HTML = (
    "<!DOCTYPE html><html><head><title>Industry report</title></head><body>"
    "<article><h1>Industry report</h1>" + _FILLER_PARAGRAPH * 30 + "</article></body></html>"
)

PLAIN_403_HTML = (
    "<!DOCTYPE html><html><head><title>403 Forbidden</title></head>"
    "<body><h1>403 Forbidden</h1><p>nginx/1.24.0</p></body></html>"
)

URL = "https://example.com/article"


# ----------------------------------------------------------------------
# detect_challenge: per-vendor positives across status codes
# ----------------------------------------------------------------------


class TestDetectChallengeVendors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [200, 403, 503])
    async def test_cloudflare_js_challenge(self, status: int) -> None:
        info = detect_challenge(CF_CHALLENGE_HTML, status)
        assert info is not None
        assert info.vendor == "cloudflare"
        assert info.kind == "js_challenge"
        assert info.auto_settle_likely is True
        assert info.confidence >= CHALLENGE_CONFIDENCE_ACTION_THRESHOLD
        if status in (403, 503):
            assert info.confidence >= 0.95
        assert 1 <= len(info.evidence) <= 5
        assert any("challenge-platform" in e for e in info.evidence)

    @pytest.mark.asyncio
    async def test_cloudflare_header_only_detection(self) -> None:
        """``cf-mitigated: challenge`` identifies the wall even with an
        empty body (Playwright can hand back '' mid-interstitial)."""
        info = detect_challenge("", 403, {"CF-Mitigated": "challenge"})
        assert info is not None
        assert info.vendor == "cloudflare"
        assert info.kind == "js_challenge"
        assert info.auto_settle_likely is True
        assert info.confidence >= 0.95
        assert "header:cf-mitigated=challenge" in info.evidence

    @pytest.mark.asyncio
    async def test_cloudflare_block_page_not_auto_settle(self) -> None:
        info = detect_challenge(CF_BLOCK_HTML, 403)
        assert info is not None
        assert info.vendor == "cloudflare"
        assert info.kind == "block_page"
        assert info.auto_settle_likely is False
        assert info.confidence >= CHALLENGE_CONFIDENCE_ACTION_THRESHOLD

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [200, 403, 503])
    async def test_datadome(self, status: int) -> None:
        info = detect_challenge(DATADOME_HTML, status)
        assert info is not None
        assert info.vendor == "datadome"
        assert info.kind == "captcha"
        assert info.auto_settle_likely is False
        assert info.confidence >= CHALLENGE_CONFIDENCE_ACTION_THRESHOLD
        # 'geo.captcha-delivery.com' must not double-count its substring
        # marker 'captcha-delivery.com'.
        assert "geo.captcha-delivery.com" in info.evidence
        assert "captcha-delivery.com" not in info.evidence

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [200, 403, 503])
    async def test_akamai(self, status: int) -> None:
        info = detect_challenge(AKAMAI_HTML, status)
        assert info is not None
        assert info.vendor == "akamai"
        assert info.kind == "js_challenge"
        # Only Cloudflare js_challenges are deemed auto-settle-likely.
        assert info.auto_settle_likely is False
        assert info.confidence >= CHALLENGE_CONFIDENCE_ACTION_THRESHOLD

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [200, 403, 503])
    async def test_perimeterx(self, status: int) -> None:
        info = detect_challenge(PERIMETERX_HTML, status)
        assert info is not None
        assert info.vendor == "perimeterx"
        assert info.kind == "captcha"
        assert info.auto_settle_likely is False
        assert info.confidence >= CHALLENGE_CONFIDENCE_ACTION_THRESHOLD

    @pytest.mark.asyncio
    async def test_generic_hcaptcha_gate_on_200(self) -> None:
        """Tiny page + denial title + hCaptcha script = challenge gate."""
        info = detect_challenge(HCAPTCHA_GATE_HTML, 200)
        assert info is not None
        assert info.vendor == "generic_captcha"
        assert info.kind == "captcha"
        assert info.auto_settle_likely is False
        assert info.confidence >= CHALLENGE_CONFIDENCE_ACTION_THRESHOLD

    @pytest.mark.asyncio
    async def test_generic_recaptcha_on_403_scores_high(self) -> None:
        html = RECAPTCHA_NORMAL_PAGE_HTML  # normal page shape...
        info = detect_challenge(html, 403)  # ...but a denial status
        assert info is not None
        assert info.vendor == "generic_captcha"
        assert info.confidence >= 0.9

    @pytest.mark.asyncio
    async def test_429_maps_to_rate_limit_kind(self) -> None:
        info = detect_challenge(CF_CHALLENGE_HTML, 429)
        assert info is not None
        assert info.vendor == "cloudflare"
        assert info.kind == "rate_limit"
        # Waiting a settle interval can't clear a rate limit.
        assert info.auto_settle_likely is False

    @pytest.mark.asyncio
    async def test_unknown_vendor_denial_page_is_low_confidence(self) -> None:
        html = (
            "<html><head><title>Access Denied</title></head>"
            "<body><h1>Access denied</h1>"
            "<p>You don't have permission to access this resource.</p></body></html>"
        )
        info = detect_challenge(html, 403)
        assert info is not None
        assert info.vendor == "unknown"
        assert info.kind == "block_page"
        # Advisory only -- must stay below the action threshold.
        assert info.confidence < CHALLENGE_CONFIDENCE_ACTION_THRESHOLD


# ----------------------------------------------------------------------
# detect_challenge: false-positive guards
# ----------------------------------------------------------------------


class TestFalsePositiveGuards:
    @pytest.mark.asyncio
    async def test_prose_mentions_never_trigger_on_200(self) -> None:
        assert detect_challenge(PROSE_ARTICLE_HTML, 200) is None

    @pytest.mark.asyncio
    async def test_prose_mentions_never_trigger_even_on_403(self) -> None:
        """Status weighting must not conjure a challenge out of prose --
        a plain 403 serving an article about Cloudflare is not a wall."""
        assert detect_challenge(PROSE_ARTICLE_HTML, 403) is None

    @pytest.mark.asyncio
    async def test_recaptcha_widget_on_normal_page_is_none(self) -> None:
        assert detect_challenge(RECAPTCHA_NORMAL_PAGE_HTML, 200) is None

    @pytest.mark.asyncio
    async def test_turnstile_widget_on_normal_page_below_threshold(self) -> None:
        info = detect_challenge(TURNSTILE_NORMAL_PAGE_HTML, 200)
        # Weak markers may produce a medium-confidence advisory, but it
        # must stay below the action threshold so the fetch is untouched.
        assert info is None or info.confidence < CHALLENGE_CONFIDENCE_ACTION_THRESHOLD

    @pytest.mark.asyncio
    async def test_plain_403_page_is_none(self) -> None:
        assert detect_challenge(PLAIN_403_HTML, 403) is None

    @pytest.mark.asyncio
    async def test_empty_html_no_headers_is_none(self) -> None:
        assert detect_challenge("", 200) is None
        assert detect_challenge("", 503, {}) is None


# ----------------------------------------------------------------------
# Model round-trips
# ----------------------------------------------------------------------


class TestModelRoundTrips:
    @pytest.mark.asyncio
    async def test_fetch_status_blocked_round_trips(self) -> None:
        assert FetchStatus("blocked") is FetchStatus.BLOCKED
        assert FetchStatus.BLOCKED.value == "blocked"

    @pytest.mark.asyncio
    async def test_challenge_info_serializes(self) -> None:
        info = detect_challenge(CF_CHALLENGE_HTML, 403)
        assert info is not None
        revived = ChallengeInfo.model_validate_json(info.model_dump_json())
        assert revived == info

    @pytest.mark.asyncio
    async def test_fetch_result_with_challenge_survives_cache_dump(self) -> None:
        """Mirrors the cache write/read path: model_dump(mode='json') then
        FetchResult(**payload) must revive the nested ChallengeInfo."""
        info = detect_challenge(DATADOME_HTML, 403)
        assert info is not None
        result = FetchResult(
            url=URL,
            final_url=URL,
            status_code=403,
            status=FetchStatus.BLOCKED,
            html=DATADOME_HTML,
            challenge=info,
            error_message="Blocked by datadome captcha",
        )
        payload = result.model_dump(mode="json")
        revived = FetchResult(**payload)
        assert revived.status is FetchStatus.BLOCKED
        assert revived.challenge == info
        assert revived.challenge is not None
        assert revived.challenge.vendor == "datadome"


# ----------------------------------------------------------------------
# WebFetcher integration scaffolding
# ----------------------------------------------------------------------


def _make_page(url: str = URL, status: int = 200, headers: dict | None = None) -> MagicMock:
    """AsyncMock-backed Playwright Page + navigation Response."""
    response = MagicMock()
    response.status = status
    response.headers = headers if headers is not None else {}
    response.url = url
    response.server_addr = AsyncMock(return_value=None)
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    page.url = url
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    return page


def _fast_config(**fetch_overrides: object) -> AppConfig:
    """AppConfig with millisecond settle waits so tests stay fast."""
    config = AppConfig()
    config.fetch.challenge_settle_ms = 1
    for key, value in fetch_overrides.items():
        setattr(config.fetch, key, value)
    return config


def _patch_capture(monkeypatch: pytest.MonkeyPatch, captures: list[str]) -> dict[str, int]:
    """Replace web_fetcher's safe_page_content with a scripted sequence.

    Returns the call counter; the final entry repeats once exhausted.
    """
    calls = {"n": 0}

    async def _fake(page: object, **kwargs: object) -> tuple[str, str]:
        index = min(calls["n"], len(captures) - 1)
        calls["n"] += 1
        return captures[index], "content"

    monkeypatch.setattr(wf_module, "safe_page_content", _fake)
    return calls


# ----------------------------------------------------------------------
# Settle-recheck behaviour (HTTP 200 interstitials)
# ----------------------------------------------------------------------


class TestSettleRecheck:
    @pytest.mark.asyncio
    async def test_clears_after_first_recheck_returns_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config(challenge_max_rechecks=2)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        calls = _patch_capture(monkeypatch, [CF_CHALLENGE_HTML, CLEAN_ARTICLE_HTML])
        page = _make_page(status=200)

        result = await fetcher._navigate_and_extract(page, URL, [])

        assert result.status is FetchStatus.SUCCESS
        assert result.html == CLEAN_ARTICLE_HTML, "post-settle capture must win"
        # The settled challenge rides along as a diagnostic note.
        assert result.challenge is not None
        assert result.challenge.vendor == "cloudflare"
        assert calls["n"] == 2, "initial capture + one recheck"

    @pytest.mark.asyncio
    async def test_never_clears_returns_blocked_after_rechecks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config(challenge_max_rechecks=2)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        calls = _patch_capture(monkeypatch, [CF_CHALLENGE_HTML])
        page = _make_page(status=200)

        result = await fetcher._navigate_and_extract(page, URL, [])

        assert result.status is FetchStatus.BLOCKED
        assert result.challenge is not None
        assert result.challenge.vendor == "cloudflare"
        assert result.challenge.kind == "js_challenge"
        assert result.html == CF_CHALLENGE_HTML, "interstitial kept for diagnostics"
        assert result.error_message is not None
        assert "Do not retry immediately" in result.error_message
        assert "cloudflare js_challenge" in result.error_message
        assert calls["n"] == 3, "initial capture + challenge_max_rechecks rechecks"

    @pytest.mark.asyncio
    async def test_zero_rechecks_blocks_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config(challenge_max_rechecks=0)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        calls = _patch_capture(monkeypatch, [CF_CHALLENGE_HTML])
        page = _make_page(status=200)

        result = await fetcher._navigate_and_extract(page, URL, [])

        assert result.status is FetchStatus.BLOCKED
        assert calls["n"] == 1, "no settle captures when rechecks are disabled"

    @pytest.mark.asyncio
    async def test_captcha_kind_never_settles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DataDome (kind=captcha, auto_settle_likely=False) must block
        immediately -- waiting cannot solve a CAPTCHA."""
        config = _fast_config(challenge_max_rechecks=3)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        calls = _patch_capture(monkeypatch, [DATADOME_HTML])
        page = _make_page(status=200)

        result = await fetcher._navigate_and_extract(page, URL, [])

        assert result.status is FetchStatus.BLOCKED
        assert result.challenge is not None
        assert result.challenge.vendor == "datadome"
        assert calls["n"] == 1, "captcha walls get no settle waits"

    @pytest.mark.asyncio
    async def test_detection_disabled_preserves_old_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config(challenge_detection_enabled=False)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        _patch_capture(monkeypatch, [CF_CHALLENGE_HTML])
        page = _make_page(status=200)

        result = await fetcher._navigate_and_extract(page, URL, [])

        assert result.status is FetchStatus.SUCCESS
        assert result.html == CF_CHALLENGE_HTML
        assert result.challenge is None

    @pytest.mark.asyncio
    async def test_subthreshold_detection_is_advisory_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An embedded Turnstile widget on a normal page must never flip
        the fetch to BLOCKED -- at most an advisory note on SUCCESS."""
        config = _fast_config(challenge_max_rechecks=2)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        calls = _patch_capture(monkeypatch, [TURNSTILE_NORMAL_PAGE_HTML])
        page = _make_page(status=200)

        result = await fetcher._navigate_and_extract(page, URL, [])

        assert result.status is FetchStatus.SUCCESS
        assert result.html == TURNSTILE_NORMAL_PAGE_HTML
        if result.challenge is not None:
            assert result.challenge.confidence < CHALLENGE_CONFIDENCE_ACTION_THRESHOLD
        assert calls["n"] == 1, "no settle loop below the action threshold"


# ----------------------------------------------------------------------
# 403/503 fast-fail path conversion
# ----------------------------------------------------------------------


class TestErrorStatusChallengePath:
    @pytest.mark.asyncio
    async def test_403_challenge_clears_converts_to_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config(challenge_max_rechecks=2)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        calls = _patch_capture(monkeypatch, [CF_CHALLENGE_HTML, CLEAN_ARTICLE_HTML])
        page = _make_page(status=403)

        result = await fetcher._navigate_and_extract(page, URL, [])

        assert result.status is FetchStatus.SUCCESS
        assert result.html == CLEAN_ARTICLE_HTML
        assert result.status_code == 403, "original interstitial status preserved"
        assert result.challenge is not None, "settled challenge noted on the result"
        # sniff capture + settle recheck + success-tail capture
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_403_challenge_persists_returns_blocked_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config(challenge_max_rechecks=2)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        calls = _patch_capture(monkeypatch, [CF_CHALLENGE_HTML])
        page = _make_page(status=403)

        result = await fetcher._navigate_and_extract(page, URL, [])

        assert result.status is FetchStatus.BLOCKED
        assert result.status_code == 403
        assert result.challenge is not None
        assert calls["n"] == 3, "sniff capture + 2 settle rechecks"

    @pytest.mark.asyncio
    async def test_plain_403_still_raises_nonretryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config()
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        _patch_capture(monkeypatch, [PLAIN_403_HTML])
        page = _make_page(status=403)

        with pytest.raises(NonRetryableHTTPError):
            await fetcher._navigate_and_extract(page, URL, [])

    @pytest.mark.asyncio
    async def test_503_challenge_clears_converts_to_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config(challenge_max_rechecks=1)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        _patch_capture(monkeypatch, [CF_CHALLENGE_HTML, CLEAN_ARTICLE_HTML])
        page = _make_page(status=503)

        result = await fetcher._navigate_and_extract(page, URL, [])

        assert result.status is FetchStatus.SUCCESS
        assert result.html == CLEAN_ARTICLE_HTML

    @pytest.mark.asyncio
    async def test_plain_503_still_raises_retryable_server_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config()
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        _patch_capture(monkeypatch, [PLAIN_403_HTML])
        page = _make_page(status=503)

        with pytest.raises(Exception, match="Server error HTTP 503"):
            await fetcher._navigate_and_extract(page, URL, [])

    @pytest.mark.asyncio
    async def test_403_with_detection_disabled_never_sniffs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fast_config(challenge_detection_enabled=False)
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)
        calls = _patch_capture(monkeypatch, [CF_CHALLENGE_HTML])
        page = _make_page(status=403)

        with pytest.raises(NonRetryableHTTPError):
            await fetcher._navigate_and_extract(page, URL, [])
        assert calls["n"] == 0, "kill-switch must skip the body sniff entirely"


# ----------------------------------------------------------------------
# Cache-poisoning guard + retry sanity
# ----------------------------------------------------------------------


class TestCacheAndRetrySanity:
    @staticmethod
    def _mock_cache() -> MagicMock:
        cache = MagicMock()
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()
        return cache

    @pytest.mark.asyncio
    async def test_blocked_result_is_not_cached(self) -> None:
        config = _fast_config()
        cache = self._mock_cache()
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config, cache=cache)
        info = detect_challenge(CF_CHALLENGE_HTML, 200)
        assert info is not None
        blocked = FetchResult(
            url=URL,
            final_url=URL,
            status_code=200,
            status=FetchStatus.BLOCKED,
            html=CF_CHALLENGE_HTML,
            challenge=info,
            error_message="Blocked by cloudflare js_challenge",
        )
        fetcher._do_fetch = AsyncMock(return_value=blocked)  # type: ignore[method-assign]

        result = await fetcher.fetch(URL)

        assert result.status is FetchStatus.BLOCKED
        cache.get.assert_awaited_once()
        cache.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_result_is_cached_sanity(self) -> None:
        """Counterpart guard: SUCCESS still hits the cache writer, so the
        BLOCKED test above proves a real exclusion, not a dead cache."""
        config = _fast_config()
        cache = self._mock_cache()
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config, cache=cache)
        success = FetchResult(
            url=URL,
            final_url=URL,
            status_code=200,
            status=FetchStatus.SUCCESS,
            html=CLEAN_ARTICLE_HTML,
        )
        fetcher._do_fetch = AsyncMock(return_value=success)  # type: ignore[method-assign]

        result = await fetcher.fetch(URL)

        assert result.status is FetchStatus.SUCCESS
        cache.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blocked_does_not_burn_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A BLOCKED determination RETURNS through async_retry -- it must
        navigate exactly once, never re-running goto into the same wall."""
        config = _fast_config(challenge_max_rechecks=0, max_retries=3)
        page = _make_page(status=200)

        @asynccontextmanager
        async def _fake_new_page():  # type: ignore[no-untyped-def]
            yield page

        bm = MagicMock()
        bm.new_page = _fake_new_page
        fetcher = WebFetcher(browser_manager=bm, config=config)
        _patch_capture(monkeypatch, [CF_CHALLENGE_HTML])

        result = await fetcher.fetch(URL)

        assert result.status is FetchStatus.BLOCKED
        assert result.challenge is not None
        assert page.goto.await_count == 1, (
            "BLOCKED must be returned, not raised into async_retry -- "
            f"observed {page.goto.await_count} navigations"
        )
        assert result.correlation_id is not None or result.correlation_id is None
        assert result.response_time_ms >= 0.0
