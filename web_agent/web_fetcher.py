"""URL navigation, page rendering, and retry logic for fetching web pages.

Handles five common failure modes intelligently:

- **Download URLs** (.pdf, .doc, etc.) -- detected immediately without retrying.
- **networkidle timeouts** -- automatically fall back to ``load`` wait state.
- **HTTP 4xx** -- fail fast (non-retryable).
- **Domain not allowed** -- short-circuit with ``BLOCKED`` status.
- **Bot-challenge walls (v1.7.0)** -- Cloudflare / DataDome / Akamai /
  PerimeterX / CAPTCHA interstitials are detected structurally (even when
  served with HTTP 200), given a bounded auto-settle chance when they are
  managed JS challenges, and otherwise surfaced honestly as
  ``FetchStatus.BLOCKED`` with ``FetchResult.challenge`` + actionable
  guidance -- never returned as SUCCESS-with-garbage-HTML and never
  retried into the same wall.

Optional features:
- ``session_id`` reuses a persistent browser session for cookie continuity.
- Debug capture saves HTML/screenshot/error JSON on failure when enabled.
- Correlation IDs are echoed back into the FetchResult for tracing.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Mapping, Sequence
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .browser_manager import BrowserManager
from .cache import Cache
from .captcha import (
    CaptchaContext,
    CaptchaResolution,
    CaptchaResolver,
    normalize_resolution,
)
from .challenge import CHALLENGE_CONFIDENCE_ACTION_THRESHOLD, detect_challenge
from .config import AppConfig
from .correlation import get_correlation_id
from .debug import DebugCapture
from .metrics import MetricsRegistry, get_metrics
from .models import ChallengeInfo, FetchResult, FetchStatus, HtmlCaptureSource
from .rate_limiter import RateLimiter
from .robots import RobotsChecker
from .session_manager import SessionManager
from .utils import (
    NonRetryableHTTPError,
    Timer,
    async_retry,
    check_domain_allowed,
    get_random_user_agent,
    httpx_peer_ip,
    is_private_address,
    locale_os_family,
    parse_retry_after,
    safe_page_content,
)

# Common office documents and archives -- shared between the fetcher
# and the downloader so the two modules can't drift on what counts as
# "binary content we extract specially". Image / audio / video / OS
# installer extensions are NOT here; they belong only to the fetcher's
# DOWNLOAD set (we route around them) and are explicitly NOT in the
# downloader's binary set (we don't extract them as documents).
_OFFICE_AND_ARCHIVE_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".tar",
        ".gz",
        ".rar",
        ".7z",
        ".csv",
        ".tsv",
    }
)


# File extensions that trigger browser downloads instead of rendering.
# Superset of _OFFICE_AND_ARCHIVE_EXTENSIONS plus media + OS installers
# which we want to route around even though we can't extract them.
_DOWNLOAD_EXTENSIONS = _OFFICE_AND_ARCHIVE_EXTENSIONS | frozenset(
    {
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
        ".exe",
        ".msi",
        ".dmg",
        ".deb",
        ".rpm",
        ".iso",
        ".img",
    }
)


def _is_download_url(url: str) -> bool:
    """Check if a URL points to a file that would trigger a browser download."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _DOWNLOAD_EXTENSIONS)


# v1.6.10: the granular classification kinds returned by
# :func:`_url_ext_classification` and :meth:`WebFetcher.classify_url`.
# Callers route on these via :func:`is_binary_kind` instead of comparing
# against the literal string ``"binary"`` (the v1.6.9 return value).
# Migration: ``c == "binary"`` -> ``is_binary_kind(c)``.
_BINARY_KINDS: frozenset[str] = frozenset({"pdf", "xlsx", "docx", "csv", "zip", "binary_other"})


def is_binary_kind(kind: str) -> bool:
    """True iff *kind* is any non-HTML binary classification.

    Used by :meth:`WebFetcher.fetch_smart`, :meth:`Recipes.find_and_download_file`,
    and :meth:`Agent.search_and_extract` to route URLs through
    :meth:`fetch_binary` vs :meth:`fetch`. Public-stable as of v1.6.10.
    """
    return kind in _BINARY_KINDS


# v1.6.11: subset of :data:`_BINARY_KINDS` that the ContentExtractor can
# extract text from. ``zip`` and ``binary_other`` are classified binary but
# yield no text -- the v1.6.10 I-1 guard catches
# ``extraction_method == "none" AND content_length == 0`` AFTER a fetch;
# this set lets callers filter BEFORE the fetch (e.g.
# ``web_research(extract_files=True)`` skips ``.mp4`` / ``.exe`` / ``.iso``
# / ``.zip`` without ever fetching them).
EXTRACTABLE_BINARY_KINDS: frozenset[str] = frozenset({"pdf", "xlsx", "docx", "csv"})


def is_extractable_binary_kind(kind: str) -> bool:
    """True iff *kind* is a binary the ContentExtractor handles.

    Use to filter URLs before passing to :meth:`WebFetcher.fetch_smart`
    when the downstream consumer is the binary extractor (e.g.
    :meth:`Recipes.web_research` with ``extract_files=True``). Returns
    False for ``"zip"``, ``"binary_other"``, ``"html"``, and ``"unknown"``.
    Public-stable as of v1.6.11.
    """
    return kind in EXTRACTABLE_BINARY_KINDS


# Content-Type prefixes / fragments that mean "this is a binary document".
# trafilatura/bs4 cannot do anything useful with these — they belong to
# the binary extraction branch (PDF/XLSX/DOCX/CSV).
_BINARY_CONTENT_TYPE_HINTS = (
    "application/pdf",
    "application/vnd.ms-excel",  # legacy XLS (detected, not extracted yet)
    "application/vnd.openxmlformats-officedocument.spreadsheetml",  # XLSX
    "application/vnd.openxmlformats-officedocument.wordprocessingml",  # DOCX
    "application/vnd.openxmlformats-officedocument.presentationml",  # PPTX
    "application/msword",
    "application/vnd.ms-powerpoint",
    "application/zip",
    "application/x-zip",
    "application/octet-stream",
    "application/x-bzip",
    "application/x-rar",
    "application/x-7z-compressed",
    "text/csv",
    "text/tab-separated-values",
)


def _content_type_is_binary(content_type: str | None) -> bool:
    """True if a Content-Type header value indicates a binary document."""
    if not content_type:
        return False
    ct = content_type.lower().split(";", 1)[0].strip()
    return any(ct.startswith(hint) for hint in _BINARY_CONTENT_TYPE_HINTS)


def _disposition_is_attachment(disposition: str | None) -> bool:
    """True if a Content-Disposition header is an attachment."""
    if not disposition:
        return False
    return "attachment" in disposition.lower()


# v1.7.0: HTTP statuses whose fast-fail path is worth sniffing for a bot
# challenge before giving up. 403 + 503 are the canonical Cloudflare /
# vendor challenge statuses; 429 is deliberately excluded -- it already
# has dedicated Retry-After / rate-limiter handling in the fetch path.
_CHALLENGE_SNIFF_STATUS_CODES: frozenset[int] = frozenset({403, 503})


def _response_headers(response: Any) -> dict[str, str]:
    """Best-effort lowercase-keyed header dict from a Playwright Response.

    ``Response.headers`` is a sync property in the async API (header names
    already lowercased by Playwright). Defensive against mocks / odd
    transports: anything that isn't a real mapping yields ``{}`` -- header
    evidence is an enrichment for challenge detection, never load-bearing.
    """
    if response is None:
        return {}
    try:
        raw = response.headers
    except Exception:  # pragma: no cover -- defensive
        return {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        try:
            out[str(key).lower()] = str(value)
        except Exception:  # pragma: no cover -- defensive
            continue
    return out


# Common HTML page extensions. URLs with these extensions are clearly
# HTML and don't need a HEAD probe -- skipping them keeps search-result
# pre-classification cheap.
_HTML_EXTENSIONS = frozenset(
    {
        ".html",
        ".htm",
        ".xhtml",
        ".aspx",
        ".asp",
        ".php",
        ".jsp",
        ".cgi",
        ".shtml",
        ".phtml",
    }
)


# v1.6.10: map known document extensions to granular kinds. Anything in
# ``_DOWNLOAD_EXTENSIONS`` that is not in this dict (mp3/mp4/exe/iso/...)
# falls through to ``"binary_other"`` via the catch-all in
# :func:`_url_ext_classification`. Keep the keys in sync with
# ``_OFFICE_AND_ARCHIVE_EXTENSIONS`` above.
_EXT_TO_KIND: dict[str, str] = {
    ".pdf": "pdf",
    ".doc": "docx",
    ".docx": "docx",
    ".xls": "xlsx",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".tsv": "csv",
    ".zip": "zip",
    ".tar": "zip",
    ".gz": "zip",
    ".rar": "zip",
    ".7z": "zip",
}


# v1.6.10: map Content-Type prefixes to granular kinds. Content types not
# in this dict but matching ``_BINARY_CONTENT_TYPE_HINTS`` (PPTX, generic
# octet-stream, ...) collapse to ``"binary_other"`` so the routing layer
# still hits :meth:`fetch_binary` while the extractor stays free to do
# nothing with them.
_CT_TO_KIND: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml": "xlsx",
    "application/vnd.ms-excel": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml": "docx",
    "application/msword": "docx",
    "text/csv": "csv",
    "text/tab-separated-values": "csv",
}


