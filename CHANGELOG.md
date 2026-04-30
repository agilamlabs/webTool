# Changelog

## [1.5.0] - 2026-04-30

### New: disk-backed TTL cache + markdown extraction

Two of the deferred items from 1.4 land here. Both are opt-in (cache
is disabled by default) or no-cost (markdown is populated for free
on every successful trafilatura extraction).

#### Cache layer

- ``web_agent/cache.py``: ``Cache`` ABC + ``DiskCache`` concrete
  implementation. JSON-on-disk, one file per entry keyed by SHA256
  of the input. Per-entry TTL with stale-on-read deletion. Soft
  ``max_cache_mb`` budget enforced via LRU-by-mtime eviction on writes.
- ``CacheConfig``: ``enabled`` (False), ``cache_dir`` (./cache),
  ``ttl_seconds`` (3600), ``max_cache_mb`` (100). Path resolved
  against ``base_dir`` like other directory configs.
- Wired into ``WebFetcher.fetch`` (cache hit skips rate-limit +
  network; robots.txt is still checked first so a host's robots.txt
  changes immediately take effect even on cached URLs) and
  ``SearchEngine.search`` (cache hit short-circuits the full provider
  chain). Both subsystems take a ``cache: Cache | None`` kwarg.
- Only **successful** fetches and **non-empty** search responses are
  cached. Errors / empty results would lock in transient failures
  across the TTL window.

#### Markdown extraction

- ``ExtractionResult.markdown: str | None`` -- new field. Populated
  whenever ``trafilatura`` is the winning extractor.
- ``ContentExtractor._extract_trafilatura`` calls
  ``trafilatura.extract(html, output_format="markdown")`` as a second
  pass after the metadata-rich ``bare_extraction``. Cheap (HTML re-
  parsed once) and the result is what most LLMs prefer to consume --
  preserves headings, lists, links, emphasis without HTML noise.
- ``markdown`` stays ``None`` when bs4 or raw-text fallbacks win
  (those layers have no markdown rendering equivalent).

### Models

- ``FetchResult.from_cache: bool`` -- True when served from cache.
- ``SearchResponse.from_cache: bool`` -- same.
- ``ExtractionResult.markdown: str | None`` -- markdown rendering.

### Tests

- ``tests/test_cache.py`` (new): 18 tests covering DiskCache
  (roundtrip, TTL, expiry-on-access, eviction, corrupt-file, lazy-
  dir-creation) + 4 Agent-integration tests (cache wiring, end-to-end
  search caching).
- ``tests/test_content_extractor.py`` extended with 2 markdown tests.

Test count: 173 -> 192 unit (+19). 192 -> 213 total. All green.

## [1.4.0] - 2026-04-30

### Search pipeline rebuilt: SearXNG -> DDGS -> Playwright

The hardcoded "Google then DuckDuckGo via Playwright" path is replaced
with a configurable provider chain. The old behavior is still available
as the third tier; the new default makes search **3-5x faster** by
skipping browser launch when an API-based provider can answer.

**New flow** for ``Agent.search_and_extract(query)``:

1. **URL short-circuit** -- if ``query`` is itself a single ``http(s)://``
   URL, skip search entirely and fetch + extract the URL directly.
2. **SearXNG** -- query a self-hosted SearXNG JSON API. Privacy-
   respecting metasearch aggregator. Skipped silently when
   ``searxng_base_url`` is not set.
3. **DDGS** -- DuckDuckGo via the ``ddgs`` Python package (no browser).
   Skipped silently if the optional dep is missing.
4. **Playwright** -- browser-driven Google + DDG-HTML scraping (the
   pre-1.4 behavior, still here as the safety-net fallback).
5. Extract page content (existing trafilatura -> bs4 -> raw chain).

### New modules

- ``web_agent/search_providers.py``:
  - ``SearchProvider`` (ABC)
  - ``SearXNGProvider`` -- httpx + SearXNG JSON API
  - ``DDGSProvider`` -- ``ddgs`` package (lazy-imported, optional)
  - ``PlaywrightProvider`` -- absorbed the old Google + DDG-HTML
    parsing logic from ``search_engine.py``

### Refactor

- ``SearchEngine`` is now a ~140-line chain orchestrator over
  ``SearchProvider`` instances. Builds the catalog from
  ``config.search.providers`` (default
  ``["searxng", "ddgs", "playwright"]``) and walks the chain until
  one returns results. ``strict=True`` raises ``SearchError`` if all
  providers exhaust.
- The 200+ lines of Google SERP parsing + DDG-HTML parsing moved out
  of ``search_engine.py`` and into ``PlaywrightProvider``. No public
  API change.

### Config additions

