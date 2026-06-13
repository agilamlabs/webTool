# AGENTS.md

Project-level guide for AI coding agents (OpenAI Codex, Claude Code, Cursor, OpenClaw, etc.) working in this repository. Format follows the [agents.md convention](https://agents.md).

## Project: web-agent-toolkit

A professional Playwright-based agentic web search / fetch / extraction / download / browser-automation toolkit. Single Python package at `web_agent/`, MIT-licensed, async-first.

- Latest version: **1.7.0**
- Python: **3.10+**
- Single source of truth for the project surface: `web_agent/__init__.py`

## Setup

```bash
# Core (Python API only)
pip install -e ".[dev]"
playwright install chromium

# Optional: MCP server (v1.6.9+; mcp[cli] is now an extra)
pip install -e ".[mcp]"

# Optional: PDF/XLSX/DOCX extractors
pip install -e ".[binary]"
```

The package has no system dependencies beyond what `playwright install` brings.

## Build / test / lint

Run all three gates before declaring work done:

```bash
python -m pytest -v          # 1324 unit tests; integration tests auto-excluded via pyproject addopts (-m "not integration")
python -m ruff check web_agent tests
python -m mypy web_agent
```

Integration tests (real Playwright browser, network) run under the `integration` marker — opt-in (28 tests; v1.7.0 quarantined them behind this marker so a bare `pytest` no longer launches real Chromium):

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
  browser_manager.py   # Chromium lifecycle + per-context stealth + UA rotation + crash auto-relaunch (v1.7.0)
  challenge.py         # Bot-wall / CAPTCHA structural detection -> ChallengeInfo (v1.7.0)
  search_engine.py     # SearchEngine -- chains providers + per-provider circuit breaker (v1.7.0)
  search_providers.py  # SearXNGProvider / DDGSProvider / PlaywrightProvider
  web_fetcher.py       # WebFetcher.fetch / fetch_many / fetch_binary + challenge settle-recheck (v1.7.0)
  content_extractor.py # trafilatura -> bs4 -> raw, plus PDF / XLSX; max_chars/offset slicing (v1.7.0)
  downloader.py        # 3-strategy file download with safety gates
  browser_actions.py   # 19 action types incl. coord-click + iframe + shadow-DOM + upload + drag (v1.6.6 + v1.6.7)
  recipes.py           # search_and_open_best_result, find_and_download_file,
                       # web_research, fill_form_and_extract
  session_manager.py   # Persistent browser sessions (cookies, storage) + storage_state export/import + idle reaper (v1.7.0)
  tab_manager.py       # Per-session tab lifecycle (v1.6.6)
  doctor.py            # Self-diagnostic capability probes (v1.6.6)
  domain_skills.py     # Domain skill registry + dispatcher (v1.6.7)
  workspace.py         # Agent-editable workspace with mode gates (v1.6.7)
  builtin_skills/      # Bundled domain skills (sec.gov / github.com / ec.europa.eu)
  network_collector.py # Per-Page request/response/download event collector (v1.6.8)
  trace_recorder.py    # Per-session JSONL action traces for replay (v1.6.8)
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

Everything an agent should import comes from the package root (`web_agent/__init__.py` exports **118 names** as of v1.7.0):

```python
from web_agent import (
    # Core entry points
    Agent, Recipes, AppConfig,

    # Result models
    AgentResult, ExtractionResult, FetchResult, FetchDiagnostic,
    SearchResponse, SearchResultItem, ResearchResult, DownloadResult,
    ScreenshotResult, ActionResult, ActionSequenceResult,
    SessionInfo, TabInfo, ObserveResult, DoctorReport, DoctorCheck,
    DomainSkill, SkillInputSpec, SkillApplicationResult,
    NetworkEvent, Citation, ToolMessage, ToolError, ToolWarning, ToolSeverity,
    ChallengeInfo, StorageStateResult, SearchOutcome,    # v1.7.0

    # Action discriminated union + members
    Action, ActionType, ActionStatus, BaseAction,
    ClickXYInput, PressKeyInput, TypeTextInput,            # v1.6.6 coord fallbacks
    UploadFileInput, IframeClickInput,                     # v1.6.7 interaction lib
    ShadowDomClickInput, DragAndDropInput,
    # (12 selector-based members -- ClickInput, FillInput, etc. -- live in models.py
    # but aren't re-exported individually; they enter through the `Action` union.)

    # Specs / helpers
    LocatorSpec, SelectorLike, FormFilterSpec,

    # Configs (13 sub-configs)
    BrowserConfig, FetchConfig, SearchConfig, SafetyConfig,
    CacheConfig, AuditConfig, AutomationConfig, ExtractionConfig, DownloadConfig,
    SkillsConfig, WorkspaceConfig,                         # v1.6.7
    DiagnosticsConfig,                                     # v1.6.8
    ProxyConfig,                                           # v1.7.0

    # v1.6.8 diagnostic primitives
    NetworkCollector, SessionTraceRecorder,

    # Correlation + infra
    correlation_scope, get_correlation_id, new_correlation_id,
    AuditLogger, RateLimiter, RobotsChecker, Cache, DiskCache,
    BudgetTracker, RetryPolicy, get_retry_policy,

    # Search engine + providers (for custom chain configs)
    SearchEngine,                                         # v1.7.0
    SearchProvider, SearXNGProvider, DDGSProvider, PlaywrightProvider,

    # Exceptions (raised in strict=True paths)
    WebAgentError, NavigationError, SearchError, DownloadError,
    BrowserError, ExtractionError, ConfigError,
    DomainNotAllowedError, SafeModeBlockedError, BudgetExceededError,
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
- **Bot-walls are surfaced, not swallowed (v1.7.0).** Challenge detection (`challenge.py`) adds `FetchStatus.BLOCKED`: a detected bot-wall / CAPTCHA — even one served on HTTP 200 — returns `BLOCKED` with an actionable `error_message` and `FetchResult.challenge`, never SUCCESS-with-garbage. `BLOCKED` results are never cached. Detection is **structural** (page markers), so prose vendor mentions must not trigger it.
- **MCP responses are single-representation + capped (v1.7.0).** Content-returning MCP tools return ONE representation (markdown default, `html` only on explicit `format='html'`) capped at `extraction.default_max_chars` (40000), with `offset` / `next_offset` paging. This cap lives **at the MCP boundary only** — the Python API default stays unlimited, so the result-model contract is unchanged.
- **Proxy + fingerprint are operator controls, off by default (v1.7.0).** `ProxyConfig` is inactive unless configured (zero behaviour change when unset) and threads through every Chromium launch + httpx side-path. `coherent_fingerprint` keeps the rotated UA's OS family consistent with the configured locale/timezone. Scope is compliant-access coherence, **not** a stealth-bypass promise.

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

## What v1.7.0 added

**Real-world hardening — solve the problems autonomous agents
actually hit on the live 2026 web.** A market-research +
full-codebase gap analysis drove six additive waves: bot-wall
detection, token-blowout / failure-opacity fixes, production
lifecycle hardening, auth persistence, search resilience, and proxy
support. **No breaking changes to documented Python public APIs** —
the only visible behaviour shifts are called out at the end. Closed
out by a 4-dimension adversarial multi-agent review (security /
fetch correctness / concurrency / search+wiring) of the
~7,400-line diff that found **no critical/high issues** and
confirmed the SSRF, path-traversal, cookie-domain, and
session-isolation guarantees survive the refactor. Gates: **1324
passed / 28 deselected**, ruff clean, mypy strict clean.

**Wave 0 — Hermetic suite + toolchain.** 28 live-network /
real-browser tests are now quarantined behind the `integration`
marker (it was declared but selected 0 tests; some "passing" tests
were silently launching real Chromium). Default `pytest` runs
`-m "not integration"` via `pyproject` `addopts`; the CI integration
job switched to `-m integration`. 6 mypy strict drift errors fixed.

**Wave 1A — Bot-wall / challenge detection** (new
`web_agent/challenge.py`). `detect_challenge(html, status_code,
headers, final_url) -> ChallengeInfo | None` with high-precision
**structural** markers (Cloudflare / DataDome / Akamai /
PerimeterX-HUMAN / reCAPTCHA / hCaptcha) — prose mentions never
trigger. New `FetchStatus.BLOCKED` + `FetchResult.challenge`. A
challenge on HTTP 200 returns `BLOCKED` with an actionable
`error_message` (was SUCCESS-with-garbage). Bounded
settle-and-recheck in `web_fetcher` re-captures a managed-JS
challenge a real browser auto-passes; `BLOCKED` is **never cached**;
`FetchDiagnostic.block_reason='bot_challenge'`. New `FetchConfig`:
`challenge_detection_enabled` / `challenge_settle_ms` /
`challenge_max_rechecks`.

**Wave 1B — Token blowout + failure transparency.**
`ExtractionResult` gained additive `fetch_status` / `status_code` /
`error_message` / `failure_stage` (a failed fetch now says *why* —
403 bot-wall vs robots-disallowed vs timeout vs blocked-domain —
instead of an opaque empty result) plus paged-read `truncated` /
`total_content_chars` / `content_offset` / `next_offset`.
`content_extractor.extract` gained newline-snapped `max_chars` /
`offset`. `ExtractionConfig.default_max_chars` (40000) caps **at
the MCP boundary only** — the Python API default stays unlimited.
`recipes.fill_form_and_extract` stamps a distinct `failure_stage`
at each exit (navigation / query_fill / filter_fill / submit /
wait_for / ssrf_redirect / capture).

**Wave 1C — Production lifecycle.** Fixed a real launch break:
playwright-stealth 2.x's hooked `launch` rejected the
ephemeral-isolation `--user-data-dir`; the launch path is now raw
`async_playwright()` with stealth applied per-context
(`_apply_stealth`), and **both** isolation flavours dispatch through
`launch_persistent_context`. Added Chromium crash auto-relaunch
(bounded + backoff), an idle-session reaper + hard session cap,
orphaned-ephemeral-profile sweep on start, and a `doctor --quick`
that finally catches a missing Chromium executable. New
`BrowserConfig`: `stealth_enabled`, `auto_relaunch`,
`relaunch_max_attempts`, `relaunch_backoff_base_s`,
`session_max_count`, `session_idle_ttl_s`, `profile_sweep_max_age_h`.

**Wave 2D — Auth persistence + login handoff.**
`SessionManager.export_state` / `import_state` round-trip a
logged-in `storage_state` (cookies + origins) to JSON and rehydrate
it in a later process — *log in once (human does
password/2FA/CAPTCHA), automate afterwards.* Path confined to
`download_dir` via `safe_join_path`; import refuses files over 8 MB.
New `StorageStateResult`; `SessionInfo.has_storage_state`; new
`Agent.export_session_state` / `import_session_state`. **New MCP
session surface** (there was previously NO way to create a session
over MCP despite tab tools needing a `session_id`):
`web_create_session`, `web_list_sessions`, `web_close_session`,
`web_export_session`, `web_import_session`. Cookies rehydrate fully;
per-origin `localStorage` is best-effort.

**Wave 2E — Search resilience + links-only surface.** `SearchEngine`
gained a per-provider **circuit breaker** (a just-blocked/errored
provider is skipped for a bounded cooldown then re-probed;
injectable clock) and distinguishes "all providers blocked" from
"genuine zero hits" via a new `SearchOutcome` + `ProviderBlockedError`,
surfaced on `SearchResponse.search_blocked`. New
`Agent.search(query, max_results, strict) -> SearchResponse` is
**links-only** (no fetch/extract) — the most-used primitive was
missing. New MCP tool `web_search_links`. New
`SearchConfig.circuit_cooldown_s`.

**Wave 2F — Proxy + fingerprint coherence.** New `ProxyConfig`
sub-config (env `WEB_AGENT_PROXY__SERVER` / `__USERNAME` /
`__PASSWORD` / `__BYPASS`; scheme http/https/socks5 validated;
**inactive by default → zero behaviour change**) threaded into all
three Chromium launch dispatches **and** the httpx side-paths (HEAD
probe, `fetch_binary`) via httpx 0.28's `proxy=` kwarg.
`BrowserConfig.coherent_fingerprint` keys the UA pool by OS family
and pins the rotated UA's OS family to the configured locale (no
UA whose OS contradicts platform/locale/timezone); the bare httpx
side-paths send a browser-coherent `User-Agent` + `Accept-Language`.
Honest scope: **operator controls for compliant access, not a
stealth-bypass promise.**

**Close-out fixes.** MEDIUM: short HTTP-200 login/signup pages
embedding reCAPTCHA were wrongly `BLOCKED` — a CAPTCHA script on a
200 now only counts with an access-denial `<title>`. LOW:
`import_state` 8 MB size cap.

**New public surface (additive).** 5 new exports — `ChallengeInfo`,
`StorageStateResult`, `ProxyConfig`, `SearchEngine`, `SearchOutcome`
(root now 118). 6 new MCP tools (`web_search_links` + 5 session
tools); MCP server count ~39 → 45.

**Tests.** ~1133 → **1324 passing** (+28 `integration`-marked,
opt-in). New files: `test_challenge_detection`,
`test_failure_transparency`, `test_token_efficiency`,
`test_lifecycle`, `test_auth_persistence`, `test_search_resilience`,
`test_proxy_fingerprint`, `test_v170_wiring`; plus rewrites of
`test_v166_cdp` / `test_v166_isolation` / `test_v168_remote_cdp` /
`test_v169_persistent_profile` for the new launch path.

**Behaviour changes (non-breaking but visible).** (1) A bot-challenge
page that used to return `SUCCESS` now returns `FetchStatus.BLOCKED`.
(2) MCP content responses return **one** representation (markdown
default) capped at `extraction.default_max_chars` (40000) per call —
was up to ~1 MB, often duplicated; pass `format='html'` for raw
HTML, a larger `max_chars`, or page via `offset` / `next_offset`.
(3) A bare `pytest` run now excludes integration tests (opt in with
`-m integration`).

## What v1.6.14 added

**Hardening slice — 8 Critical fixes from a brutal full-codebase
audit.** No new features. Pure correctness, security, and DoS
hardening. Bundle of 8 fixes across 10 source files + 3 new test
files (+22 tests). Implementation delegated to 3 specialised
`general-purpose` agents working in parallel on disjoint file sets;
~12 minutes wall-clock.

The 8 Criticals were surfaced by a v1.6.13 close-out review that
spawned 4 parallel `feature-dev:code-reviewer` agents (security,
fetch/extract correctness, browser orchestration, API/tests/docs).
The synthesis identified 8 must-fix findings with confidence ≥85%,
which became this slice.

**Security cluster (C-2, C-3, C-5)** — `browser_actions.py`,
`agent.py`, `trace_recorder.py`, `mcp_server.py`

- **C-2**: `WaitInput(target=FUNCTION)` honours
  `safety.allow_js_evaluation`. Pre-flight scanner in
  `BrowserActions.execute_sequence` previously gated only
  `EvaluateInput`; `wait_for_function` executes arbitrary JS in
  the page context and was a cookie-exfil vector via LLM prompt
  injection. Now gated symmetrically.
- **C-3**: `Agent.replay_trace` + `TraceRecorder.load_entries`
  reject `trace_file` paths outside `trace_dir`. Defence-in-depth
  (both layers check) closes a LFI via MCP `web_replay_trace`.
- **C-5**: `web_interact` MCP docstring updated 12 -> 19 action
  types. The stale doc hid 7 v1.6.6/v1.6.7 actions (`click_xy`,
  `type_text`, `press_key`, `upload_file`, `iframe_click`,
  `shadow_dom_click`, `drag_and_drop`) from every MCP client's LLM.

**Throughput / DoS cluster (C-1, C-4, C-7)** — `rate_limiter.py`,
`web_fetcher.py`, `network_collector.py`, `config.py`

- **C-1**: `RateLimiter.MAX_RETRY_AFTER_SECONDS = 300.0` class
  constant + clamp in `notify_429`. A server's
  `Retry-After: 99999999` used to put `acquire(host)` into a
  ~1157-day sleep. Now capped at 5 minutes.
- **C-4**: `WebFetcher.fetch_many` with `session_id` bounded by
  `asyncio.Semaphore(BrowserConfig.max_pages_per_session_fetch)`
  (new config field, default 5, ge=1, le=50). Pre-v1.6.14 ran
  unbounded `asyncio.gather` over a single `BrowserContext`,
  reproducibly crashing the Chromium renderer at ~20+ parallel
  pages.
- **C-7**: `NetworkCollector.wait_for_pending_bodies` rewritten to
  use `asyncio.wait(timeout=...)` + explicit `task.cancel()` per
  pending task + drain. The previous
  `asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True),
  timeout=N)` cancelled the gather wrapper but NOT its children,
  orphaning body-capture tasks against possibly-closed Pages.

**Pipeline cluster (C-6, C-8)** — `recipes.py`, `tab_manager.py`

- **C-6**: `Recipes.fill_form_and_extract` short-circuits with
  `ExtractionResult(extraction_method="none", content_length=0)`
  when `safe_page_content` returns `("", "navigating")`. Pre-
  v1.6.14 built a misleading `FetchResult(status=SUCCESS, html="")`
  that hid the capture failure from the caller. Matches the
  `Downloader._do_save_page` `NETWORK_ERROR` pattern from v1.6.13.
- **C-8**: `TabManager.close_tab` holds `_lock` across
  `page.close()`. Pre-v1.6.14 the lock was released before the
  await, so a concurrent `switch_tab` could observe an
  inconsistent intermediate state while `_evict_on_close` (sync
  Playwright close-event callback) was mutating `_tabs` /
  `_current_tab_id`.

**Tests (22 new, AsyncMock-driven, no Playwright launch)**

- `tests/test_v1614_security.py` (8 tests): WaitInput JS gate
  block + allow paths, regression guard for non-FUNCTION wait
  targets, replay_trace containment for absolute and `..` escape,
  defence-in-depth at the trace-recorder layer, docstring
  introspection for all 19 action types.
- `tests/test_v1614_throughput.py` (8 tests): Retry-After cap
  with extreme and `None` cases + normal-value pass-through;
  fetch_many session-path semaphore + ephemeral-path no-gate;
  wait_for_pending_bodies cancellation + empty + drain.
- `tests/test_v1614_pipeline.py` (4 tests): fill_form
  nav-race short-circuit + happy-path preservation; close_tab
  lock-held-across-close + double-close idempotency.

**No breaking changes** to documented v1.6.13 public APIs. The new
`BrowserConfig.max_pages_per_session_fetch=5` default is the only
behavioural cap; raise via `AppConfig(browser={"max_pages_per_session_fetch": 20})`
if you want the pre-v1.6.14 unbounded behaviour (at your renderer's
risk).

## What v1.6.13 added

Single-slice patch addressing one specific production failure mode
surfaced in the v1.6.12 close-out discussion: `page.content()`
raising `"Unable to retrieve content because the page is navigating
and changing the content"` mid-fetch. The race is transient (the
page is loaded fine, the snapshot moment was wrong) but pre-v1.6.13
it triggered a full re-navigation via the `async_retry` decorator
(2-5s wasted per occurrence) and on aggressively redirecting pages
could exhaust retries and fail the fetch entirely.

