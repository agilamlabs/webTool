# Changelog

## [1.6.3] - 2026-05-07

### Follow-up to v1.6.2: 8-issue review pass

Tightens the smart-routing + structured-error work that landed in
v1.6.1/v1.6.2. Backward-compatible -- no breaking changes.

#### Smart routing now covers the direct-URL search path (#1)

`Agent.search_and_extract("https://x.com/report.pdf")` previously
called `WebFetcher.fetch` directly in the URL-as-query branch and
fell through to a NETWORK_ERROR for known download URLs. The branch
now runs the same classification + routing logic as
`fetch_and_extract`: known download extensions and HEAD-probed
extensionless documents go through `fetch_binary`; HTML stays on
the browser path.

#### Search results probed for extensionless binaries (#2)

In normal search mode, results were split into file_items / page_items
using URL-extension only. Extensionless PDFs from search results
(common with regulator dashboards: `/download?id=42`) silently fell
into HTML extraction and produced empty pages.

The split now uses a three-way classifier:
- Known download extension -> `binary` (immediate)
- Known HTML extension (`.html`, `.aspx`, `.php`, `.jsp`, `.asp`,
  `.htm`, `.xhtml`, `.shtml`, `.phtml`, `.cgi`) -> `html` (immediate)
- Otherwise -> `unknown` (HEAD-probed)

Unknown URLs are HEAD-probed in parallel via `asyncio.gather`, so
the latency cost is one round-trip total -- not one per result.
Probe failures default to HTML and the URL falls through to the
existing fetch path.

When `extract_files=True`, newly-detected extensionless binaries are
extracted inline; otherwise they land in `download_candidates`.

#### `classify_url` accepts `session_id` (#3)

`WebFetcher.classify_url(url, *, session_id=None)`: when supplied,
the HEAD probe inherits cookies from the Playwright session via the
existing `_cookies_for_session` path. Authenticated extensionless
document URLs (intranet downloads, regulator dashboards) now pass
the same auth gate as `fetch_binary`.

#### Structured warnings/errors recorded at the source (#4)

The hot path no longer relies on prefix-string classification to
derive structured codes after the fact. New internal `_MessageBag`
class with `.warn(code, message, url)` and `.err(code, message, url)`
methods that maintain both string and `ToolMessage` lists in lockstep,
populating `code` at the call site.

`AgentResult.structured_warnings[*].code` is now always one of:
`domain_blocked`, `fetch_failed`, `fetch_exception`, `download_skipped`,
`binary_extraction_failed`, `budget_exceeded`, `no_search_results`,
`no_allowed_pages`, `all_fetches_failed`. The legacy prefix
classifier (`_classify_message` / `_to_structured`) is retained as a
back-compat helper but is no longer used by the agent itself, so
unrecognized message text no longer leaks `code="unknown"` into
results.

#### User-extensible ranking profiles (#8)

New `AppConfig.ranking_profiles: dict[str, list[str]] = {}`. Combined
with the built-in `RANKING_PROFILES` at `Recipes.__init__` time;
user-defined profiles override built-ins on name collision so callers
can redefine, e.g., `"docs"` for an internal portal.

```python
config = AppConfig(ranking_profiles={
    "internal_kb": ["wiki.acme.io", "kb.acme.io"],
    "docs": ["my-docs.acme.io"],   # overrides built-in 'docs'
})
async with Agent(config) as agent:
    result = await agent.web_research(
        "rate-limiter design",
        domain_profile="internal_kb",
    )
```

`Recipes._resolve_hints` (instance method) consults the merged dict;
the legacy free function `_resolve_domain_hints` stays for back-compat
but only sees built-ins.

#### Stale doc cleanup (#5, #6, #7)

- README config table: `wait_until` default now correctly shows
  `domcontentloaded` (was `networkidle` -- doc lag from v1.6.2).
- README MCP section + `mcp_server.py` module docstring: 11 -> 12
  tools, with `web_fill_form_and_extract` listed.
- `content_extractor.py` top docstring updated: PDF/XLSX -> PDF/XLSX/
  DOCX/CSV.
- `pyproject.toml` `[binary]` extra comment updated to reflect DOCX
  inclusion.

### Test additions

- `tests/test_v163_routing.py`: 13 tests for `_url_ext_classification`,
  `classify_url(session_id=...)`, direct-URL routing, and search-result
  parallel probe behavior (probed / not probed / probe disabled /
  promotion to download_candidate).