def _url_ext_classification(url: str) -> str:
    """Classify a URL by its extension alone, without any network I/O.

    v1.6.10: returns one of ``'pdf' | 'xlsx' | 'docx' | 'csv' | 'zip' |
    'binary_other' | 'html' | 'unknown'``. Callers route via
    :func:`is_binary_kind` rather than comparing to a single ``"binary"``
    string (the v1.6.9 return). Used as the fast pre-filter before HEAD
    probing.
    """
    path = urlparse(url).path.lower()
    for ext, kind in _EXT_TO_KIND.items():
        if path.endswith(ext):
            return kind
    if any(path.endswith(ext) for ext in _DOWNLOAD_EXTENSIONS):
        return "binary_other"
    if any(path.endswith(ext) for ext in _HTML_EXTENSIONS):
        return "html"
    return "unknown"


async def _response_peer_is_private(response: Any) -> bool:
    """Return True if a Playwright Response's actual peer IP is private.

    v1.6.14 C-1(b): closes post-connect DNS rebinding for the navigation
    path. ``check_domain_allowed`` validates the host against *cached*
    DNS, but Chromium re-resolves at connect time -- so a host that
    resolved public at check time can rebind to an internal address
    (169.254.169.254, RFC1918, loopback) before the actual TCP connect.
    Playwright exposes the real peer via ``Response.server_addr()`` (a
    dict-like with ``ipAddress`` / ``port``, or ``None``); we re-run the
    private-IP check on that concrete address.

    Returns False (cannot prove private) when ``response`` is None, when
    ``server_addr`` is unavailable/None, or when no ``ipAddress`` is
    present -- the caller still has the host-level gate as the first line.
    """
    if response is None:
        return False
    try:
        server_addr = await response.server_addr()
    except Exception:  # pragma: no cover -- defensive (older Playwright)
        return False
    if not server_addr:
        return False
    # server_addr is a TypedDict-like mapping: {"ipAddress": str, "port": int}.
    ip_address = ""
    try:
        ip_address = (server_addr.get("ipAddress") or "").strip()
    except AttributeError:  # pragma: no cover -- unexpected shape
        return False
    if not ip_address:
        return False
    return is_private_address(ip_address)