**New public helper**
- `safe_page_content(page, *, retries=3, settle_ms=250,
  use_cdp_fallback=True, cdp_timeout_ms=5000) -> tuple[str, HtmlCaptureSource]`
  in `utils.py`, where
  `HtmlCaptureSource = Literal["content", "evaluate", "cdp", "navigating"]`
  is exported from `web_agent.models` (single source of truth shared
  with `FetchResult.html_capture_source`). Three-tier capture:
  1. **`page.content()`** with bounded retry on the specific
     navigation-race message (matched via substring on
     `"navigating and changing"` / `"page is navigating"` -- the
     typed Playwright error class is private). Each retry is
     preceded by `wait_for_load_state("domcontentloaded",
     timeout=2000)` + a `settle_ms` sleep.
  2. **`page.evaluate('document.documentElement.outerHTML')`** --
     runs in the page context, tolerates some races the remote
     protocol rejects.
  3. **CDP `DOM.getOuterHTML`** -- reads the browser's internal
     DOM tree directly. Session detached in a `finally` block so
     it never leaks. Skipped when `use_cdp_fallback=False`.
  Returns `(html, source)` where source is `"content" | "evaluate"
  | "cdp" | "navigating"`. Designed to **never raise on the race
  path**. Non-race exceptions re-raise so the outer `async_retry`
  decorator owns generic failure handling.