- ``SearchConfig.providers: list[str] = ["searxng", "ddgs", "playwright"]``
- ``SearchConfig.searxng_base_url: str | None = None``
- ``SearchConfig.searxng_timeout: float = 10.0``

### Dependencies

- New runtime dep: ``ddgs >= 9.0.0`` (formerly ``duckduckgo-search``).

### Tests

- ``tests/test_search_providers.py`` (new): 23 tests covering each
  provider in isolation (mocked HTTP / mocked ``ddgs``), the chain
  orchestrator (mocked providers via ``_RecordingProvider``), and
  URL-as-query detection.
- Test count: 171 -> 196 unit tests (192 -> 217 total with
  integration). All green.

### Performance

- Live integration suite (``tests/test_agent.py``) runs in **~36s**
  vs. ~106s pre-1.4 because DDGS resolves search results in
  sub-second without a browser launch.

## [1.3.0] - 2026-04-30

### New: politeness layer + audit log

- **`RateLimiter`** (`web_agent.rate_limiter`): per-host async rate gate
  using minimum-interval scheduling. Different hosts proceed
  concurrently; same-host requests are serialized to
  `safety.rate_limit_per_host_rps` (default **2.0** rps). Set to `0`
  to disable.
- **`RobotsChecker`** (`web_agent.robots`): fetches and obeys each
  host's `robots.txt` before requesting pages. Uses stdlib
  `urllib.robotparser` and an httpx fetcher with a 5-second timeout.
  Per-host TTL cache (default 1 hour). Missing or unreachable
  `robots.txt` is treated as allow-all. Gated by
  `safety.respect_robots_txt` (default **True**).
- **`AuditLogger`** (`web_agent.audit`): append-only JSONL log of every
  public Agent operation. Each entry: `timestamp`, `correlation_id`,
  `method`, `args`, `status` (`success` / `error`), `elapsed_ms`,
  optional `error` repr. Disabled by default; enable via
  `audit.enabled = True` and `audit.audit_log_path = "..."`.

### Wiring

- `WebFetcher.__init__` and `Downloader.__init__` accept new optional
  `rate_limiter` and `robots` kwargs. When set, both gates run before
  any network I/O. `SearchEngine.__init__` accepts `rate_limiter`
  (per-host limit applies to `www.google.com` and `html.duckduckgo.com`).
- `Agent` instantiates a single `RateLimiter`, `RobotsChecker`, and
  `AuditLogger` from `SafetyConfig` / `AuditConfig` and passes them
  into the subsystems. Each public Agent method now runs inside
  `_call_scope(...)` which composes `correlation_scope` + audit log
  into one async-context-manager.

### Config additions

- `SafetyConfig` gains: `rate_limit_per_host_rps: float = 2.0`,
  `respect_robots_txt: bool = True`, `robots_user_agent: str = "web-agent-toolkit"`.
- New `AuditConfig` (top-level `audit:` field on `AppConfig`):
  `enabled: bool = False`, `audit_log_path: str = "./audit.jsonl"`.
- `AppConfig._resolve_paths` now resolves `audit.audit_log_path`
  against `base_dir`.

### Tests

Test count: **150 → 171** unit tests (21 new across `test_politeness.py`
and `test_audit.py`). Integration test count unchanged at 21. Total
**192** passing.

### Deferred to a follow-up

- **Cache layer** (HTTP fetch / search result cache): scoped out of
  this release. Would need: cache key generation, FetchResult
  serialization, TTL + LRU, `from_cache: bool` on result models,
  integration in 3 places.
- **Pluggable search providers**: scoped out. Current `SearchEngine`
  is hardcoded for Google + DuckDuckGo. Refactoring to a
  `SearchProvider` ABC + registry would be a separate architectural
  change.

## [1.2.0] - 2026-04-30

### Security (Critical fixes from full-project code review)

- **Path traversal protection**: `Downloader.download(filename=...)` and
  `ScreenshotInput.path` now reject `..` traversal and absolute paths via
  the new `safe_join_path` helper.
- **SSRF protection**:
  - Added `SafetyConfig.block_private_ips` (default **True**) which blocks
    RFC1918, loopback, link-local (incl. AWS IMDS at 169.254.169.254), and
    unspecified IPs.
  - `Downloader._download_httpx` now installs an httpx event hook that
    re-validates every redirect target against the safety config -- a
    whitelisted host can no longer redirect to a private IP / denied domain.
  - `WebFetcher` re-checks `page.url` after Playwright navigation for the
    same defense-in-depth purpose.

### Breaking Changes

