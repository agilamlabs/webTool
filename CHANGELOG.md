# Changelog

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