**New model field**
- `FetchResult.html_capture_source: Optional[Literal["content",
  "evaluate", "cdp", "navigating"]]` -- surfaces which tier won,
  for telemetry / extractor / recipe consumers. `None` for binary
  fetches.

**Call sites refactored**
- `web_fetcher.py:634` (main fetch path; propagates source into
  `FetchResult.html_capture_source`).
- `downloader.py:432` (save-page; logs source at INFO when degraded,
  returns `HTTP_ERROR` when all tiers abandon -- no zero-byte
  files).
- `recipes.py:898` (`fill_form_and_extract`; especially prone to
  the race because form-submit flows often redirect).
- `debug.py:80` (failure-time snapshots; the race fires
  particularly often here because debug capture runs mid-failure
  when the page is often already redirecting).

**Tests**
- 11 new tests in `tests/test_agent.py::TestV1613Integration`. All
  drive `safe_page_content` directly with `AsyncMock`-backed Page
  objects -- no Playwright launch. Cover: happy path, retry on
  race, evaluate fallback, CDP fallback (incl. detach cleanup),
  all-tiers-fail returns `("", "navigating")`, non-race errors
  re-raise, CDP-disabled skip, schema test for the new model
  field, race-marker detection across both upstream message
  variants, **settle-skip on last tier-1 attempt** (review-pass
  I-2: N-1 settles for N attempts), **CDP hang times out via
  `asyncio.wait_for`** (review-pass M-2: detach still cleans up).