- `tests/test_v163_messagebag_profiles.py`: 11 tests for `_MessageBag`
  semantics, no-`unknown`-leakage regression, and user-extensible
  ranking profiles (merging, override, unknown-profile silent ignore).

Total: 319/319 unit tests passing (v1.6.2 was 295; +24).

### Backward compatibility

- All new fields/params default to empty/None.
- `_classify_message` / `_to_structured` retained for external callers.
- `_resolve_domain_hints` (free function) retained, now only sees
  built-in profiles.
- `classify_url(url)` (positional only) still works -- the new
  `session_id` is keyword-only.
- Old JSON dumps still parse against the v1.6.3 model.

## [1.6.2] - 2026-05-07

### Follow-up to v1.6.1: 12-issue review pass

This release lands the full client review of v1.6.1 (12 issues across
routing, extraction, MCP surface, ranking, and observability).
Backward-compatible -- existing callers keep working unchanged.

#### Smart binary routing in `fetch_and_extract` (#1, #2, #10)

`Agent.fetch_and_extract(url)` now routes PDF/XLSX/DOCX/CSV URLs
through the binary extractor automatically:

1. URL extension matches a known download type → ``fetch_binary``.
2. Otherwise, with the new ``binary_probe`` flag (default True), send
   a HEAD request and inspect ``Content-Type`` / ``Content-Disposition``
   to detect *extensionless* document URLs (regulator dashboards
   often serve `/download?id=42` with `Content-Type: application/pdf`).
3. Otherwise → normal HTML fetch through the browser.

New `WebFetcher.classify_url(url) -> 'binary' | 'html' | 'unknown'`
exposed as the underlying primitive. Toggle the probe globally via
`SafetyConfig.probe_binary_urls = False`.

#### Streaming binary fetches with size cap (#3)

`fetch_binary` now uses `httpx.Client.stream("GET", ...)` and accumulates
chunks while enforcing `DownloadConfig.max_file_size_mb`. Aborts and
returns `HTTP_ERROR` with a clear message when the cap is hit, instead
of letting a rogue large response exhaust memory.

#### Browser session cookies reused for `fetch_binary` (#4)

When the caller threads `session_id=...` into `fetch_binary`,
authentication cookies set in the Playwright `BrowserContext`
(typically by an earlier `agent.interact(login_url, ...)` call) are
copied into the httpx request. Authenticated regulator dashboards now
work end-to-end without manual cookie handling.

#### MCP tool surface (#5)

- `web_search` exposes `extract_files`.
- `web_search_best` and `web_research` expose `prefer_domains` and
  `domain_profile`.
- `web_fetch` exposes `binary_probe`.
- New tool: `web_fill_form_and_extract` -- the v1.6.1 form-fill recipe
  is now reachable from MCP clients.

#### Ranking profiles (#8)

New `RANKING_PROFILES` dict (publicly importable) with five curated
profiles:

- `official_sources` -- regulators, central banks, multilaterals
- `docs` -- canonical software documentation hosts
- `research` -- arxiv, PubMed, ACM, IEEE, etc.
- `news` -- wire services + leading mastheads
- `files` -- common canonical-PDF / dataset hosts

New parameter `domain_profile: str | None` on
`Agent.search_and_open_best_result` and `Agent.web_research`. Combines
with caller-supplied `prefer_domains`; unknown profiles are silently
ignored.

#### Structured `ToolError` / `ToolWarning` (#12)

New `ToolMessage` model with `code` (snake_case identifier),
`message`, optional `url`, and `severity` (enum: INFO/WARNING/ERROR/FATAL).
`ToolWarning` and `ToolError` are aliases for `ToolMessage`.

Two new fields on `AgentResult` and `ResearchResult`:

- `structured_warnings: list[ToolMessage]` -- machine-readable
  counterpart to `warnings: list[str]`.
- `structured_errors: list[ToolMessage]` -- counterpart to `errors`.

The legacy string lists are still populated -- callers can adopt
structured forms incrementally. v1.7.0 will deprecate the strings.

#### CSV + DOCX extraction (#11)

`ContentExtractor` now dispatches to two more binary branches:

- CSV/TSV -> stdlib `csv` (no extra dependency). Auto-detects
  delimiter, falls back to UTF-8 BOM / latin-1.
- DOCX -> `python-docx` (added to the `[binary]` extra). Walks
  paragraphs and tables.

XLS (legacy) and PPTX deferred -- xlrd has CVEs, PPTX is a niche use.

#### Defaults + housekeeping (#6, #7, #9)

