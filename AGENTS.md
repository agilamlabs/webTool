# AGENTS.md

Project-level guide for AI coding agents (OpenAI Codex, Claude Code, Cursor, OpenClaw, etc.) working in this repository. Format follows the [agents.md convention](https://agents.md).

## Project: web-agent-toolkit

A professional Playwright-based agentic web search / fetch / extraction / download / browser-automation toolkit. Single Python package at `web_agent/`, MIT-licensed, async-first.

- Latest version: **1.6.6**
- Python: **3.10+**
- Single source of truth for the project surface: `web_agent/__init__.py`

## Setup

```bash
# Core
pip install -e ".[dev]"
playwright install chromium

# Optional: PDF/XLSX extraction
pip install -e ".[binary]"
```

The package has no system dependencies beyond what `playwright install` brings.

## Build / test / lint

Run all three gates before declaring work done:

```bash
python -m pytest -v          # ~450 tests; full suite must pass
python -m ruff check web_agent tests
python -m mypy web_agent
```

Integration tests (real Playwright browser, network) run under the `integration` marker — opt-in:

```bash
python -m pytest -v -m integration
```

CI runs lint + unit tests on every push and a separate integration job — see `.github/workflows/ci.yml`.

## Repository layout

```
web_agent/             # The package. One module = one responsibility.
  agent.py             # Public Agent orchestrator (entry point)
  models.py            # All Pydantic v2 models (single source for the wire format)
  config.py            # AppConfig + sub-configs (Browser/Fetch/Search/Safety/Cache/...)
  exceptions.py        # WebAgentError hierarchy (used in strict=True paths)
  browser_manager.py   # Chromium lifecycle + stealth + UA rotation
  search_engine.py     # SearchEngine -- chains providers
  search_providers.py  # SearXNGProvider / DDGSProvider / PlaywrightProvider
  web_fetcher.py       # WebFetcher.fetch / fetch_many / fetch_binary
  content_extractor.py # trafilatura -> bs4 -> raw, plus PDF / XLSX
  downloader.py        # 3-strategy file download with safety gates
  browser_actions.py   # 15 action types incl. coord-click/type/press (v1.6.6)
  recipes.py           # search_and_open_best_result, find_and_download_file,
                       # web_research, fill_form_and_extract
  session_manager.py   # Persistent browser sessions (cookies, storage)
  tab_manager.py       # Per-session tab lifecycle (v1.6.6)
  doctor.py            # Self-diagnostic capability probes (v1.6.6)
  cache.py             # DiskCache with TTL + LRU eviction (opt-in)
  audit.py             # JSONL audit log of every public Agent call (opt-in)
  rate_limiter.py      # Per-host token-bucket gate
  robots.py            # robots.txt obedience via stdlib urllib.robotparser
  correlation.py       # contextvars-based correlation IDs for tracing
  utils.py             # async_retry, get_random_user_agent, safe_join_path,
                       # is_private_address, BudgetTracker, check_domain_allowed
  debug.py             # On-failure HTML+screenshot+JSON capture
  mcp_server.py        # Model Context Protocol server entry point
  main.py              # Click-based CLI entry point

tests/                 # All tests; mirrors the package layout 1:1
```

## Public API surface

Everything an agent should import comes from the package root:

```python
from web_agent import (
    Agent, AppConfig,
    AgentResult, ExtractionResult, FetchResult, FetchDiagnostic,
    SearchResultItem, ResearchResult, DownloadResult, ScreenshotResult,
    ActionType, Action, LocatorSpec, FormFilterSpec,
    # Configs
    BrowserConfig, FetchConfig, SearchConfig, SafetyConfig,
    CacheConfig, AuditConfig, AutomationConfig, ExtractionConfig, DownloadConfig,
    # Exceptions (raised in strict=True paths)
    WebAgentError, NavigationError, SearchError, DownloadError,
    BrowserError, ExtractionError, ConfigError,
    DomainNotAllowedError, SafeModeBlockedError,
    ActionError, ActionTimeoutError, SelectorNotFoundError,
)
```

Anything not re-exported in `web_agent/__init__.py` is internal — do not depend on import paths like `web_agent.web_fetcher.WebFetcher`.

## Architecture invariants

These rules constrain every change:

- **Single Agent context.** All public usage starts with `async with Agent(config) as agent:`. The `Agent` owns Playwright lifecycle; nothing else should `start()`/`stop()` the browser.
- **Result-based by default, exceptions opt-in.** Public methods return result models with `errors`, `warnings`, `diagnostics`. Pass `strict=True` to opt into the exception path (`NavigationError`, `SearchError`, etc.).
- **Pydantic v2 everywhere on the wire.** Every method signature uses Pydantic models for input where complexity warrants (`FormFilterSpec`, `Action`, `LocatorSpec`) and always returns one. No raw dicts in the public API.
- **Three-tier extraction.** trafilatura → BeautifulSoup → raw text for HTML; pypdf for PDF; openpyxl for XLSX. Last layer always succeeds (or returns `extraction_method="none"` cleanly).
- **Safety-first, always opt-in for risky paths.** `SafetyConfig` gates JS evaluation, downloads, form submission, private-IP egress (SSRF). Robots is on by default; cache + audit + automation safety are off by default unless turned on in config.
- **Cache > robots > rate-limit > network.** That order in the fetcher matters — it's documented in `web_fetcher.py`.
- **Per-host rate limiting and per-host robots.txt** are checked before every outbound request.
- **Defense-in-depth URL safety.** Domain allowlist checked at fetch start, after Playwright redirects, and after BrowserActions navigate (per-action drift). Path traversal blocked in `safe_join_path`. Private IPs blocked in `is_private_address`.
- **Correlation IDs everywhere.** Every public Agent call wraps a `correlation_scope`; every result echoes `correlation_id` so logs/audit/trace tie together.

## Coding conventions

- **Line length 100, ruff format**, double quotes. Configured in `pyproject.toml`.
- **`from __future__ import annotations`** at the top of every new module so type hints don't pay runtime cost.
- **Imports**: stdlib → third-party → first-party (`from .module import X`), each block separated by a blank line. Ruff isort enforces this.
- **Type-checked with mypy strict.** Every function has signatures. Tests are the only place untyped is allowed.
- **No emojis in code, comments, or docs** unless the user asks.
- **Comments**: only when the *why* is non-obvious. No comment that just restates the next line.
- **No bare `except`.** Every catch states the exception type. `Exception` is fine; `BaseException` only for cancellation paths.
- **Logging via loguru.** `logger.info / .warning / .error` — never `print`. Use `{}` placeholder substitution: `logger.info("Fetched {n}", n=len(items))`.
- **Async-first.** Every I/O method is `async def`. No `time.sleep` in core code (use `asyncio.sleep` or `RateLimiter`).
- **Tests** live next to the package layout (`tests/test_<module>.py`). Mock-based unit tests first; integration tests go behind the `@pytest.mark.integration` marker.

## How to add a feature

1. **Model first.** Add the wire shape to `models.py`. Default to optional/empty fields so existing JSON dumps still parse.
2. **Re-export from `__init__.py`** if it's part of the public API.
3. **Implement in the appropriate module.** Don't reach across module boundaries — call existing public helpers.
4. **Wire in `Agent`** if it's a user-facing operation; thread it into `Recipes` for composite workflows.
5. **Add tests.** Mock-based unit tests in `tests/test_<feature>.py`. If it needs a real browser, add an integration test with `@pytest.mark.integration`.
6. **Run gates locally.** `pytest`, `ruff`, `mypy` — all three.
7. **Update CHANGELOG.md.** Every released change has an entry under the next version.
8. **README.md** if the feature is user-visible.

## What v1.6.1 added (so you know the conventions)

- `AgentResult.warnings` (non-fatal) split from `errors` (fatal).
- `AgentResult.download_candidates`: skipped file URLs as structured `SearchResultItem`.
- `AgentResult.diagnostics`: `list[FetchDiagnostic]` per URL — status, provider, block_reason.
- `Agent.search_and_extract(extract_files=True)` plus `WebFetcher.fetch_binary` for PDF/XLSX inline extraction.
- `_unwrap_search_url` auto-converts SERP URLs (`google.com/search?q=...`) to plain queries.
- `prefer_domains=[...]` parameter on ranking-based recipes.
- `Agent.fill_form_and_extract(url, FormFilterSpec)` for dynamic calendar/filter pages.
- Optional `[binary]` extra: `pip install web-agent-toolkit[binary]`.

## What v1.6.6 added

Six browser-control features, adapted from `browser-harness` but
adjusted to webTool's structured architecture and safety stance:
**webTool never attaches to the user's existing personal Chrome.**

**Browser launch (Features 1 + 2)**
- `BrowserConfig.isolation_mode` -- launches Chromium against a
  webTool-owned `--user-data-dir` (ephemeral tempdir by default,
  or a named persistent profile). Failed launches don't leak.
- `BrowserConfig.cdp_enabled` -- adds `--remote-debugging-port` to
  the launch args and exposes `Agent.get_cdp_endpoint()` for
  external observers. CDP requires isolation; `attach_existing_browser`
  is rejected at config validation.

**Tabs (Feature 3)**
- New `TabManager` per session: `agent.list_tabs / current_tab /
  new_tab / switch_tab / close_tab`. Popups auto-register but don't
  steal focus.
- `BaseAction` parent class adds optional `tab_id` to every Action
  input. Transparent to v1.6.5 JSON callers.
- **Behavior change:** session-owned `interact()` calls now reuse
  the session's current tab. Escape hatch:
  `automation.fresh_tab_per_call=True`.

**Coordinate fallbacks (Feature 4)**
- Three new Action types: `click_xy`, `type_text`, `press_key`. Use
  after `observe()` for canvas/shadow/iframe targets selectors can't
  reach. Coordinates are CSS pixels; honor `device_pixel_ratio` from
  observe.
- Top-level `Agent.click_xy / type_text / press_key` (all require
  `session_id`).

**Observe (Feature 5)**
- `Agent.observe(url, session_id, tab_id, include_text, include_aria)`
  returns an `ObserveResult` with screenshot path, viewport / page /
  scroll dimensions, DPR, optional truncated text, optional ARIA
  snapshot. Powers the observe -> act -> verify loop.

**Doctor (Feature 6)**
- `Agent.doctor(quick=False)` runs 14 capability probes and returns
  a `DoctorReport` with summary `healthy` | `usable_with_warnings` |
  `unusable`. CLI: `web-agent doctor [--quick] [--json]` -- exits 2
  on `unusable` so CI can gate on it.

**Tests:** 41 new across 7 files (`tests/test_v166_*.py`). Total
suite ~450, all green.

## What v1.6.5 added

A 16-issue review-pass focused on closing the SSRF / cookie-isolation
gaps that v1.6.4 left open and on the long-tail polish items the
external review surfaced.

**Critical (security)**
- **Per-host cookie isolation in `WebFetcher._cookies_for_session`.**
  Returns a domain-aware `httpx.Cookies` jar instead of a flat
  `{name: value}` dict; cookies for `bank.com` no longer leak to
  attacker.com when both share a `session_id`.
- **`classify_url` pre-gates the URL against `check_domain_allowed`**
  before any HEAD probe -- closes the input-side of the SSRF gap that
  v1.6.4 had only closed on the redirect side.
- **Playwright download paths re-validate post-redirect URLs.**
  `_do_save_page` checks `page.url`; `_download_with_playwright`
  checks `download.url` before any `save_as` call. Mirrors the SSRF
  hardening already in the httpx and Playwright fetch paths.

**High**
- **`env_nested_delimiter="__"`** added to `AppConfig.model_config` so
  `WEB_AGENT_BROWSER__HEADLESS=false` actually configures
  `browser.headless` (the README claim was previously silently broken).
- **Cached DNS resolution** in `is_private_address` via a 2048-entry
  LRU cache. The default-on private-IP gate no longer pays a fresh
  `getaddrinfo` per outbound request.

**Medium**
- **Cache-hit honesty.** `SearchEngine` no longer rewrites
  `searched_at` on cache hits; `from_cache=True` is the only
  staleness signal.
- **`async_retry` validates `max_retries >= 1`** at decorator-construction
  time instead of raising a bogus `TypeError` later.
- **`find_and_download_file` recovers extensionless binary URLs** via
  `classify_url`, so regulator-archive URLs without an extension are
  no longer silently dropped.
- **`_unwrap_search_url` caps the unwrapped query** at 1024 chars so a
  hostile SERP URL with a giant `?q=` payload cannot poison the
  pipeline.
- **Dead `_classify_message` / `_to_structured` /
  `_MESSAGE_PREFIX_CODES` removed** -- replaced by `_MessageBag` in
  v1.6.3; the back-compat shim is gone.

**Low / polish**
- **`take_screenshot` uses `FetchConfig.wait_until`** instead of
  hardcoded `networkidle`.
- **Shared `_OFFICE_AND_ARCHIVE_EXTENSIONS`** in `web_fetcher` consumed
  by both fetcher and downloader -- prevents drift between the two
  modules' extension lists.
- **`_PAGE_DIALOG_STATES: WeakKeyDictionary[Page, _DialogState]`**
  replaces the v1.6.4 hack of attribute-stuffing on Page objects.
- **`SafetyConfig` auto-normalizes URL-shaped patterns**
  (`"https://Evil.com/"` -> `"evil.com"`) so deny/allow lists from
  external config files actually match.
- **MCP server honors `WEB_AGENT_CONFIG`** env var pointing at a YAML
  file -- operator can deploy with `safe_mode=True` / domain
  allowlists without code changes.

## What v1.6.4 added

- **Cross-platform `safe_join_path`.** New `_is_cross_platform_absolute`
  helper rejects POSIX, Windows-drive (any letter, either slash),
  Windows-root, and UNC absolute paths regardless of OS. Closes the
  Linux-only CI test failure that's been pending since v1.6.1.
- **bs4 meta content coercion.** `Tag.get("content")` is widened to
  `str | None` at the call site. Fixes 2 mypy errors on newer bs4
  stubs.
- **Playwright download size cap.** Strategy 2 (page-save) pre-checks
  Content-Length + rendered DOM size before disk write; Strategy 3
  (expect-download) stat-checks and unlinks oversize results.
- **HEAD probe redirect re-validation.** `classify_url` now treats a
  HEAD that redirects to a denied host as `unknown` instead of
  reporting on the denied target's content-type.
- **`SECURITY.md`.** Threat model, defense-in-depth layers, hardening
  recipe for production, DNS rebinding limitation documented.
- **README polish.** CI badge, Python-version badge, source-install
  clarity (no PyPI yet).

## What v1.6.3 added

- **Smart routing in `search_and_extract`'s direct-URL path.** Previously
  the URL-as-query branch called `fetch` directly; now it runs the same
  classification + routing as `fetch_and_extract`.
- **Parallel HEAD-probe of search results.** `_url_ext_classification`
  splits results into `binary` / `html` / `unknown`; only `unknown`
  URLs are probed, in parallel via `asyncio.gather` (one RTT total).
- **`classify_url(url, *, session_id=...)`.** HEAD probe inherits
  Playwright session cookies for authenticated extensionless documents.
- **`_MessageBag`** records ToolMessage codes at the call site (no more
  prefix-string classification on the hot path).
- **`AppConfig.ranking_profiles: dict[str, list[str]]`** lets callers
  add or override ranking profiles without touching source. Merged with
  built-in `RANKING_PROFILES` at `Recipes.__init__`; user wins on collision.

## What v1.6.2 added

- **Smart binary routing.** `Agent.fetch_and_extract(url)` HEAD-probes
  extensionless URLs (`SafetyConfig.probe_binary_urls=True` by default)
  and routes detected binaries to `WebFetcher.fetch_binary`.
- **Streaming + size cap.** `fetch_binary` streams response chunks and
  enforces `DownloadConfig.max_file_size_mb`.
- **Session cookies.** `fetch_binary(session_id=...)` copies cookies
  from the Playwright `BrowserContext` into httpx for authenticated
  document fetches.
- **CSV + DOCX extraction.** `ContentExtractor` adds `_extract_csv`
  (stdlib, no dep) and `_extract_docx` (python-docx, in `[binary]`).
  Dispatch in `extract()` via `_is_csv` / `_is_docx`.
- **Ranking profiles.** Named host lists in `recipes.RANKING_PROFILES`
  reachable via `domain_profile=` param on the recipes.
- **Structured `ToolMessage`.** New `AgentResult.structured_warnings`
  and `structured_errors` lists alongside the legacy strings.
- **MCP surface.** `web_search(extract_files=...)`,
  `web_search_best(prefer_domains=..., domain_profile=...)`,
  `web_research(...)`, new `web_fill_form_and_extract` tool.
- **Defaults.** `FetchConfig.wait_until="domcontentloaded"` (was
  `"networkidle"`); CI tests Python 3.13 alongside 3.10 and 3.12.

## When in doubt

- Read `web_agent/agent.py` first — every public surface is wired there.
- The `models.py` file is the single source of truth for the public data shape. If a field isn't there, the agent can't see it.
- `tests/` mirrors `web_agent/` 1:1 — find the test that covers the module you're touching, copy its fixture style, add a sibling test.
- Every new feature must keep the "result-based by default" contract. If you find yourself raising in a public method without a `strict=True` flag, stop and reconsider.