**Files changed**
- `web_agent/utils.py`, `web_agent/models.py`,
  `web_agent/web_fetcher.py`, `web_agent/downloader.py`,
  `web_agent/recipes.py`, `web_agent/debug.py`,
  `web_agent/__init__.py`, `tests/test_agent.py`,
  `CHANGELOG.md`, `README.md`, `AGENTS.md`.

**No breaking changes.** All v1.6.12 public APIs unchanged.
`html_capture_source` defaults to `None` so existing
`FetchResult(...)` callers see no schema break.

**Review-pass fixes folded in** (0 Critical, 2 Important, 3 Minor;
all 5 fixed in v1.6.13 itself):
- I-1: `downloader.py` returns `NETWORK_ERROR` (not `HTTP_ERROR`)
  when all 3 capture tiers abandon. Content-capture failure is
  transport-level, not an HTTP status from the server.
- I-2: `safe_page_content` skips the settle on the final tier-1
  attempt -- saves up to 2.25s of latency on the degraded path.
- M-1: `HtmlCaptureSource` literal alias moved to `models.py` as
  the single source of truth. Exported from `web_agent.*`.
- M-2: `cdp_timeout_ms` wired via `asyncio.wait_for` around each
  `cdp.send` -- a hung CDP session can't block the helper.