- CI matrix: added Python 3.13 alongside 3.10 and 3.12.
- `FetchConfig.wait_until` default flipped from `"networkidle"` to
  `"domcontentloaded"` -- robust against pages with analytics /
  long-polling that prevent networkidle from ever firing. The
  existing per-URL fallback to `"load"` remains for callers who
  override the default.
- `SearchResultItem` docstring updated: "A single Google search result"
  → "A single search result from any configured provider."

### Test additions

- `tests/test_v162_routing.py`: 13 tests for header-based binary
  detection, smart routing, streaming size cap, cookie sharing.
- `tests/test_v162_extraction.py`: ~10 tests for CSV (multiple
  delimiters, BOM, latin-1, empty) and DOCX (synthesized fixture +
  missing-library degrade).
- `tests/test_v162_models_and_profiles.py`: 14 tests for ranking
  profiles, structured ToolMessage round-trip, message classification.

Total: ~37 new tests, full unit suite 295/295 passing.

### Backward compatibility

- `errors`/`warnings` stay as `list[str]`. New `structured_errors`/
  `structured_warnings` are additive.
- Old JSON dumps still parse against the v1.6.2 model.
- `[binary]` stays optional -- without it, CSV still works (stdlib);
  DOCX/PDF/XLSX degrade with a clear install hint.
- `wait_until` default change is documented; callers that depended on
  `networkidle` can pin it via `AppConfig(fetch={"wait_until": "networkidle"})`.

## [1.6.1] - 2026-05-07

### Failure-surface hardening (7 client-suggested improvements)

This release sharpens what callers see when fetches partially fail,
expands the extraction pipeline to cover PDF/XLSX, and adds a
declarative form-fill recipe for dynamic calendar / filings pages.
All changes are backward-compatible: existing callers continue to
work; new behavior is opt-in via flags or new fields that default
to empty.

#### 1. Warnings split from fatal errors

`AgentResult` and `ResearchResult` previously mixed informational
issues (blocked domains, skipped file URLs) and fatal ones
(everything failed) in a single `errors: list[str]`. Now:

- `errors`: only fatal issues. If non-empty, treat the call as failed.
- `warnings`: non-fatal informational issues; the call still produced
  usable output.

Callers checking `if not result.errors:` for "did this succeed" will
now correctly succeed in cases where one URL of five was blocked.

#### 2. Structured download candidates

When `search_and_extract` encounters PDF/XLSX/etc. URLs it used to
emit a string error per URL ("File URL skipped..."). Now those URLs
land in `AgentResult.download_candidates: list[SearchResultItem]` —
fully structured (title, snippet, position, provider) so callers can
programmatically retry via `agent.download(url)`.

#### 3. Built-in PDF / XLSX extraction

New optional dependency group `[binary]` (pypdf + openpyxl).

- `Agent.search_and_extract(query, extract_files=True)` now routes
  PDF/XLSX results through a binary fetch path (httpx) and extracts
  text inline into `pages` instead of skipping them.
- `ContentExtractor.extract` dispatches on `FetchResult.binary` and
  `content_type` to the right branch:
  - PDF → `pypdf.PdfReader`, with encrypted-PDF detection.
  - XLSX → `openpyxl`, dumped TSV-style per sheet.
- Library-missing path returns
  `ExtractionResult(extraction_method="none")` with a clear
  `pip install 'web-agent-toolkit[binary]'` log warning — never
  crashes the pipeline.

New `WebFetcher.fetch_binary(url, session_id=None)` wraps the binary
GET with the same domain / robots / rate-limit gates as `fetch`, and
re-validates `final_url` after redirects.

#### 4. Caller-provided domain hints (`prefer_domains`)

`Recipes._rank` and the public `Agent.search_and_open_best_result` /
`Agent.web_research` now accept `prefer_domains: list[str]`. Any
result whose host matches a hint (exact or as a parent suffix) gets a
+0.40 ranking bonus — large enough to dominate the well-known-domain
heuristic. Use it when you know the regulator / vendor / source you
expect (e.g. `["ec.europa.eu", "esma.europa.eu"]`).

#### 5. Search-engine SERP URLs unwrap to queries

Calling `search_and_extract("https://www.google.com/search?q=tesla+10k")`
used to fetch the SERP HTML and try to extract content from it
(useless and triggers anti-bot). Now SERP URLs from Google / Bing /
DuckDuckGo / Brave / SearX / SearXNG are unwrapped to their `?q=`
parameter and the toolkit runs its own search instead. Plain URLs
(`https://example.com/page`) still go through the URL-as-query
fast-path.