class WebFetcher:
    """Fetches web pages using Playwright with retry, safety, and debug support.

    Args:
        browser_manager: Shared browser lifecycle manager.
        config: Application configuration.
        sessions: Optional SessionManager for persistent browser sessions.
        debug: Optional DebugCapture for failure artifact capture.
        captcha_resolver: Optional in-process CAPTCHA / bot-challenge
            resolver hook (see :mod:`web_agent.captcha`). Invoked on a
            standing wall before BLOCKED; None disables the hook.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        config: AppConfig,
        sessions: Optional[SessionManager] = None,
        debug: Optional[DebugCapture] = None,
        rate_limiter: Optional[RateLimiter] = None,
        robots: Optional[RobotsChecker] = None,
        cache: Optional[Cache] = None,
        network_collector: Optional[Any] = None,
        metrics: Optional[MetricsRegistry] = None,
        captcha_resolver: Optional[CaptchaResolver] = None,
    ) -> None:
        self._bm = browser_manager
        self._config = config
        self._sessions = sessions
        self._debug = debug or DebugCapture(config)
        self._rate_limiter = rate_limiter
        self._robots = robots
        self._cache = cache
        # v1.6.8: shared NetworkCollector -- when set, fetch() copies the
        # per-Page events into FetchResult.network_events / api_candidates /
        # download_candidates_runtime on success. None when the Agent
        # didn't provide one (older test scaffolding).
        self._network_collector = network_collector
        # v1.7.0 (Wave 4A): observability registry. Defaults to a shared
        # no-op registry when the Agent doesn't pass one, so every increment
        # is a single cheap call (no per-site None guard) and existing call
        # sites are unaffected.
        self._metrics = get_metrics(metrics)
        # v1.7.0 (Wave 7): optional in-process CAPTCHA / bot-challenge
        # resolver hook. None == no hook (a standing wall returns BLOCKED
        # exactly as before). Mutable post-construction via the property.
        self._captcha_resolver = captcha_resolver

    @property
    def captcha_resolver(self) -> Optional[CaptchaResolver]:
        """The configured CAPTCHA / bot-challenge resolver hook, or None."""
        return self._captcha_resolver

    @captcha_resolver.setter
    def captcha_resolver(self, resolver: Optional[CaptchaResolver]) -> None:
        self._captcha_resolver = resolver

    # ------------------------------------------------------------------
    # v1.6.12: shared 429 signalling helper
    # ------------------------------------------------------------------

    def _signal_429(self, response: Any, url: str, host: str) -> float | None:
        """v1.6.12: parse ``Retry-After`` and notify the rate limiter.

        Used by BOTH the HTML path (``_fetch_with_retry`` -- which then
        raises a retryable Exception) and the binary path
        (``fetch_binary`` -- which then returns ``FetchResult(status=
        HTTP_ERROR)`` since the binary path is not retry-wrapped).
        Does NOT raise itself -- callers decide on the post-signal
        action.

        Args:
            response: Playwright Response (HTML path) or httpx Response
                (binary path). Both expose ``.headers`` as a mapping
                with lowercase keys.
            url: Full URL of the request (for logging context only).
            host: Hostname for the rate-limiter signal.

        Returns:
            Parsed ``Retry-After`` value in seconds, or ``None`` when
            the header was absent or unparseable. Useful for callers
            wanting to include the value in an error message.
        """
        retry_after: float | None = None
        try:
            if response is not None:
                retry_after = parse_retry_after(response.headers.get("retry-after"))
        except Exception:  # pragma: no cover -- defensive
            pass
        if self._rate_limiter is not None:
            self._rate_limiter.notify_429(host, retry_after)
        return retry_after

    # ------------------------------------------------------------------
    # v1.7.0 (Wave 4A): observability -- record at the outcome chokepoint
    # ------------------------------------------------------------------

    def _record_fetch_outcome(self, result: FetchResult) -> FetchResult:
        """Stamp ``fetch_total`` + ``fetch_outcome{status}`` for one result.

        Called at the single return chokepoint of :meth:`fetch` so every
        finalized FetchResult is counted exactly once (cache hits, blocks,
        successes, and the exception-to-FetchResult conversions all funnel
        here). ``challenge_detected{vendor}`` is incremented when the result
        carries challenge info -- the structural bot-wall signal. Counters
        only; no control-flow or value changes. Returns ``result`` so callers
        can ``return self._record_fetch_outcome(...)`` inline.
        """
        self._metrics.incr("fetch_total")
        self._metrics.incr("fetch_outcome", status=result.status.value)
        if result.challenge is not None:
            self._metrics.incr("challenge_detected", vendor=result.challenge.vendor)
        return result

    def _observe_fetch_payload(self, result: FetchResult) -> None:
        """Record ``bytes_downloaded`` / ``ttfb_ms`` distributions for a fetch.

        Best-effort, success-path enrichment: ``bytes_downloaded`` prefers the
        captured page weight (``total_bytes_downloaded``, present only when
        network capture is on) and falls back to the rendered HTML length;
        ``ttfb_ms`` is recorded when the navigation TTFB was captured. No-op
        on the disabled registry. Never raises.
        """
        if not self._metrics.enabled:
            return
        size = result.total_bytes_downloaded
        if size is None and result.html is not None:
            size = len(result.html)
        if size is not None:
            self._metrics.observe("bytes_downloaded", float(size))
        if result.ttfb_ms is not None:
            self._metrics.observe("ttfb_ms", float(result.ttfb_ms))

    # ------------------------------------------------------------------
    # v1.7.0 (Wave 2F): coherent identity for the httpx side-paths
    # ------------------------------------------------------------------

    def _coherent_side_path_headers(self) -> dict[str, str]:
        """Minimal browser-consistent header set for the httpx side-paths.

        The HEAD probe and ``fetch_binary`` previously sent only a random
        User-Agent on top of httpx's default Python identity, so a request
        could claim a Chrome UA while advertising ``Accept: */*`` and no
        ``Accept-Language`` -- a trivially incoherent fingerprint that does
        not match what the browser path sends for the SAME operator.

        This builds a coherent triplet:

        * ``User-Agent`` -- drawn from the SAME coherence policy as the
          browser context (OS family pinned by ``BrowserConfig.locale``
          when ``coherent_fingerprint`` is on), so the side-path UA OS does
          not contradict the browser's; otherwise the full rotation pool.
        * ``Accept-Language`` -- derived from ``BrowserConfig.locale`` (e.g.
          ``en-US`` -> ``en-US,en;q=0.9``) so it matches the browser's
          locale claim.
        * ``Accept`` -- a browser-like document Accept string.

        Returns a fresh dict each call (UA rotates per request).
        """
        bcfg = self._config.browser
        os_family = (
            locale_os_family(bcfg.locale) if getattr(bcfg, "coherent_fingerprint", True) else None
        )
        locale = (bcfg.locale or "en-US").strip() or "en-US"
        primary = locale.split(",", 1)[0].strip()
        # Build "en-US,en;q=0.9" from "en-US"; for a bare "en" just "en".
        base_lang = primary.split("-", 1)[0]
        accept_language = (
            f"{primary},{base_lang};q=0.9" if base_lang and base_lang != primary else primary
        )
        return {
            "User-Agent": get_random_user_agent(os_family),
            "Accept-Language": accept_language,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
        }

    def _httpx_proxy_kwargs(self) -> dict[str, Any]:
        """v1.7.0 (Wave 2F): ``proxy=`` kwarg for the httpx side-paths.

        Returns ``{"proxy": <url>}`` when ``ProxyConfig.server`` is set,
        else an EMPTY dict so the ``proxy`` key is omitted entirely from
        ``httpx.AsyncClient(...)`` (httpx 0.28 takes a single ``proxy=``
        URL; ``proxies=`` was removed). Splatting an empty dict keeps the
        "absent, not None" contract the launch path also follows.
        """
        proxy_url = self._config.proxy.httpx_proxy_url()
        return {"proxy": proxy_url} if proxy_url is not None else {}

    async def fetch_smart(
        self,
        url: str,
        *,
        session_id: Optional[str] = None,
        binary_probe: bool = True,
    ) -> FetchResult:
        """v1.6.9: single source of truth for binary-vs-HTML routing.

        Resolution order:
          1. Known download extension (.pdf/.xlsx/.docx/.csv/...) -> :meth:`fetch_binary`.
          2. ``binary_probe=True`` AND :attr:`SafetyConfig.probe_binary_urls`
             AND :func:`is_binary_kind` returns True for the ``classify_url``
             result -> :meth:`fetch_binary`. (v1.6.10: ``classify_url`` returns
             granular kinds -- ``pdf | xlsx | docx | csv | zip | binary_other``
             -- and :func:`is_binary_kind` is the public migration helper.)
          3. Otherwise -> :meth:`fetch` (HTML path).

        Used by :class:`Agent` and :class:`Recipes` so the rules are
        defined once. Prior to v1.6.9, recipes like
        ``search_and_open_best_result`` called ``fetch()`` directly,
        which sent extensionless binary URLs (Content-Type: application/pdf)
        into the HTML extractor and produced garbage.

        Args:
            url: The URL to route.
            session_id: Optional persistent browser session.
            binary_probe: When True (default), HEAD-probe extensionless
                URLs to detect binary documents via Content-Type /
                Content-Disposition. Disable to skip the probe.

        Returns:
            FetchResult from whichever underlying method handled the URL.
        """
        # v1.6.10: classification is now one of the granular kinds
        # ({pdf, xlsx, docx, csv, zip, binary_other, html, unknown}) so
        # the routing check goes through ``is_binary_kind`` instead of
        # comparing to the v1.6.9 single string ``"binary"``.
        classification = "html"
        if _is_download_url(url):
            classification = _url_ext_classification(url)
        elif binary_probe and self._config.safety.probe_binary_urls:
            classification = await self.classify_url(url, session_id=session_id)
        if is_binary_kind(classification):
            return await self.fetch_binary(url, session_id=session_id)
        return await self.fetch(url, session_id=session_id)

    async def fetch(
        self,
        url: str,
        session_id: Optional[str] = None,
    ) -> FetchResult:
        """Navigate to URL, wait for rendering, and return HTML content.

        Args:
            url: The URL to fetch.
            session_id: Optional persistent browser session to use.

        Returns:
            FetchResult with HTML on success, or error details on failure.
        """
        cid = get_correlation_id()

        # Domain allow/deny gate
        if not check_domain_allowed(url, self._config.safety):
            host = urlparse(url).hostname or ""
            logger.warning("Domain blocked by safety policy: {host}", host=host)
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.BLOCKED,
                    error_message=f"Domain not allowed by SafetyConfig: {host}",
                    correlation_id=cid,
                )
            )

        # Fast-path: detect file download URLs without launching a browser page
        if _is_download_url(url):
            logger.info(
                "Skipping fetch for download URL: {url} (use agent.download() instead)",
                url=url,
            )
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.NETWORK_ERROR,
                    error_message=(
                        f"URL points to a downloadable file. "
                        f"Use agent.download('{url}') instead of fetch."
                    ),
                    correlation_id=cid,
                )
            )

        # Politeness layer: robots.txt check (before any network I/O)
        if self._robots is not None and not await self._robots.is_allowed(url):
            host = urlparse(url).hostname or ""
            logger.info("robots.txt disallows {url} for {ua}", url=url, ua=self._robots.user_agent)
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.BLOCKED,
                    error_message=(
                        f"robots.txt for {host} disallows this URL "
                        f"for User-Agent {self._robots.user_agent!r}"
                    ),
                    correlation_id=cid,
                )
            )

        # Cache lookup. NOTE: robots.txt has already been checked above --
        # this is intentional. If robots.txt now disallows a path that we
        # cached earlier (under more permissive rules), we want the new
        # disallow to win. Compliance > a few extra robots fetches per
        # cached URL (those are themselves cached by RobotsChecker).
        # Cache lookup runs before the rate limiter so a cache hit
        # doesn't burn a per-host token.
        #
        # v1.6.14 C-3: fold session identity into the cache key. The
        # pre-v1.6.14 key was ``f"fetch:{url}"`` with no session/cookie
        # identity, so authenticated HTML fetched inside session A could
        # be served to a request running under session B (cross-session
        # data leak). Namespacing by ``session_id`` isolates persistent
        # sessions from each other; the ephemeral (no ``session_id``)
        # path keeps a single shared ``'ephemeral'`` namespace, preserving
        # its prior cross-call sharing behaviour exactly.
        cache_key = f"fetch:{session_id or 'ephemeral'}:{url}"
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Cache hit: {url}", url=url)
                cached["correlation_id"] = cid
                cached["from_cache"] = True
                return self._record_fetch_outcome(FetchResult(**cached))

        # Politeness layer: per-host rate limit (may sleep).
        #
        # v1.6.16 FE-1: the acquire moved INTO the retry loop
        # (``_do_fetch`` -> ``_fetch_with_retry``) so it is re-acquired on
        # every attempt. Previously it ran once here, before the retry
        # loop, so a 429's ``notify_429`` extension of ``_next_allowed`` was
        # ignored by in-loop retries (they waited only async_retry's jitter,
        # not Retry-After). Re-acquiring per attempt honours the extended
        # deadline. The cache lookup above still short-circuits before
        # ``_do_fetch``, so a cache hit never burns a per-host token.
        timer = Timer()
        try:
            with timer:
                result = await self._do_fetch(url, session_id=session_id)
            result.correlation_id = cid
            # v1.6.16 fix: stamp the measured elapsed time on the success path.
            # ``_navigate_and_extract`` builds the FetchResult without
            # response_time_ms (only the error returns below set it), so the
            # HTML success path -- and its cached payload -- previously always
            # reported 0.0. Read AFTER the ``with`` exits (so the timer is
            # finalised) and set BEFORE caching so the cached value is honest.
            result.response_time_ms = timer.elapsed_ms
            # Cache successful fetches only -- caching errors / blocks
            # would lock in transient failures across the TTL window.
            # Key matches the lookup above (session-namespaced, C-3).
            if self._cache is not None and result.status == FetchStatus.SUCCESS:
                await self._cache.set(cache_key, result.model_dump(mode="json"))
            # v1.7.0 (Wave 4A): record the success/blocked/etc. outcome AND
            # the bytes/ttfb distributions on the path that actually
            # navigated. Counters only -- _do_fetch already finalized the
            # result; we observe here so the no-op fast paths above don't pay.
            self._observe_fetch_payload(result)
            return self._record_fetch_outcome(result)
        except NonRetryableHTTPError as e:
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status_code=e.status_code,
                    status=FetchStatus.HTTP_ERROR,
                    error_message=str(e),
                    response_time_ms=timer.elapsed_ms,
                    correlation_id=cid,
                )
            )
        except PlaywrightTimeout:
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.TIMEOUT,
                    error_message="Navigation timed out",
                    response_time_ms=timer.elapsed_ms,
                    correlation_id=cid,
                )
            )
        except Exception as e:
            error_msg = str(e)
            if "download is starting" in error_msg.lower():
                logger.info(
                    "URL triggered a download: {url} (use agent.download() instead)",
                    url=url,
                )
                return self._record_fetch_outcome(
                    FetchResult(
                        url=url,
                        final_url=url,
                        status=FetchStatus.NETWORK_ERROR,
                        error_message=(
                            f"URL triggered a file download. "
                            f"Use agent.download('{url}') instead of fetch."
                        ),
                        response_time_ms=timer.elapsed_ms,
                        correlation_id=cid,
                    )
                )
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.NETWORK_ERROR,
                    error_message=error_msg,
                    response_time_ms=timer.elapsed_ms,
                    correlation_id=cid,
                )
            )

    async def _do_fetch(
        self,
        url: str,
        session_id: Optional[str] = None,
    ) -> FetchResult:
        """Internal fetch with retry and networkidle->load fallback."""
        cfg = self._config.fetch

        # Outer scope -- accumulates debug artifacts across all retry attempts
        # so failures-then-success leaves the saved file paths visible to the
        # caller via FetchResult.debug_artifacts.
        debug_artifacts: list[str] = []
        # Persist the wait strategy across retries so once networkidle fails
        # for a given URL, subsequent retries use 'load' directly (Phase D2).
        wait_strategy = [cfg.wait_until]  # list for nonlocal-style mutation

        @async_retry(
            max_retries=cfg.max_retries,
            base_delay=cfg.retry_base_delay,
            max_delay=cfg.retry_max_delay,
            non_retryable_exceptions=(NonRetryableHTTPError, _DownloadStartedError),
        )
        async def _fetch_with_retry() -> FetchResult:
            # v1.6.16 FE-1: acquire the per-host token on EVERY attempt so a
            # 429's ``notify_429`` extension of ``_next_allowed`` (set inside
            # ``_navigate_and_extract`` via ``_signal_429``) is honoured by
            # the next retry. ``acquire`` re-reads ``_next_allowed`` after
            # each sleep, so the extended Retry-After deadline now actually
            # delays the retry instead of being ignored.
            if self._rate_limiter is not None:
                await self._rate_limiter.acquire(urlparse(url).hostname or "")
            page: Page
            if session_id and self._sessions is not None:
                ctx = self._sessions.get(session_id)
                self._sessions.touch(session_id)
                page = await ctx.new_page()
                # v1.6.8: attach network capture (no-op when disabled). The
                # SessionManager wires the collector into TabManager for
                # session-owned tabs, but this `ctx.new_page()` call creates
                # a one-off page outside TabManager's awareness, so we must
                # attach explicitly here.
                if self._network_collector is not None:
                    self._network_collector.attach(page)
                try:
                    return await self._navigate_and_extract(
                        page, url, debug_artifacts, wait_strategy
                    )
                finally:
                    await page.close()
            else:
                # BrowserManager.new_page() already attaches the collector
                # for ephemeral pages.
                async with self._bm.new_page() as page:
                    return await self._navigate_and_extract(
                        page, url, debug_artifacts, wait_strategy
                    )

        result: FetchResult = await _fetch_with_retry()
        # Final aggregated artifacts list -- overrides whatever the inner call
        # populated, so the caller sees ALL captures across retries.
        result.debug_artifacts = list(debug_artifacts)
        return result

    async def _navigate_and_extract(
        self,
        page: Page,
        url: str,
        debug_artifacts: list[str],
        wait_strategy_box: Sequence[str] | None = None,
    ) -> FetchResult:
        """Perform the actual navigation + content read on an open page.

        ``wait_strategy_box`` is a single-element list used by ``_do_fetch``
        to persist the wait strategy across retries: once networkidle fails
        for this URL, the box is updated to ``"load"`` so subsequent retries
        skip the doomed networkidle attempt entirely (Phase D2). Annotated
        ``Sequence`` (covariant) so callers may pass Literal-typed lists; the
        retry-persistence write-back only happens when the box is a real list.
        """
        cfg = self._config.fetch
        if wait_strategy_box is None:
            wait_strategy_box = [cfg.wait_until]
        wait_strategy = wait_strategy_box[0]
        try:
            try:
                response = await page.goto(url, wait_until=wait_strategy)  # type: ignore[arg-type]
            except PlaywrightError as e:
                if "download is starting" in str(e).lower():
                    raise _DownloadStartedError(url) from e
                raise
            except PlaywrightTimeout:
                if wait_strategy == "networkidle":
                    logger.debug(
                        "networkidle timed out for {url}, falling through to 'load' "
                        "(persisted for subsequent retries)",
                        url=url,
                    )
                    if isinstance(wait_strategy_box, list):
                        wait_strategy_box[0] = "load"  # persist across retries
                    response = await page.goto(url, wait_until="load")
                else:
                    raise

            status_code = response.status if response else None

            # Re-validate the URL after Playwright follows any redirects
            # (defense-in-depth SSRF protection: a whitelisted host could
            # redirect to a private IP / denied domain).
            #
            # v1.6.14 C-7: also validate the server-side final URL
            # (``response.url``), not just ``page.url``. A meta-refresh or
            # ``Location:`` redirect to an internal host is reflected in
            # the navigation Response's URL even when ``page.url`` lags or
            # is rewritten client-side, so checking both closes that gap.
            final_url = page.url
            response_url = response.url if response is not None else None
            for candidate in (final_url, response_url):
                if (
                    candidate
                    and candidate != url
                    and not check_domain_allowed(candidate, self._config.safety)
                ):
                    from .exceptions import NavigationError

                    raise NavigationError(
                        f"Page redirected to disallowed URL: {candidate}",
                        url=candidate,
                        status_code=status_code,
                    )

            # v1.6.14 C-1(b): re-check the ACTUAL connected peer IP. The
            # host-level gate above uses cached DNS; Chromium re-resolves
            # at connect time, so a rebind to an internal address would
            # otherwise slip through. ``response.server_addr()`` reports
            # the real peer; block it when private-IP protection is on.
            if getattr(self._config.safety, "block_private_ips", False) and (
                await _response_peer_is_private(response)
            ):
                from .exceptions import NavigationError

                raise NavigationError(
                    f"Navigation connected to a private/loopback/link-local "
                    f"peer for {url} (post-connect DNS-rebinding guard)",
                    url=final_url,
                    status_code=status_code,
                )

            # v1.7.0: bot-wall sniff on the HTTP-error fast-fail path. A
            # 403/503 whose body is a managed JS challenge (Cloudflare et
            # al.) is auto-passable by a real browser in a few seconds --
            # give it a bounded settle-recheck chance BEFORE fast-failing.
            # Outcomes:
            #   - FetchResult   -> still challenged after the settle budget:
            #     return BLOCKED. A determination, NOT an exception --
            #     raising would send async_retry re-navigating into the
            #     same wall.
            #   - ChallengeInfo -> the challenge auto-settled: skip the
            #     error raises below; the page now shows real content and
            #     the normal success tail captures it.
            #   - None          -> no actionable challenge: the original
            #     fast-fail semantics apply unchanged.
            settled_challenge: Optional[ChallengeInfo] = None
            if (
                cfg.challenge_detection_enabled
                and status_code is not None
                and status_code in _CHALLENGE_SNIFF_STATUS_CODES
            ):
                outcome = await self._challenge_outcome_for_error_status(
                    page, url, response, status_code
                )
                if isinstance(outcome, FetchResult):
                    return outcome
                settled_challenge = outcome
                if settled_challenge is not None:
                    final_url = self._recheck_url_after_settle(page, url, status_code)

            if settled_challenge is None:
                if status_code and status_code in cfg.non_retryable_status_codes:
                    raise NonRetryableHTTPError(status_code, url)
                if status_code == 429:
                    # v1.6.12: signal the rate limiter (extends next_allowed
                    # so the next ``acquire(host)`` waits Retry-After or
                    # fallback) then raise a retryable Exception so
                    # ``async_retry`` retries with the new wait honoured.
                    # Helper shared with ``fetch_binary``.
                    host = urlparse(url).hostname or ""
                    retry_after = self._signal_429(response, url, host)
                    raise Exception(
                        f"HTTP 429 Too Many Requests for {url}"
                        + (f" (Retry-After: {retry_after}s)" if retry_after is not None else "")
                    )
                if status_code and status_code >= 500:
                    raise Exception(f"Server error HTTP {status_code}")

            if cfg.wait_for_selector:
                await page.wait_for_selector(cfg.wait_for_selector, timeout=10000)

            if cfg.extra_wait_ms > 0:
                await asyncio.sleep(cfg.extra_wait_ms / 1000)

            # v1.6.13: capture via the 3-tier helper so the
            # mid-navigation race ("Unable to retrieve content because the
            # page is navigating and changing the content") doesn't kill
            # an otherwise-successful fetch. Tier 1 = page.content() with
            # bounded retry; tier 2 = page.evaluate(outerHTML); tier 3 =
            # CDP DOM.getOuterHTML. The chosen tier is surfaced on
            # ``FetchResult.html_capture_source`` for telemetry.
            html, html_capture_source = await safe_page_content(page)
            if html_capture_source == "navigating":
                logger.warning(
                    "page.content() abandoned after all tiers for {url} -- "
                    "FetchResult.html will be empty",
                    url=url,
                )

            # v1.7.0: bot-wall honesty on the nominal-success path. Vendors
            # routinely serve challenge interstitials WITH HTTP 200; before
            # v1.7.0 that garbage HTML came back as SUCCESS and got
            # extracted as if it were content. Detect structurally, give
            # auto-settle-likely challenges the bounded settle-recheck, and
            # return an honest BLOCKED when the wall persists. Detections
            # below the action threshold (e.g. an embedded CAPTCHA widget
            # on a normal page) ride along as an advisory on the SUCCESS
            # result. Skipped when the 403/503 sniff above already settled
            # a challenge this navigation (its budget is spent).
            challenge_note: Optional[ChallengeInfo] = settled_challenge
            if cfg.challenge_detection_enabled and settled_challenge is None:
                detected = detect_challenge(
                    html, status_code, _response_headers(response), final_url
                )
                if detected is not None:
                    if detected.confidence >= CHALLENGE_CONFIDENCE_ACTION_THRESHOLD:
                        still: Optional[ChallengeInfo] = detected
                        if detected.auto_settle_likely and cfg.challenge_max_rechecks > 0:
                            still, html, html_capture_source = await self._settle_challenge(
                                page, url, detected, html, html_capture_source
                            )
                        resolved_via_hook = False
                        if still is not None:
                            # Settle couldn't clear it (or it was never
                            # auto-settle-likely -- a CAPTCHA / block page):
                            # give a configured resolver hook a bounded,
                            # re-verified chance before returning BLOCKED.
                            still, html, html_capture_source = (
                                await self._attempt_captcha_resolution(
                                    page, url, still, html, html_capture_source
                                )
                            )
                            resolved_via_hook = still is None
                        if still is not None:
                            return self._blocked_challenge_result(
                                url=url,
                                final_url=page.url or final_url,
                                status_code=status_code,
                                html=html,
                                info=still,
                            )
                        # Cleared: the page re-navigated to real content
                        # (auto-settle and/or the resolver hook). The advisory
                        # note records whether the hook was what cleared it.
                        challenge_note = (
                            self._mark_resolution(detected, succeeded=True)
                            if resolved_via_hook
                            else detected
                        )
                        final_url = self._recheck_url_after_settle(page, url, status_code)
                    else:
                        challenge_note = detected

            # v1.6.12: true DOM parse time = ``domInteractive -
            # responseEnd`` (time from "response fully received" to
            # "DOM tree built"). Earlier draft used ``domComplete -
            # domInteractive`` but that includes subresource loading +
            # deferred-script execution -- it's post-parse load time,
            # not parse time. Best-effort + swallowed on failure;
            # ``about:blank`` / ``data:`` URLs and sandboxed iframes
            # don't expose the Navigation Timing API.
            dom_parse_ms: float | None = None
            try:
                dom_val = await page.evaluate(
                    "() => {"
                    " const entries = performance.getEntriesByType('navigation');"
                    " if (!entries.length) return null;"
                    " const nav = entries[0];"
                    " if (typeof nav.domInteractive !== 'number' "
                    "|| typeof nav.responseEnd !== 'number') return null;"
                    " return Math.max(0, nav.domInteractive - nav.responseEnd);"
                    "}"
                )
                if dom_val is not None:
                    dom_parse_ms = round(float(dom_val), 2)
            except Exception:  # pragma: no cover -- defensive
                pass

            # v1.6.8: snapshot network events / api_candidates / download
            # intents BEFORE the caller closes the Page. The deques live
            # in a WeakKeyDictionary so a closed Page evicts them; we
            # capture into lists now and let the result outlive the Page.
            net_events: list = []
            api_cands: list[str] = []
            dl_intents: list[str] = []
            if self._network_collector is not None:
                # v1.6.12: when response-body capture is on, drain any
                # in-flight body reads BEFORE snapshotting so the
                # captured bodies land on the events the caller sees.
                # Bounded wait (5s default) so a stuck body never
                # blocks the fetch indefinitely.
                if self._config.diagnostics.capture_response_bodies:
                    await self._network_collector.wait_for_pending_bodies()
                net_events = self._network_collector.events_for(page)
                api_cands = self._network_collector.api_candidates_for(page)
                dl_intents = self._network_collector.download_intents_for(page)

            # v1.6.12: aggregate per-fetch telemetry from network events.
            # ``ttfb_ms`` = the document request's TTFB (not subresources).
            # ``total_bytes_downloaded`` = page weight = sum of
            # Content-Length over ALL response events (main doc +
            # subresources: images, scripts, CSS, ...). The response
            # body for the navigation itself is ``len(html)``.
            ttfb_ms: float | None = None
            total_bytes_downloaded: int | None = None
            if net_events:
                for evt in net_events:
                    if (
                        evt.event_type == "response"
                        and evt.resource_type == "document"
                        and evt.ttfb_ms is not None
                    ):
                        ttfb_ms = evt.ttfb_ms
                        break
                total = sum(
                    evt.body_size or 0
                    for evt in net_events
                    if evt.event_type == "response" and evt.body_size
                )
                if total > 0:
                    total_bytes_downloaded = total

            return FetchResult(
                url=url,
                final_url=final_url,
                status_code=status_code,
                status=FetchStatus.SUCCESS,
                html=html,
                debug_artifacts=debug_artifacts,
                network_events=net_events,
                api_candidates=api_cands,
                download_candidates_runtime=dl_intents,
                ttfb_ms=ttfb_ms,
                dom_parse_ms=dom_parse_ms,
                total_bytes_downloaded=total_bytes_downloaded,
                html_capture_source=html_capture_source,
                challenge=challenge_note,
            )
        except Exception as exc:
            # Capture debug artifacts on any failure path before re-raising
            if self._debug.enabled:
                artifacts = await self._debug.capture_page(page, exc, "fetch", context={"url": url})
                debug_artifacts.extend(artifacts)
            raise

    # ------------------------------------------------------------------
    # v1.7.0: bot-challenge detection + bounded auto-settle
    # ------------------------------------------------------------------

    def _recheck_url_after_settle(
        self, page: Page, url: str, status_code: Optional[int]
    ) -> str:
        """Re-validate ``page.url`` after a challenge cleared.

        Passing a challenge usually re-navigates the page; the redirect
        safety gate earlier in ``_navigate_and_extract`` only saw the
        interstitial URL, so re-check the post-settle URL against the
        domain policy (defense-in-depth, mirrors the existing redirect
        gate -- including its retryable :class:`NavigationError`).
        """
        final_url: str = page.url
        if final_url != url and not check_domain_allowed(final_url, self._config.safety):
            from .exceptions import NavigationError

            raise NavigationError(
                f"Page redirected to disallowed URL after challenge settle: {final_url}",
                url=final_url,
                status_code=status_code,
            )
        return final_url

    async def _settle_challenge(
        self,
        page: Page,
        url: str,
        info: ChallengeInfo,
        html: str,
        capture_source: HtmlCaptureSource,
    ) -> tuple[Optional[ChallengeInfo], str, HtmlCaptureSource]:
        """Bounded settle-and-recheck loop for an auto-settle-likely wall.

        Sleeps :attr:`FetchConfig.challenge_settle_ms` then re-captures the
        live page via :func:`safe_page_content` and re-runs detection, up
        to :attr:`FetchConfig.challenge_max_rechecks` rounds. Re-detection
        passes ``status_code=None`` on purpose: the original HTTP status
        belonged to the interstitial, and letting a stale 403 inflate the
        confidence of residual weak markers (e.g. a Turnstile widget on
        the real page) would mis-block a successful recovery.

        Returns:
            ``(None, html, source)`` once the challenge cleared (``html``
            is the post-settle capture), or
            ``(still_challenged_info, last_html, last_source)`` when the
            recheck budget is exhausted or the page became uncapturable
            mid-settle. Never raises.
        """
        cfg = self._config.fetch
        current = info
        for recheck in range(1, cfg.challenge_max_rechecks + 1):
            await asyncio.sleep(cfg.challenge_settle_ms / 1000)
            try:
                html, capture_source = await safe_page_content(page)
            except Exception as exc:
                logger.debug(
                    "Challenge settle recheck {n}/{total} could not capture "
                    "content for {url}: {e}",
                    n=recheck,
                    total=cfg.challenge_max_rechecks,
                    url=url,
                    e=exc,
                )
                return current, html, capture_source
            redetected = detect_challenge(html, None, None, page.url)
            if (
                redetected is None
                or redetected.confidence < CHALLENGE_CONFIDENCE_ACTION_THRESHOLD
            ):
                logger.info(
                    "Bot challenge ({vendor}/{kind}) auto-settled for {url} "
                    "after {n} recheck(s) (~{ms}ms waited)",
                    vendor=current.vendor,
                    kind=current.kind,
                    url=url,
                    n=recheck,
                    ms=recheck * cfg.challenge_settle_ms,
                )
                return None, html, capture_source
            current = redetected
        logger.debug(
            "Bot challenge for {url} did not settle after {n} recheck(s)",
            url=url,
            n=cfg.challenge_max_rechecks,
        )
        return current, html, capture_source

    async def _challenge_outcome_for_error_status(
        self,
        page: Page,
        url: str,
        response: Any,
        status_code: int,
    ) -> FetchResult | ChallengeInfo | None:
        """Sniff a 403/503 body for a bot wall before fast-failing.

        Pre-v1.7.0 these statuses fast-failed without ever looking at the
        rendered body -- even when it was a managed JS challenge that a
        real browser auto-passes in ~3-5s.

        Returns:
            - ``None``: no actionable challenge (capture failed, nothing
              detected, or confidence below the action threshold) -- the
              caller proceeds with the original fast-fail raise.
            - :class:`ChallengeInfo`: challenge detected AND it settled
              within the recheck budget -- the caller skips the error
              raises and proceeds to the normal success tail.
            - :class:`FetchResult`: challenge detected and still standing
              -- an honest BLOCKED result the caller returns as-is.
        """
        try:
            html, capture_source = await safe_page_content(page)
        except Exception as exc:
            logger.debug(
                "Challenge sniff could not capture HTTP {code} body for {url}: {e}",
                code=status_code,
                url=url,
                e=exc,
            )
            return None
        info = detect_challenge(html, status_code, _response_headers(response), page.url)
        if info is None or info.confidence < CHALLENGE_CONFIDENCE_ACTION_THRESHOLD:
            return None
        if info.auto_settle_likely and self._config.fetch.challenge_max_rechecks > 0:
            still, html, capture_source = await self._settle_challenge(
                page, url, info, html, capture_source
            )
            if still is None:
                return info
            info = still
        # A standing 403/503 wall: give a configured resolver hook a
        # bounded, re-verified attempt before fast-failing to BLOCKED.
        standing, html, capture_source = await self._attempt_captcha_resolution(
            page, url, info, html, capture_source
        )
        if standing is None:
            # Resolved -- return a stamped advisory ChallengeInfo so the
            # success tail records that a wall was here and the hook cleared
            # it (the caller proceeds to capture the now-real content).
            return self._mark_resolution(info, succeeded=True)
        return self._blocked_challenge_result(
            url=url,
            final_url=page.url or url,
            status_code=status_code,
            html=html,
            info=standing,
        )

    def _blocked_challenge_result(
        self,
        *,
        url: str,
        final_url: str,
        status_code: Optional[int],
        html: str,
        info: ChallengeInfo,
    ) -> FetchResult:
        """Build the honest BLOCKED result for a standing bot wall.

        The interstitial HTML rides along for diagnostics (it is NOT page
        content). ``error_message`` carries actionable guidance for the
        calling LLM. Callers RETURN this result -- never raise it into
        ``async_retry`` -- so a blocked fetch doesn't burn re-navigations
        into the same wall; and ``fetch()`` only caches SUCCESS, so a
        BLOCKED determination can never poison the cache.
        """
        guidance = (
            f"Blocked by {info.vendor} {info.kind} "
            f"(confidence {info.confidence:.2f}). Do not retry immediately. "
            "Options: (1) retry after 60s or more, (2) reuse an "
            "authenticated/named browser profile session that has passed "
            "this site's checks before, (3) escalate to a human via a "
            "headed login handoff, (4) try an alternative source for the "
            "same information."
        )
        logger.warning(
            "Bot challenge blocked fetch of {url}: vendor={vendor} kind={kind} "
            "confidence={conf} evidence={evidence}",
            url=url,
            vendor=info.vendor,
            kind=info.kind,
            conf=info.confidence,
            evidence=info.evidence,
        )
        return FetchResult(
            url=url,
            final_url=final_url or url,
            status_code=status_code,
            status=FetchStatus.BLOCKED,
            html=html or None,
            challenge=info,
            error_message=guidance,
        )

    # ------------------------------------------------------------------
    # v1.7.0 (Wave 7): pluggable CAPTCHA / bot-challenge resolver hook
    # ------------------------------------------------------------------

    @staticmethod
    def _mark_resolution(info: ChallengeInfo, *, succeeded: bool) -> ChallengeInfo:
        """Stamp the resolver-outcome flags onto a ChallengeInfo copy."""
        return info.model_copy(
            update={"resolution_attempted": True, "resolution_succeeded": succeeded}
        )

    async def _invoke_captcha_resolver(
        self, resolver: CaptchaResolver, ctx: CaptchaContext
    ) -> CaptchaResolution:
        """Call the hook (sync or async), bounding an async hook by timeout.

        A synchronous hook is not timed -- it blocks the event loop, so the
        contract is "keep it fast" (use ``async def`` for anything that
        waits). An async hook is wrapped in :func:`asyncio.wait_for` with
        ``FetchConfig.captcha_attempt_timeout_s`` (0 disables the bound).
        The return value is normalized to a :class:`CaptchaResolution`.
        Exceptions (incl. timeout) propagate to the caller, which isolates
        them.

        A sync hook that overruns the budget can't be interrupted (it has
        already blocked the loop by the time it returns), but we measure it
        and warn after the fact so the operator knows to switch to ``async``.
        """
        timeout = self._config.fetch.captcha_attempt_timeout_s
        started = time.monotonic()
        outcome = resolver(ctx)
        if inspect.isawaitable(outcome):
            if timeout and timeout > 0:
                outcome = await asyncio.wait_for(outcome, timeout=timeout)
            else:
                outcome = await outcome
        else:
            # Sync hook: it already ran to completion on this thread, blocking
            # the event loop. Can't bound it retroactively -- but if it
            # overran the budget, surface that so it can be made async.
            elapsed = time.monotonic() - started
            if timeout and timeout > 0 and elapsed > timeout:
                logger.warning(
                    "Synchronous CAPTCHA resolver blocked the event loop for "
                    "{e:.1f}s (over the {t:.1f}s budget). Make the hook "
                    "'async def' so it is bounded by captcha_attempt_timeout_s "
                    "and does not stall other concurrent work.",
                    e=elapsed,
                    t=timeout,
                )
        return normalize_resolution(outcome)

    async def _attempt_captcha_resolution(
        self,
        page: Page,
        url: str,
        info: ChallengeInfo,
        html: str,
        capture_source: HtmlCaptureSource,
    ) -> tuple[Optional[ChallengeInfo], str, HtmlCaptureSource]:
        """Give a configured resolver hook a bounded chance at a standing wall.

        Loops up to :attr:`FetchConfig.captcha_max_attempts`: invoke the
        hook, then -- crucially -- RE-RUN :func:`detect_challenge` against
        the freshly captured page. The hook's own ``resolved`` flag is
        advisory; only a clean (or sub-threshold) re-detection clears the
        wall. A hook that returns ``resolved=False`` concedes the wall and
        the loop stops early. Hook exceptions / timeouts / a failed
        re-capture are isolated -- they end the loop and leave the wall
        standing, never crashing the fetch.

        Returns:
            ``(None, html, source)`` when re-detection confirms the wall
            cleared (``html`` is the post-resolution capture), or
            ``(stamped_info, html, source)`` -- ``stamped_info`` carries
            ``resolution_attempted=True, resolution_succeeded=False`` -- when
            the wall still stands. A no-op (no resolver / disabled / zero
            budget) returns ``(info, html, source)`` unchanged and untouched.
        """
        cfg = self._config.fetch
        resolver = self._captcha_resolver
        if resolver is None or not cfg.captcha_resolution_enabled or cfg.captcha_max_attempts <= 0:
            return info, html, capture_source

        current = info
        for attempt in range(1, cfg.captcha_max_attempts + 1):
            ctx = CaptchaContext(
                page=page,
                challenge=current,
                url=url,
                final_url=page.url,
                attempt=attempt,
                max_attempts=cfg.captcha_max_attempts,
                correlation_id=get_correlation_id(),
            )
            self._metrics.incr("captcha_resolution_attempt", vendor=current.vendor)
            try:
                resolution = await self._invoke_captcha_resolver(resolver, ctx)
            except (asyncio.TimeoutError, TimeoutError):
                # asyncio.wait_for raises asyncio.TimeoutError; on Python 3.10
                # that is a DISTINCT class from the builtin TimeoutError, so we
                # catch both -- this also classifies a resolver that self-raises
                # a builtin TimeoutError as a timeout, not a generic error.
                logger.warning(
                    "CAPTCHA resolver timed out (>{s}s) for {url} on attempt {n}/{m}",
                    s=cfg.captcha_attempt_timeout_s,
                    url=url,
                    n=attempt,
                    m=cfg.captcha_max_attempts,
                )
                self._metrics.incr("captcha_resolution_outcome", result="timeout")
                break
            except Exception as exc:
                logger.warning(
                    "CAPTCHA resolver raised for {url} on attempt {n}/{m}: {e}",
                    url=url,
                    n=attempt,
                    m=cfg.captcha_max_attempts,
                    e=exc,
                )
                self._metrics.incr("captcha_resolution_outcome", result="error")
                break

            # Authoritative re-detection: the resolver's verdict is advisory.
            # Re-read the live page and re-run structural detection; only a
            # clean / sub-threshold result clears the wall.
            try:
                html, capture_source = await safe_page_content(page)
            except Exception as exc:
                logger.debug(
                    "Could not re-capture page after CAPTCHA resolver for {url}: {e}",
                    url=url,
                    e=exc,
                )
                self._metrics.incr("captcha_resolution_outcome", result="error")
                break
            redetected = detect_challenge(html, None, None, page.url)
            if redetected is None or redetected.confidence < CHALLENGE_CONFIDENCE_ACTION_THRESHOLD:
                suffix = "" if resolution.detail is None else f" ({resolution.detail})"
                logger.info(
                    "CAPTCHA resolver cleared {vendor}/{kind} wall for {url} "
                    "after {n} attempt(s){suffix}",
                    vendor=current.vendor,
                    kind=current.kind,
                    url=url,
                    n=attempt,
                    suffix=suffix,
                )
                self._metrics.incr("captcha_resolution_outcome", result="resolved")
                return None, html, capture_source
            current = redetected
            if not resolution.resolved:
                # The resolver conceded this wall -- stop rather than burn the
                # remaining attempt budget on something it has given up on.
                logger.debug(
                    "CAPTCHA resolver conceded {vendor}/{kind} for {url} on attempt {n}",
                    vendor=current.vendor,
                    kind=current.kind,
                    url=url,
                    n=attempt,
                )
                self._metrics.incr("captcha_resolution_outcome", result="failed")
                break
        else:
            # Loop exhausted: every attempt ran, the hook kept claiming
            # progress, but re-detection never cleared the wall.
            self._metrics.incr("captcha_resolution_outcome", result="failed")

        return self._mark_resolution(current, succeeded=False), html, capture_source

    async def fetch_many(
        self,
        urls: list[str],
        session_id: Optional[str] = None,
    ) -> list[FetchResult]:
        """Fetch multiple URLs concurrently, bounded by BrowserManager's semaphore.

        v1.6.14 C-4: when ``session_id`` is supplied, an
        :class:`asyncio.Semaphore` gates the concurrent ``self.fetch``
        calls to :attr:`BrowserConfig.max_pages_per_session_fetch`. The
        session path creates pages via ``ctx.new_page()`` directly
        (bypassing :class:`BrowserManager`'s context semaphore that
        otherwise caps concurrency on the ephemeral path), so without
        this gate 20+ concurrent fetches against one BrowserContext
        reproducibly crash Chromium's renderer. The ephemeral path (no
        ``session_id``) is intentionally left untouched -- it goes
        through ``BrowserManager.new_page`` which is already gated by
        ``max_contexts``.

        Args:
            urls: List of URLs to fetch.
            session_id: Optional shared persistent session for all fetches.

        Returns:
            List of FetchResult in the same order as input URLs. Exceptions
            raised by an individual ``self.fetch`` are isolated per-url and
            converted into an error FetchResult (a single failure never
            aborts the whole batch); ``asyncio.CancelledError`` still
            propagates.
        """
        if session_id is not None:
            # v1.6.14 C-4: session-path gate. The semaphore is created
            # per-call (not stored on the WebFetcher) because a
            # different session_id in a subsequent call shouldn't share
            # the budget -- this is "concurrency inside one fetch_many",
            # not "concurrency across the WebFetcher's lifetime".
            sem = asyncio.Semaphore(self._config.browser.max_pages_per_session_fetch)

            async def _gated(u: str) -> FetchResult:
                async with sem:
                    return await self.fetch(u, session_id=session_id)

            tasks = [_gated(url) for url in urls]
        else:
            tasks = [self.fetch(url, session_id=session_id) for url in urls]
        # H6: isolate per-url failures. A bare ``gather`` (no
        # ``return_exceptions=True``) aborts the entire batch -- and
        # orphans every sibling's in-flight page -- the moment ANY
        # ``self.fetch`` raises (exhausted retries re-raise, an
        # unexpected Playwright error, or cancellation). Mirror
        # ``Recipes.web_research``: collect results with
        # ``return_exceptions=True``, then zip back over ``urls`` in order
        # (tasks are created from ``urls`` in order in BOTH branches, so
        # index alignment holds). Re-raise CancelledError so cancellation
        # propagates; convert any other exception into an error FetchResult
        # for that url.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[FetchResult] = []
        for url, result in zip(urls, results, strict=True):
            if isinstance(result, FetchResult):
                out.append(result)
                continue
            if isinstance(result, asyncio.CancelledError):
                # Never swallow cancellation -- propagate exactly like
                # web_research does for its gather results.
                raise result
            # Any other exception: degrade this single url to an error
            # result so its siblings survive.
            logger.warning(
                "fetch_many: fetch raised for {url}: {exc}",
                url=url,
                exc=result,
            )
            out.append(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.NETWORK_ERROR,
                    error_message=str(result),
                    correlation_id=get_correlation_id(),
                )
            )
        return out

    async def classify_url(
        self,
        url: str,
        *,
        session_id: Optional[str] = None,
    ) -> str:
        """Return a granular classification kind for a URL.

        v1.6.10: returns one of ``'pdf' | 'xlsx' | 'docx' | 'csv' | 'zip'
        | 'binary_other' | 'html' | 'unknown'``. Callers route via
        :func:`is_binary_kind` instead of comparing to a single
        ``"binary"`` string (the v1.6.9 return type). Used by
        :meth:`fetch_smart` and :meth:`Recipes.find_and_download_file`
        for binary-vs-HTML and file-type filtering.

        Resolution order:
          1. URL extension matches a known download/HTML pattern -> direct answer
          2. Content-Type/Content-Disposition probe via HEAD (skipped when
             :attr:`SafetyConfig.probe_binary_urls` is False)
          3. ``'unknown'`` otherwise (caller should default to HTML).

        Args:
            url: The URL to classify.
            session_id: Optional persistent session whose cookies should
                be applied to the HEAD probe. Required when probing
                authenticated extensionless document URLs (regulator
                dashboards, intranet downloads).
        """
        # Pre-gate: do not probe denied / private-IP URLs even with HEAD.
        # The fetch path already gates the URL; the classifier must too --
        # otherwise a HEAD probe leaks a request to a host the policy
        # explicitly forbids (SSRF defense-in-depth, mirrors fetch /
        # fetch_binary).
        if not check_domain_allowed(url, self._config.safety):
            logger.debug(
                "classify_url skipping HEAD probe for disallowed URL: {url}",
                url=url,
            )
            return "unknown"

        ext_class = _url_ext_classification(url)
        if ext_class != "unknown":
            return ext_class
        if not self._config.safety.probe_binary_urls:
            return "unknown"

        # HEAD probe with redirects + UA + cookies (when session present).
        # We deliberately swallow all errors -- a failing HEAD must NEVER
        # block the subsequent fetch, only inform routing.
        #
        # Defense-in-depth: re-validate the FINAL redirected URL against
        # the safety policy. follow_redirects=True can land us on a
        # disallowed host that would never have passed the entry gate;
        # treating that as 'unknown' avoids using the probe to leak that
        # the redirect target exists, and keeps SSRF mitigations honest.
        #
        # v1.6.16 FC-1: also validate EACH redirect hop's Location and the
        # actual connected peer IP, mirroring ``fetch_binary`` /
        # ``downloader._download_httpx``. Checking only the final URL let a
        # whitelisted host 302 through an internal hop, and a rebinding host
        # connect to a private peer, before the benign final URL was seen.
        from .exceptions import DomainNotAllowedError, NavigationError

        safety = self._config.safety

        async def _check_redirect(response: httpx.Response) -> None:
            if 300 <= response.status_code < 400:
                location = response.headers.get("location", "")
                # v1.6.16 FC-1 fix: resolve a possibly-RELATIVE ``Location``
                # against the responding URL before gating (see fetch_binary's
                # twin) so a same-host relative redirect isn't mis-blocked and
                # then swallowed to 'unknown', mis-routing the URL.
                if location:
                    next_url = urljoin(str(response.url), location)
                    if not check_domain_allowed(next_url, safety):
                        raise NavigationError(
                            f"HEAD probe redirect to disallowed URL blocked: {next_url}",
                            url=next_url,
                            status_code=response.status_code,
                        )

        try:
            cookie_jar = await self._cookies_for_session(session_id, url)
            # v1.7.0 (Wave 2F): coherent browser-consistent headers (UA OS
            # matches the browser locale) + outbound proxy when configured.
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=10.0,
                headers=self._coherent_side_path_headers(),
                cookies=cookie_jar,
                event_hooks={"response": [_check_redirect]},
                **self._httpx_proxy_kwargs(),
            ) as client:
                # v1.6.16 FC-1 fix: use a STREAMING HEAD so the post-connect
                # peer-IP re-check actually fires. ``httpx_peer_ip`` reads the
                # httpcore ``network_stream`` extension, which is ONLY populated
                # for a streaming response -- a buffered ``client.head()`` left
                # it empty, making the DNS-rebinding guard below a silent no-op.
                # A streaming HEAD pulls no body, so the probe stays as light as
                # before while restoring the peer check.
                async with client.stream("HEAD", url) as resp:
                    # post-connect peer-IP re-check (DNS rebinding).
                    if getattr(safety, "block_private_ips", False):
                        peer_ip = httpx_peer_ip(resp)
                        if peer_ip and is_private_address(peer_ip):
                            logger.debug(
                                "HEAD probe connected to private peer {ip} for {url}; "
                                "treating as 'unknown'",
                                ip=peer_ip,
                                url=url,
                            )
                            return "unknown"
                    final_url = str(resp.url)
                    if final_url != url and not check_domain_allowed(
                        final_url, self._config.safety
                    ):
                        logger.debug(
                            "HEAD probe followed redirect to disallowed URL {final}; "
                            "treating as 'unknown'",
                            final=final_url,
                        )
                        return "unknown"
                    ct = resp.headers.get("content-type")
                    disp = resp.headers.get("content-disposition")
                    # v1.6.10: map Content-Type to a granular kind first so
                    # callers can filter by file_types. Anything binary that
                    # doesn't match a known mapping collapses to
                    # 'binary_other' (still routed through fetch_binary).
                    ct_norm = (ct or "").lower().split(";", 1)[0].strip()
                    for prefix, kind in _CT_TO_KIND.items():
                        if ct_norm.startswith(prefix):
                            return kind
                    if _content_type_is_binary(ct) or _disposition_is_attachment(disp):
                        return "binary_other"
                    if ct_norm.startswith(("text/html", "application/xhtml")):
                        return "html"
                    return "unknown"
        except (NavigationError, DomainNotAllowedError) as exc:
            # L1: the redirect hook (_check_redirect) blocked a redirect to
            # a denied / private host -- a probable SSRF attempt. The safe
            # behavior (return 'unknown' so a failing HEAD never blocks the
            # subsequent fetch) is preserved, but log at WARNING rather than
            # DEBUG so the blocked-redirect signal is observable instead of
            # being swallowed alongside ordinary network errors.
            logger.warning(
                "HEAD probe redirect blocked for {url} (SSRF defense): {e}",
                url=url,
                e=exc,
            )
            return "unknown"
        except Exception as exc:
            logger.debug("HEAD probe failed for {url}: {e}", url=url, e=exc)
            return "unknown"

    async def _cookies_for_session(
        self, session_id: Optional[str], target_url: str
    ) -> httpx.Cookies:
        """Extract Playwright BrowserContext cookies into a host-scoped jar.

        When a caller threads ``session_id`` through, we want authenticated
        downloads (regulator dashboards, intranet docs) to inherit login
        state established earlier via ``Agent.interact``. **Critical**: we
        only forward cookies whose Playwright domain matches the target
        URL's host (exact or parent suffix). A flat ``{name: value}`` dict
        would leak EVERY session cookie to EVERY host the agent fetches --
        including attacker.com getting bank.com auth cookies.

        Each retained cookie is set on the returned :class:`httpx.Cookies`
        with its declared domain so httpx applies its own cookie-domain
        rules during request construction (defense in depth).

        Returns an empty jar when the session is None / unknown / cookie
        retrieval fails -- never raises.

        Args:
            session_id: Persistent session id, or None for ephemeral.
            target_url: The URL we're about to request. Cookies whose
                domain doesn't match this host are dropped.
        """
        jar = httpx.Cookies()
        if not session_id or self._sessions is None:
            return jar
        try:
            ctx = self._sessions.get(session_id)
        except Exception:
            return jar
        try:
            cookies = await ctx.cookies()
        except Exception as exc:
            logger.debug("Failed to read session cookies: {e}", e=exc)
            return jar

        parsed_target = urlparse(target_url)
        target_host = (parsed_target.hostname or "").lower()
        if not target_host:
            return jar
        # v1.6.16 FC-2: a Secure-flagged cookie must never be forwarded over a
        # plaintext http:// request -- that is exactly the cleartext leak the
        # Secure attribute exists to prevent (browsers send Secure cookies on
        # https only). An http download from a session holding Secure auth
        # cookies would otherwise put them on the wire in the clear.
        target_is_https = (parsed_target.scheme or "").lower() == "https"

        for c in cookies:
            if "name" not in c or "value" not in c:
                continue
            if c.get("secure") and not target_is_https:
                continue
            domain = (c.get("domain") or "").lstrip(".").lower()
            # Cookies without a declared domain only apply to the target
            # host they were set on; we treat that as a per-host cookie
            # and pin it to the target host.
            if not domain:
                jar.set(c["name"], c["value"], domain=target_host)
                continue
            # Send only when target host equals or is a subdomain of the
            # cookie's declared domain.
            if target_host == domain or target_host.endswith("." + domain):
                jar.set(c["name"], c["value"], domain=domain)
        return jar

    async def fetch_binary(
        self,
        url: str,
        session_id: Optional[str] = None,
    ) -> FetchResult:
        """Fetch a binary resource (PDF/XLSX/CSV/DOCX/etc.) into memory via httpx.

        Used by :meth:`Agent.search_and_extract` with ``extract_files=True``
        and by :meth:`Agent.fetch_and_extract` when the URL points to a
        downloadable file. Skips the browser entirely (httpx is faster and
        binary files don't need rendering). Honors the same domain /
        robots / rate-limit / size-cap / cookie gates as
        :meth:`fetch`.

        Streams the response in chunks and aborts when the accumulated
        size exceeds ``DownloadConfig.max_file_size_mb`` -- prevents a
        rogue large file from exhausting memory.

        When ``session_id`` is supplied and the SessionManager has that
        session, cookies are copied from the Playwright context into the
        httpx request so authenticated downloads succeed.

        Args:
            url: Binary resource URL.
            session_id: Optional persistent session whose cookies should
                be applied to the httpx request.

        Returns:
            FetchResult with ``binary`` populated on success and
            ``content_type`` set from the response header. ``html`` is
            always None for binary fetches. Returns HTTP_ERROR with a
            clear message when the size cap is exceeded.
        """
        cid = get_correlation_id()

        if not check_domain_allowed(url, self._config.safety):
            host = urlparse(url).hostname or ""
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.BLOCKED,
                    error_message=f"Domain not allowed by SafetyConfig: {host}",
                    correlation_id=cid,
                )
            )

        if self._robots is not None and not await self._robots.is_allowed(url):
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.BLOCKED,
                    error_message="robots.txt disallows this URL for binary fetch",
                    correlation_id=cid,
                )
            )

        if self._rate_limiter is not None:
            await self._rate_limiter.acquire(urlparse(url).hostname or "")

        from .exceptions import NavigationError

        max_bytes = max(1, self._config.download.max_file_size_mb) * 1024 * 1024
        cookie_jar = await self._cookies_for_session(session_id, url)
        safety = self._config.safety

        # v1.6.16 FB-1: per-redirect Location validation. ``fetch_binary``
        # follows redirects, so a whitelisted host could 302 through an
        # internal hop (169.254.169.254 / RFC1918 / loopback) before the
        # benign final URL is reached. Re-validate EACH 3xx Location against
        # the safety policy -- mirrors ``downloader._download_httpx``'s
        # ``_check_redirect`` hook (C-1c). The raised ``NavigationError`` is
        # caught below and surfaced as ``FetchStatus.BLOCKED``.
        async def _check_redirect(response: httpx.Response) -> None:
            if 300 <= response.status_code < 400:
                location = response.headers.get("location", "")
                # v1.6.16 FB-1 fix: RFC 9110 allows a RELATIVE ``Location``
                # (e.g. ``/download/file.pdf``), which is extremely common for
                # trailing-slash / auth redirects. The raw header has no host,
                # so gating it directly made ``check_domain_allowed`` reject a
                # legitimate same-host relative redirect ("no host") and turn a
                # normal binary fetch into a spurious BLOCKED. Resolve against
                # the responding URL first; ``urljoin`` leaves an ABSOLUTE
                # Location unchanged, so a cross-host redirect is still gated on
                # its real host.
                if location:
                    next_url = urljoin(str(response.url), location)
                    if not check_domain_allowed(next_url, safety):
                        raise NavigationError(
                            f"Redirect to disallowed URL blocked: {next_url}",
                            url=next_url,
                            status_code=response.status_code,
                        )

        timer = Timer()
        try:
            with timer:
                # v1.6.14 C-6: derive the binary-fetch timeout from
                # navigation_timeout (ms -> s) but BOUND it. navigation_timeout
                # is a page-load knob; reusing it unbounded means a large value
                # (e.g. 300000ms) becomes a 300s httpx timeout, compounding
                # across retries into minutes of blocking. Cap at 120s -- a
                # generous per-operation (connect/read) bound for a streamed
                # binary, safe against a runaway config value.
                binary_timeout_s = min(
                    max(self._config.browser.navigation_timeout / 1000.0, 1.0), 120.0
                )
                # v1.7.0 (Wave 2F): coherent browser-consistent headers (UA
                # OS matches the browser locale + Accept-Language) and the
                # outbound proxy when configured, so the binary side-path
                # presents the same identity as the browser path instead of
                # httpx's default Python TLS/UA fingerprint.
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=binary_timeout_s,
                    headers=self._coherent_side_path_headers(),
                    cookies=cookie_jar,
                    event_hooks={"response": [_check_redirect]},
                    **self._httpx_proxy_kwargs(),
                ) as client:
                    async with client.stream("GET", url) as resp:
                        # v1.6.16 FB-1: post-connect peer-IP re-check (DNS
                        # rebinding). ``check_domain_allowed`` validated the
                        # host against cached DNS, but httpx re-resolves at
                        # connect time, so a rebind to an internal address
                        # could otherwise slip through. Inspect the actual
                        # connected peer and refuse if it is private. Mirrors
                        # the Playwright ``server_addr`` guard (C-1b) in
                        # ``fetch`` and the httpx guard (C-1c) in the downloader.
                        if getattr(safety, "block_private_ips", False):
                            peer_ip = httpx_peer_ip(resp)
                            if peer_ip and is_private_address(peer_ip):
                                return self._record_fetch_outcome(
                                    FetchResult(
                                        url=url,
                                        final_url=str(resp.url),
                                        status=FetchStatus.BLOCKED,
                                        error_message=(
                                            f"Binary fetch connected to a private/loopback/"
                                            f"link-local peer ({peer_ip}) for {url} "
                                            f"(post-connect DNS-rebinding guard)"
                                        ),
                                        response_time_ms=timer.elapsed_ms,
                                        correlation_id=cid,
                                    )
                                )
                        final_url = str(resp.url)
                        if final_url != url and not check_domain_allowed(
                            final_url, self._config.safety
                        ):
                            return self._record_fetch_outcome(
                                FetchResult(
                                    url=url,
                                    final_url=final_url,
                                    status=FetchStatus.BLOCKED,
                                    error_message=f"Redirected to disallowed URL: {final_url}",
                                    response_time_ms=timer.elapsed_ms,
                                    correlation_id=cid,
                                )
                            )
                        if resp.status_code == 429:
                            # v1.6.12: signal the rate limiter so the
                            # next ``fetch_binary`` call on this host
                            # waits Retry-After. ``fetch_binary`` is
                            # NOT retry-wrapped (unlike the HTML path),
                            # so we return HTTP_ERROR rather than raise
                            # -- the caller decides whether to retry.
                            host = urlparse(url).hostname or ""
                            retry_after = self._signal_429(resp, url, host)
                            return self._record_fetch_outcome(
                                FetchResult(
                                    url=url,
                                    final_url=final_url,
                                    status_code=resp.status_code,
                                    status=FetchStatus.HTTP_ERROR,
                                    error_message=(
                                        "HTTP 429 Too Many Requests"
                                        + (
                                            f" (Retry-After: {retry_after}s)"
                                            if retry_after is not None
                                            else ""
                                        )
                                    ),
                                    response_time_ms=timer.elapsed_ms,
                                    correlation_id=cid,
                                )
                            )
                        if resp.status_code >= 400:
                            return self._record_fetch_outcome(
                                FetchResult(
                                    url=url,
                                    final_url=final_url,
                                    status_code=resp.status_code,
                                    status=FetchStatus.HTTP_ERROR,
                                    error_message=f"HTTP {resp.status_code}",
                                    response_time_ms=timer.elapsed_ms,
                                    correlation_id=cid,
                                )
                            )
                        # Stream chunks with running cap. We can't trust
                        # Content-Length (some servers omit / lie); enforce
                        # the cap by accumulator instead.
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes(chunk_size=8192):
                            total += len(chunk)
                            if total > max_bytes:
                                return self._record_fetch_outcome(
                                    FetchResult(
                                        url=url,
                                        final_url=final_url,
                                        status_code=resp.status_code,
                                        status=FetchStatus.HTTP_ERROR,
                                        error_message=(
                                            f"Binary exceeded "
                                            f"{self._config.download.max_file_size_mb} MB cap "
                                            f"(stopped at {total} bytes)"
                                        ),
                                        response_time_ms=timer.elapsed_ms,
                                        correlation_id=cid,
                                    )
                                )
                            chunks.append(chunk)
                        # v1.7.0 (Wave 4A): record the bytes downloaded on the
                        # binary success path (the HTML path observes via
                        # _observe_fetch_payload; binary has the exact size).
                        self._metrics.observe("bytes_downloaded", float(total))
                        return self._record_fetch_outcome(
                            FetchResult(
                                url=url,
                                final_url=final_url,
                                status_code=resp.status_code,
                                status=FetchStatus.SUCCESS,
                                binary=b"".join(chunks),
                                content_type=resp.headers.get("content-type"),
                                response_time_ms=timer.elapsed_ms,
                                correlation_id=cid,
                            )
                        )
        except NavigationError as exc:
            # v1.6.16 FB-1: a redirect to a disallowed/private host raised
            # by the ``_check_redirect`` event hook above is a security stop,
            # not a transport failure -- surface it as BLOCKED (matching the
            # final-URL / peer-IP guards) rather than NETWORK_ERROR.
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=getattr(exc, "url", "") or url,
                    status=FetchStatus.BLOCKED,
                    error_message=str(exc),
                    response_time_ms=timer.elapsed_ms,
                    correlation_id=cid,
                )
            )
        except httpx.TimeoutException:
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.TIMEOUT,
                    error_message="Binary fetch timed out",
                    response_time_ms=timer.elapsed_ms,
                    correlation_id=cid,
                )
            )
        except Exception as exc:
            return self._record_fetch_outcome(
                FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.NETWORK_ERROR,
                    error_message=str(exc),
                    response_time_ms=timer.elapsed_ms,
                    correlation_id=cid,
                )
            )


class _DownloadStartedError(Exception):
    """Internal: URL triggered a file download instead of page navigation."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"Download started for {url}")