- M-3: doc signatures updated to show
  `tuple[str, HtmlCaptureSource]` not loose `tuple[str, str]`.

## What v1.6.12 added

Throttle + telemetry + structured-data patch on v1.6.11. Three
slices: HTTP 429 handling, granular per-fetch telemetry, and
structured-data extraction (JSON-LD always-on plus opt-in XHR/fetch
JSON body capture and a `prefer_api` extractor mode).

**Behaviour change (Item 1)**
- HTTP 429 ("Too Many Requests") no longer returns
  `FetchResult(status=SUCCESS, status_code=429)`. The new branch in
  `WebFetcher._fetch_with_retry`:
  1. Parses `Retry-After` via the new `parse_retry_after` helper.
  2. Signals the per-host `RateLimiter` via the new `notify_429`
     method -- extends `_next_allowed` so the next `acquire(host)`
     waits at least `max(Retry-After, interval * 2.0)`.
  3. Raises a retryable `Exception`, so `async_retry` retries with
     the new wait honoured.

  **Migration**: callers explicitly checking
  `fetch_result.status_code == 429` will no longer see that case --
  they will see either a successful retry result OR (after
  `max_retries` exhausted) a raised `Exception`.

**New public API (Item 2)**
- `parse_retry_after(header_value: str | None) -> float | None` --
  RFC 9110 §10.2.3 parser handling both integer-seconds and HTTP-date
  forms. Re-exported from `web_agent`. Uses `email.utils` (stdlib).

**New rate limiter method**
- `RateLimiter.notify_429(host, retry_after_seconds=None, *, fallback_factor=2.0)`
  -- callers use the limiter via `Agent` indirection, but the method
  is callable on any constructed `RateLimiter` for advanced
  scenarios. Internal `_429_counts` tally tracked for a future
  adaptive-rps policy.

**Telemetry depth (Item 3)**
- `NetworkEvent.ttfb_ms` -- from Playwright's
  `request.timing['responseStart']`. None on cross-origin requests
  with restricted `Timing-Allow-Origin`.
- `NetworkEvent.body_size` -- from `Content-Length` header. None on
  chunked responses (we deliberately avoid `await response.body()`).
- `FetchResult.ttfb_ms` -- TTFB of the first `document`-typed
  response (the navigation).