#### 6. Per-URL fetch diagnostics

New `FetchDiagnostic` model with:

- `url`, `final_url`, `status`, `status_code`
- `provider`: which search backend surfaced the URL (`searxng` /
  `ddgs` / `playwright` / `direct` / `unknown`)
- `block_reason`: `domain_blocked` | `robots_disallowed` |
  `timeout` | `http_error` | `network_error` | `download_skipped` | None
- `content_length`, `response_time_ms`, `from_cache`

`AgentResult.diagnostics` and `ResearchResult.diagnostics` carry one
entry per URL the pipeline considered, in order. Lets callers
programmatically inspect *why* each URL succeeded or failed without
parsing free-form error strings.

`SearchResultItem.provider` (new field) is populated by every search
provider and threaded into diagnostics.

#### 7. Form-fill recipe for dynamic calendar / filings pages

New `FormFilterSpec` model — declarative spec for a search/filter
form (query selector + value, ordered filters, submit, wait_for).

New `Agent.fill_form_and_extract(url, spec, *, session_id=None)`:

1. Open `url`.
2. Fill `spec.query_selector` with `spec.query_value`.
3. Apply each `(locator, value)` in `spec.filters` (auto-detecting
   `<select>` vs input).
4. Click `spec.submit_selector` (or press Enter on the query input).
5. Wait for `spec.wait_for` (or `networkidle`).
6. Run `ContentExtractor.extract` on the post-submit HTML.

Result-based: returns `ExtractionResult(extraction_method="none")`
on timeout / locator-not-found rather than raising.

### Other

- `FetchResult` gained `binary: bytes | None` and `content_type: str | None`
  fields for the binary fetch path; `html` and `binary` are mutually
  exclusive on a given result.
- `AGENTS.md` (new): project-level guide for AI coding agents
  (Codex, Claude Code, Cursor, OpenClaw). Documents setup, lay-of-the-
  land, conventions, and the "how to add a feature" loop.

### Test additions

- `tests/test_v161_models.py`: warnings/download_candidates/diagnostics defaults.
- `tests/test_search_url_unwrap.py`: SERP URL detection + unwrapping.
- `tests/test_prefer_domains.py`: ranking bonus.
- `tests/test_binary_extraction.py`: PDF + XLSX paths, missing-library degrade.
- `tests/test_form_recipe_spec.py`: `FormFilterSpec` validation.

### Backward compatibility

- `errors` field shape unchanged; only its semantics tightened (now
  contains *only* fatal issues). Code checking `if not result.errors:`
  may now succeed where it previously returned True with non-fatal
  noise — this is the intended improvement.
- All new model fields default to empty/None; old JSON dumps still
  parse.
- `[binary]` extra is genuinely optional. Without it, callers that
  don't pass `extract_files=True` see no behavior change at all.

## [1.6.0] - 2026-04-30

### Production-safety hardening (the last two gaps before "ready for use")

#### BrowserActions URL safety (4 new gates)

Browser-automation calls were the only network-touching subsystem
that didn't fully validate URL state. WebFetcher and Downloader had
been hardened in v1.2 (pre-check + post-redirect re-check); this
release closes the same loop on BrowserActions.

1. **`_do_navigate` GOTO pre-check**: ``NavigateInput.url`` is now
   validated against the safety policy *before* ``page.goto`` is
   called. Without this, an LLM-supplied automation script could
   navigate the headless browser to AWS IMDS / RFC1918 / a denied
   host, bypassing the policy that gates fetch and download.
2. **`_do_navigate` post-redirect re-check**: every nav direction
   (GOTO / BACK / FORWARD / RELOAD) re-validates ``page.url``
   afterwards. A whitelisted host can no longer redirect us to a
   private IP via 30x.
3. **`execute_sequence` initial-goto re-check**: the entry-URL goto
   inside ``execute_sequence`` now re-checks ``page.url`` after
   landing. Catches redirects on the very first navigation.
4. **`execute_sequence` per-action drift detection**: after every
   action in the sequence, ``page.url`` is checked. If a click,
   form-submit, or JS-driven nav lands on a disallowed domain or
   private IP, the offending action is downgraded to FAILED, all
   remaining actions are SKIPPED, and the sequence aborts
   regardless of ``stop_on_error``.