- **`SafetyConfig.allow_js_evaluation` defaults to `False`** (was implicitly
  True). MCP/CLI callers that rely on `EvaluateInput` actions must opt in
  by setting `safety.allow_js_evaluation: true` (or run with `safe_mode=False`
  AND no longer rely on the old implicit allow).
- The single `safe_mode` flag has been split into 4 granular flags:
  - `allow_js_evaluation` (default False) -- gates `EvaluateInput`
  - `allow_downloads` (default True) -- gates file downloads
  - `allow_form_submit` (default True) -- gates submit-button clicks
  - `block_private_ips` (default True) -- SSRF protection
- `safe_mode` remains as a master kill-switch: when True it forces all
  three `allow_*` flags to False (regardless of explicit settings).

### Correctness Bugs Fixed

- **UnboundLocalError in `BrowserActions.execute_sequence`**: when
  `ctx.new_page()` failed on the session path, the original exception was
  shadowed by an `UnboundLocalError`. Fixed by initializing cleanup state
  before the if/else branch.
- **`debug_artifacts` lost across retries**: `WebFetcher._do_fetch` declared
  the artifacts list inside the retry-decorated function, so files written
  to disk on a transient failure became unreachable from the eventual
  `FetchResult`. The list is now hoisted to the outer scope and accumulates
  across all retry attempts.
- **`BrowserManager.start()` race condition**: two concurrent calls could
  both launch Chromium and leak the first browser. Fixed with an
  `asyncio.Lock` covering both `start()` and `stop()`.

### Exception Hierarchy

The 10 typed exception classes that were exported but never raised are now
wired up at the obvious internal sites:

- `BrowserError` -- raised by `BrowserManager.start()` on Playwright launch
  failure.
- `SearchError` -- raised by `SearchEngine.search(strict=True)` when both
  Google and DuckDuckGo return zero results.
- `DomainNotAllowedError` -- raised by `check_domain_allowed(strict=True)`.
- `ConfigError` -- raised by `AppConfig.from_yaml()` on YAML parse or
  validation failure.
- `SelectorNotFoundError` -- raised by `_resolve_locator` when the
  `LocatorSpec` is empty.
- `ActionError` -- raised by `_do_select` when no value/label/index is set.
- `NavigationError` -- raised by `Downloader._download_httpx` on disallowed
  redirect targets, and by `WebFetcher` when post-redirect URL is blocked.
- `ExtractionError` -- raised by `ContentExtractor.extract(strict=True)`
  when all three extraction layers fail.

### New `strict=False` parameter

All public `Agent` methods now accept `strict: bool = False`:
- `Agent.search_and_extract(..., strict=True)` -- raises `SearchError`
- `Agent.fetch_and_extract(..., strict=True)` -- raises `NavigationError`
- `Agent.download(..., strict=True)` -- raises `DownloadError`

Default `strict=False` preserves the existing result-based API.

### Reliability

- `_download_httpx` cleans up partial files (try/finally + unlink) so a
  size-limit-exceeded download doesn't leave corrupted bytes that the
  Playwright fallback overwrites with HTML.
- `WebFetcher` networkidle-to-load fallback now persists across retry
  attempts. Previously, every retry would burn 45s on networkidle before
  falling through; now once the first attempt fails, subsequent retries
  use `load` directly.
- `SearchEngine` rejects DDG result URLs with non-http(s) schemes
  (`javascript:`, `data:`).
- `_looks_like_submit` heuristic now checks `text` / `label` / `placeholder`
  fields of `LocatorSpec`, not just `role_name` and `selector`. Closes the
  safe_mode bypass via `LocatorSpec(text="Sign in")`.

### Polish

- `Recipes.web_research` caches `_rank` scores in a dict before sorting,
  avoiding redundant tokenization per item.
- `SessionManager.create()` now holds the lock across the entire creation
  (context build + UA probe + dict registration) for correctness.
- `SessionManager.close_all()` catches `KeyError` separately and logs at
  DEBUG, so already-closed sessions don't produce misleading "Error
  closing session" warnings during normal teardown.

### Tests

Test count rises from 111 to ~145. New test files:
- `tests/test_security.py` (path traversal, private IP detection)
- `tests/test_exceptions.py` (typed exceptions raised at expected sites)
- `tests/test_safety.py` extended with granular-flag tests + extended
  `_looks_like_submit` checks.

## [1.1.0] - 2026-04-29

Initial high-ROI improvements: correlation IDs, retry policies, safety
controls (basic), debug mode, semantic locators, browser sessions,
high-level recipes (search_and_open_best_result, find_and_download_file,
web_research), MCP server expanded from 5 to 11 tools.

## [1.0.0] - 2026-04-15

Initial release: search, fetch, extract, download, browser automation,
MCP server with 5 tools.