- `FetchResult.dom_parse_ms` -- from
  `performance.getEntriesByType('navigation')[0]`'s
  `domInteractive - responseEnd` (true parse time, not post-parse
  load). None on `about:blank` / `data:` / sandboxed iframes.
- `FetchResult.total_bytes_downloaded` -- page weight = sum across
  all response `body_size` values when `capture_network=True`
  (includes main document + subresources -- use `len(html)` for
  the navigation response body size).

All five new model fields are `Optional[...]` with `None` default.

**Structured-data extraction (Items 6-8)**
- **JSON-LD enrichment (always-on)**.
  `ExtractionResult.structured_data: list[dict]` populated from
  `<script type="application/ld+json">` blocks on every HTML
  extraction. `@graph` containers are unwrapped. Malformed JSON-LD
  is swallowed (very common in the wild). Module-level
  `_extract_json_ld(html)` helper.
- **XHR/fetch body capture (opt-in)**.
  `DiagnosticsConfig.capture_response_bodies` + companion
  `max_response_body_bytes` (default 256 KiB) +
  `body_capture_content_types` (default JSON variants). When on,
  `NetworkCollector` schedules async body reads + drains them via
  the new `wait_for_pending_bodies(timeout=5.0)` (auto-wired into
  `WebFetcher.fetch`). Two new `NetworkEvent` fields:
  `body_text: Optional[str]` + `body_truncated: bool`.
- **`ContentExtractor.extract(prefer_api=True)`** routes through
  the largest captured JSON body when one is available; falls back
  transparently to trafilatura/bs4/raw when not. New extraction
  method value `"api_json"`. Useful on SPAs where the XHR payload
  is strictly cleaner than the rendered DOM.

**Tests**
- 11 new unit-level integration tests in
  `tests/test_agent.py::TestV1612Integration`. Originals:
  `parse_retry_after` × 2, `notify_429`, `_signal_429`, WebFetcher
  429-branch source-inspection, `NetworkCollector._on_response`
  TTFB+body-size capture. New for structured-data:
  `_extract_json_ld` happy + malformed-tolerant, async body capture
  + truncation, `prefer_api=True` extractor routing.

## What v1.6.11 added

Follow-up polish on v1.6.10. No new features -- 7 items addressing one
behavioural issue, one correctness gap, one stale-migration-wording
bug, plus four polish items.

**Behaviour changes (Items 1-2)**
- `web_research(extract_files=True)` now filters search results
  against `EXTRACTABLE_BINARY_KINDS = {"pdf", "xlsx", "docx", "csv"}`
  BEFORE calling `fetch_smart`. `.mp4` / `.exe` / `.iso` / `.zip` are
  routed to `download_candidates` with the new
  `block_reason="not_extractable_kind"`. Pre-v1.6.11 these were
  fetched as binary and the v1.6.10 I-1 guard caught them post-fetch
  (wasted bandwidth). The I-1 guard remains as the safety net for
  HEAD-probed extensionless binaries.
- `Recipes.find_and_download_file` no longer falls back to "any
  download-looking URL" when no extension match exists -- v1.6.11
  removes the prior Fallback 1. A caller asking for
  `file_types=["pdf"]` over results containing only `.xlsx` URLs now
  gets `NETWORK_ERROR` instead of the wrong file. **Migration**:
  widen `file_types` explicitly (e.g. `["pdf", "xlsx", "docx",
  "csv"]`) to opt back into a multi-kind search.

**New public API**
- `EXTRACTABLE_BINARY_KINDS: frozenset[str]` -- subset of
  `_BINARY_KINDS` that the `ContentExtractor` can extract text from.
- `is_extractable_binary_kind(kind: str) -> bool` -- public-stable
  predicate for the set above. Re-exported from `web_agent`.

**Docs cleanup**
- CHANGELOG v1.6.10 migration sentence: `_is_binary_kind` ->
  `is_binary_kind` (C-2 fix renamed the helper but the migration
  instruction still referenced the underscore name).
- `fetch_smart()` and `_inspect_element_at_point()` docstrings
  refreshed to v1.6.10 semantics (granular kinds and
  `coordinate_click_unknown_policy="block"` respectively).
- README replaces the hardcoded "37 tools" MCP count with category
  wording (drift-proof).
- SECURITY.md `cdp_host` bullet now aligns with the `remote_cdp_url`
  bullet's loopback wording (`127.0.0.0/8 / ::1 / localhost`).

**Tests**
- 4 new unit-level integration tests in
  `tests/test_agent.py::TestV1611Integration`:
  `extract_files=True` filter (skip + allow paths), and
  `find_and_download_file` Fallback 1 removal (rejection + happy
  path). Mocked `Recipes` -- no Playwright launch.

## What v1.6.10 added

Follow-up hardening on v1.6.9. No new features -- 8 items spanning one
real functional bug fix plus seven consistency / UX improvements.

**Functional correctness (Items 1-3, must-fix)**
- `Recipes.web_research` now accepts successful binary `FetchResult`s
  from `fetch_smart`. The v1.6.9 gate (`not fr.html`) dropped them as
  `fetch_failed`; the v1.6.10 gate is `not (fr.html or fr.binary)`.
  Extensionless PDFs / regulator dashboards are no longer silently
  lost.