5. **`take_screenshot` post-redirect re-check**: ``page.goto`` inside
   the screenshot capture now re-validates ``page.url`` before the
   image is written. Prevents leaking a screenshot of a private-
   network page reached via redirect.

#### Model addition

- ``ScreenshotResult.error_message: str | None`` -- new field. Used
  by the post-redirect-blocked path to surface the failure reason
  (previously a blocked screenshot returned ``status=FAILED`` with
  no explanation).

#### Integration CI lane

The existing lint + unit-test workflow gates every PR. A new
**integration** job now runs the full Playwright + live-network
suite (``tests/test_browser_actions.py`` + ``tests/test_agent.py``,
21 tests, ~2 minutes) on:

- Push to ``main`` (post-merge sanity check)
- Nightly schedule at 07:00 UTC (catches drift in upstream search
  engines, CAPTCHA behavior, Playwright browser releases)
- Manual ``workflow_dispatch``

The integration job is **not** wired to PRs because search engines
can serve CAPTCHAs from CI's IP ranges, and that flakiness shouldn't
block legitimate merges. ``continue-on-error: true`` keeps a
flaky-network run from failing the overall workflow -- the job's
value is signal, not gating. On failure, ``debug/`` and
``screenshots/`` are uploaded as a 3-day artifact for diagnosis.

The job uses ``playwright install --with-deps chromium`` so the
runner picks up Chromium + system libs in one step.

### Tests

- ``tests/test_browser_url_safety.py`` (new): 9 mock-based unit tests
  covering the 5 new gates above. No browser launch -- they mock
  ``Page.url`` and ``Page.goto`` to exercise pre-check, post-
  redirect re-check, and per-action drift paths in isolation.

Test count: 193 -> 202 unit (+9). 214 -> 223 total (192 unit +
21 integration before; 202 unit + 21 integration now).

## [1.5.2] - 2026-04-30

### Hygiene cleanup (10 findings from full-project review)

Bug fix:

- ``SearchEngine.search`` cache hits now reset ``searched_at`` to
  ``datetime.now(timezone.utc)`` instead of returning the original
  search timestamp. Previously, a 1-hour-old cache hit would surface
  a stale ``searched_at`` that misled callers into thinking the data
  was fresh; now ``from_cache=True`` is the source of truth and
  ``searched_at`` reflects when the cached payload was returned.
  ``WebFetcher`` already did the right thing.

Dead code:

- Removed unused ``RetryableHTTPError`` class from
  ``web_agent/utils.py`` -- never raised anywhere; the 5xx path in
  ``web_fetcher.py`` raises a plain ``Exception`` and lets
  ``async_retry`` handle it.
- Removed two unused fixtures (``minimal_html``, ``empty_html``)
  from ``tests/conftest.py``.

Documentation drift:

- ``SearchError`` docstring no longer says "Google and DuckDuckGo
  both failed" -- now correctly describes the v1.4 multi-provider
  chain. Same fix in ``Agent.search_and_extract`` docstring (3 spots),
  ``mcp_server.py:web_search`` docstring, and the README's strict-mode
  example.
- README "Project Structure" section was missing 5 modules added in
  v1.3-1.5: ``rate_limiter.py``, ``robots.py``, ``audit.py``,
  ``cache.py``, ``search_providers.py``. Added with version-stamp
  one-liners.
- README test count refreshed: 111 -> 213, 90 unit -> 192 unit. The
  unit-test command was rewritten to use ``--ignore`` instead of an
  explicit (and incomplete) module list.
- README export count refreshed: 49 -> 60 names; version stamp
  ``v1.1.0`` -> ``v1.5.2``.
- README configuration table + ``config.example.yaml`` now document
  ``browser.slow_mo`` (was implemented but undocumented).

Repo hygiene:

- ``.gitignore`` now also ignores ``cache/`` and ``audit.jsonl``
  (the default runtime output paths for v1.3 audit + v1.5 cache).
- ``search_providers.py`` mixed ``Optional[X]`` and ``X | None`` in
  the same file -- normalized to ``X | None`` (consistent with
  Python 3.10+ idiom and the rest of the file's style).

Comment fix:

- ``WebFetcher.fetch`` cache-lookup comment was already corrected in
  v1.5.1; no change here.

Tests:

- ``test_cache.py`` extended with ``test_fetch_caches_result_and_serves_from_cache``
  -- covers the ``FetchResult.from_cache`` field that previously had
  no direct test (only the ``SearchResponse.from_cache`` counterpart
  was tested).

Test count: 192 -> 193 unit (+1). 213 -> 214 total.

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