- New `web_research(extract_files=False)` param mirroring
  `search_and_extract(extract_files=False)`. When True, download-URL
  search results are extracted inline via `fetch_smart` instead of
  routed to `download_candidates`. Default False preserves v1.6.9
  behaviour.
- `WebFetcher.classify_url` returns one of `'pdf' | 'xlsx' | 'docx'
  | 'csv' | 'zip' | 'binary_other' | 'html' | 'unknown'` instead of
  collapsing every binary to `"binary"`. New `is_binary_kind(s)`
  helper (`from web_agent import is_binary_kind`) is the routing
  predicate. `find_and_download_file(file_types=["pdf"])` now
  rejects extensionless XLSX/ZIP that HEAD-probed as binary.
  **Breaking** for direct callers comparing to `"binary"`; migration:
  use `is_binary_kind(c)`.

**Consistency / UX (Items 4-6, should-fix)**
- `SafetyConfig.coordinate_click_unknown_policy: Literal["allow",
  "block"] = "allow"`. When `"block"`, click_xy rejects clicks where
  `elementFromPoint` returns no element. Forced `"block"` in
  safe_mode. Independent of `allow_form_submit` (review C-1 fix) --
  fires whenever `allow_coordinate_clicks=True`, so strict callers
  can opt into block-on-unknown without disabling submits.
- `BrowserConfig.cdp_host` validation now uses `_is_loopback_host`
  (accepts `127.0.0.0/8`, `::1`, `localhost`) matching the
  `remote_cdp_url` semantics widened in v1.6.8 (review C-3).
- `Agent.get_owned_cdp_connection_info()` returns a structured
  `CdpConnectionInfo` (`cdp_url`, `profile_dir`, `ownership_token`)
  or `None`. Bundles the three values a sibling `remote_cdp` Agent
  needs, so callers don't have to discover three separate
  `BrowserManager` getters. New MCP tool
  `web_get_owned_cdp_connection_info`. `CdpConnectionInfo` exported
  from `web_agent`.

**Docs + tests (Items 7-8)**
- README + AGENTS now prominently warn that named profiles expose a
  **single shared `BrowserContext`** for all `session_id`s
  (Playwright limitation). Use `profile_mode="ephemeral"` for
  per-session isolation.
- 6 new integration tests in
  `tests/test_agent.py::TestV1610Integration`: connection-bundle
  return, cookie persistence across Agent lifetimes, unknown-policy
  click blocking under `allow_form_submit=False`, fetch_smart
  routing of `"pdf"` classification, documented shared-context
  regression test, and a C-1 regression test for unknown-policy
  blocking under `allow_form_submit=True`. Plus 2 new unit tests in
  `tests/test_v169_smart_binary_routing.py` for the post-v1.6.10
  enum values, and 4 stub updates in
  `tests/test_v162_routing.py` / `test_v163_routing.py` /
  `test_v165_medium.py` for the `classify_url` enum change.

**Review pass (3 fixes: 2 Critical + 1 Important)**
- **C-1**: `coordinate_click_unknown_policy="block"` was unreachable
  when `allow_form_submit=True` -- the unknown-policy check was
  nested inside the destructive-check guard. Fix: hoisted
  `_inspect_element_at_point`; destructive check and unknown-policy
  check now fire independently. Regression test added.
- **C-2**: `_is_binary_kind` (leading underscore) wasn't exported
  from the `web_agent.*` public namespace, breaking the CHANGELOG
  migration story. Fix: renamed to `is_binary_kind` and added to
  `web_agent.__all__`. `from web_agent import is_binary_kind` works.
- **I-1**: `web_research(extract_files=True)` silently appended
  contentless `Citation`s for unrecognized binaries (PPTX, ZIP,
  octet-stream) where the extractor returned
  `extraction_method="none"`. Fix: emit a `binary_not_extracted`
  warning + diagnostic and skip the result.

## What v1.6.9 added

Hardening patch -- no new features, ten safety + consistency fixes.

**P0 click_xy safety**
- `SafetyConfig.allow_coordinate_clicks: bool = True` (forced `False`
  by `safe_mode=True` via the existing master kill-switch pattern).
- `BrowserActions._do_click_xy` now runs
  `document.elementFromPoint(x, y)` (when `allow_form_submit=False`)
  to inspect the target stack and block submit / login / delete / pay
  controls.

**P0 remote_cdp ownership token**
- New `web_agent/ownership.py:OwnershipToken` writes
  `<profile_dir>/.webtool-ownership` on every isolated launch.
- `BrowserConfig.remote_cdp_ownership_token` +
  `remote_cdp_profile_dir` are **required** for `backend='remote_cdp'`.
- `BrowserManager.start()` verifies the token via
  `secrets.compare_digest` before opening `connect_over_cdp`.
- Sibling `remote_cdp` Agents read the token via
  `OwnershipToken.read(profile_dir)`.

**Named profile -> launch_persistent_context**
- `chromium.launch_persistent_context` returns a `BrowserContext`, not
  a `Browser`. All callers share the single persistent context via
  the new `_NoCloseContextProxy` (no-op `close()`); the persistent
  context is closed once from `BrowserManager.stop()`.
- Cookies / localStorage now actually persist across `Agent`
  lifetimes (integration test in `tests/test_agent.py::TestV169NamedProfilePersistence`).
- **Caveat (Playwright limitation): named profiles are NOT
  session-isolated.** Every `session_id` on a named-profile Agent shares
  the single persistent `BrowserContext` -- cookies, localStorage,
  IndexedDB, and cache are visible across sessions. Use
  `profile_mode="ephemeral"` when per-session isolation is required.

**Other**
- `BrowserConfig.disable_chromium_sandbox: Optional[bool] = None`
  auto-detects CI / container; local dev keeps the Chromium sandbox
  enabled.
- New `WebFetcher.fetch_smart` consolidates binary-vs-HTML routing;
  every Agent + Recipes call site now uses it.
- `mcp[cli]` moved to `[project.optional-dependencies] mcp`; install
  with `pip install "web-agent-toolkit[mcp]"`.
- `BrowserConfig.locale` / `timezone_id` / `user_agent_mode` /
  `user_agent` configurable (defaults preserve v1.6.8 behavior).
- `SkillsConfig.enabled` -> `project_skills_enabled` with deprecation
  alias.

## What v1.6.8 added

Diagnostics and Advanced Browser Intelligence: webTool becomes
explainable and debuggable. Six features, all **off by default**:

**Network event capture (Rank 7, P1)**
- New `web_agent/network_collector.py`: `NetworkCollector` attaches
  `page.on("request" / "response" / "requestfailed")` to every Page via
  `WeakKeyDictionary[Page, deque(maxlen=N)]`. Popups auto-attach through
  the existing `TabManager._on_new_page` hook.
- New `NetworkEvent` model. New fields on `FetchResult` and
  `ActionSequenceResult`: `network_events`, `api_candidates`,
  `download_candidates_runtime` / `download_candidates`.

**API endpoint discovery + download diagnostics**
- `NetworkCollector.api_candidates_for(page)`: dedupes XHR/fetch JSON
  response URLs.
- `page.on("download")` notification listener (independent of the
  downloader's `expect_download` consumer) records intents and calls
  `download.delete()` to avoid Chromium tmpfile pileup.

**Post-action screenshot verification**
- `BrowserActions._capture_verification_screenshot` writes
  `verify-<cid>-<index>.png` under `automation.screenshot_dir` after
  each successful action. Best-effort; never fails the sequence.
- New `ActionSequenceResult.verification_screenshots: list[str]`.

**Session replay / audit traces**
- New `web_agent/trace_recorder.py`: per-session JSONL action log
  with `{ts, ordinal, session_id, correlation_id, method, args,
  status, elapsed_ms, url}`. Distinct from the global `AuditLogger`.
- 3 new Agent methods: `replay_trace(file)`, `list_traces()`,
  `get_remote_cdp_url()`.
- New CLI subcommand: `web-agent replay <trace_file>`.

**Remote CDP backend (Rank 10, P2)**
- Third `BrowserConfig.backend` literal `"remote_cdp"` +
  `remote_cdp_url` field. `BrowserManager.start()` dispatches to
  `chromium.connect_over_cdp(remote_cdp_url)` instead of `launch()`.
  Config validator enforces loopback-only, `ws://`/`wss://`,
  rejects combinations with isolation_mode or cdp_enabled.
- `stop()` disconnects without killing the remote process.

**Tests:** 64 new across 6 files (`tests/test_v168_*.py`). Total
suite ~574, all green. No new core dependencies.

## What v1.6.7 added

Skills and Playbooks: webTool now accumulates reusable per-site
knowledge instead of rediscovering quirks each run. Five features:

**Domain skills (Ranks 1+2+3, P0)**
- New `web_agent/domain_skills.py`: `SkillRegistry` loads markdown
  files with YAML frontmatter from three tiers
  (project > workspace > builtin). `agent.list_domain_skills() /
  get_domain_skills(url) / apply_domain_skill(url, name, inputs)`.
- New core dep: `python-frontmatter>=1.0.0`.
- 3 bundled skills: `sec.gov/filing_search`,
  `github.com/release_download`, `ec.europa.eu/document_search`. Each
  ships with a Python runner; user markdown skills are info-only.

**Workspace (Rank 9, P2)**
- New `web_agent/workspace.py`: mode-gated read/write to
  `.webtool-workspace/`. Modes: `read_only` /
  `markdown_skills_only` (default) / `reviewed_python_helpers` /
  `unsafe_python_helpers`. Workspace skills under
  `domain-skills/` auto-load into the SkillRegistry.
- Default: `workspace.enabled=False`. Opt-in for safety.

**Interaction skill library (Rank 12, P2)**
- 8 new top-level Agent methods: `handle_dialog`, `select_dropdown`,
  `upload_file`, `drag_and_drop`, `scroll_until_text`,
  `click_inside_iframe`, `click_shadow_dom`, `print_page_as_pdf`.
- 4 new Action types: `UPLOAD_FILE`, `IFRAME_CLICK`,
  `SHADOW_DOM_CLICK`, `DRAG_AND_DROP`.
- Safety: `safety.allow_upload_outside_download_dir` (default
  False) gates `upload_file` paths to the download dir.

**Tests:** 51 new across 3 files (`tests/test_v167_*.py`). Total
suite ~505, all green.

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
