# webTool

Professional agentic web search, fetch, download, extraction, and browser automation toolkit built on Playwright's headless Chromium.

Designed as a tool for AI agents that need to search the web, fetch JavaScript-heavy pages, extract structured content, download files, and automate browser interactions -- all through a clean async Python API.

## Features

- **Web Search** -- Free-first provider chain: SearXNG -> DDGS -> Playwright fallback. URL-as-query short-circuits to direct fetch.
- **Page Fetching** -- Renders JavaScript, retries with exponential backoff, detects download URLs. Optional disk cache (TTL-based)
- **Content Extraction** -- Three-tier fallback: trafilatura (F1 ~0.958) -> BeautifulSoup4 -> raw text. Markdown rendering populated automatically when trafilatura wins
- **File Download** -- Three strategies: httpx streaming -> Playwright page save -> Playwright JS download
- **Browser Automation** -- 12 action types composable into scripted sequences
- **Semantic Locators** -- Find elements by ARIA role, label, text, or test_id (not just CSS)
- **Browser Sessions** -- Persistent named contexts retain cookies/login across multi-call workflows
- **High-Level Recipes** -- `search_and_open_best_result`, `find_and_download_file`, `web_research`
- **Safety Controls** -- Domain allow/deny lists, granular `allow_*` flags, SSRF protection, per-call budgets
- **Politeness Layer** -- Per-host rate limiter + robots.txt obedience (both opt-out)
- **Audit Log** -- Append-only JSONL of every Agent operation (off by default)
- **Disk Cache** -- TTL cache for fetch + search results, with LRU-by-mtime eviction (off by default)
- **Retry Profiles** -- Declarative `fast`/`balanced`/`paranoid` retry policies
- **Debug Mode** -- Auto-capture HTML/screenshot/error JSON on failures
- **Correlation IDs** -- Trace single requests across all subsystems via auto-injected log fields
- **Screenshots** -- Viewport, full-page, or element-specific captures
- **Anti-Detection** -- playwright-stealth, user-agent rotation, resource blocking
- **Structured Output** -- All results are Pydantic v2 models serializable to JSON
- **MCP Server** -- 11 tools exposed to Claude Desktop, Claude Code, Cursor, and other MCP-compatible AI clients

## Installation

```bash
pip install -e ".[dev]"
playwright install chromium
```

On Linux, use `--with-deps` to auto-install Chromium's system dependencies:

```bash
playwright install --with-deps chromium
```

**Requirements:** Python 3.10+

## Quick Start

### Python API (recommended for AI agents)

```python
import asyncio
from web_agent import Agent

async def main():
    async with Agent() as agent:
        # Search the web and extract content from top results
        result = await agent.search_and_extract("Python web scraping", max_results=5)
        for page in result.pages:
            print(f"{page.title}: {page.content_length} chars")

        # Fetch and extract a single page
        page = await agent.fetch_and_extract("https://example.com")
        print(page.content[:200])

        # Download a file
        dl = await agent.download("https://example.com/report.pdf")
        print(f"Saved to {dl.filepath} ({dl.size_bytes} bytes)")

        # Take a screenshot
        ss = await agent.screenshot("https://example.com", full_page=True)
        print(f"Screenshot: {ss.path}")

asyncio.run(main())
```

### CLI

```bash
# Search and extract
python -m web_agent search "latest AI research papers" --max-results 5

# Fetch a single page
python -m web_agent fetch "https://example.com"

# Download a file
python -m web_agent download "https://www.sec.gov/Archives/edgar/data/1652044/000165204425000014/goog-20241231.htm"

# Take a screenshot
python -m web_agent screenshot "https://example.com" --full-page

# Run a browser automation sequence
python -m web_agent interact "https://example.com" --actions actions.json
```

## Configuration

### Programmatic (recommended)

No config file needed. All settings have sensible defaults:

```python
from web_agent import Agent, AppConfig

# All defaults:
async with Agent() as agent:
    ...

# Custom config:
config = AppConfig(
    browser={"headless": False, "max_contexts": 5},
    search={"max_results": 20, "language": "en"},
    fetch={"max_retries": 5},
    log_level="DEBUG",
    output_dir="/tmp/results",
)
async with Agent(config) as agent:
    ...
```

### Environment Variables

All settings support env vars with the `WEB_AGENT_` prefix:

```bash
export WEB_AGENT_LOG_LEVEL=DEBUG
export WEB_AGENT_BROWSER__HEADLESS=false
export WEB_AGENT_SEARCH__MAX_RESULTS=20
export WEB_AGENT_FETCH__MAX_RETRIES=5
```

### YAML File

```bash
python -m web_agent --config config.yaml search "query"
```

See [config.example.yaml](config.example.yaml) for all available options.

### Configuration Reference

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `browser` | `headless` | `true` | Run browser without visible window |
| `browser` | `max_contexts` | `3` | Max concurrent browser contexts |
| `browser` | `default_timeout` | `30000` | Default action timeout (ms) |
| `browser` | `navigation_timeout` | `45000` | Page navigation timeout (ms) |
| `browser` | `block_resources` | `["image","font","stylesheet","media"]` | Resource types to block for speed |
| `browser` | `viewport_width` | `1920` | Browser viewport width |
| `browser` | `viewport_height` | `1080` | Browser viewport height |
| `search` | `max_results` | `10` | Number of search results to return |
| `search` | `language` | `"en"` | Search language |
| `search` | `region` | `"us"` | Search region |
| `fetch` | `wait_until` | `"networkidle"` | Wait condition (`networkidle`, `load`, `domcontentloaded`) |
| `fetch` | `retry_policy` | `"balanced"` | Profile: `fast` / `balanced` / `paranoid` |
| `fetch` | `max_retries` | `3` | Retry count (overrides policy when set) |
| `fetch` | `retry_base_delay` | `1.0` | Base delay (seconds) for exponential backoff |
| `download` | `download_dir` | `"./downloads"` | Directory for downloaded files |
| `download` | `max_file_size_mb` | `100` | Maximum file size limit |
| `extraction` | `favor_recall` | `true` | Prefer extracting more content vs precision |
| `extraction` | `include_tables` | `true` | Include table data in extraction |
| `extraction` | `min_content_length` | `50` | Minimum characters to accept extraction |
| `automation` | `default_action_timeout` | `10000` | Timeout per browser action (ms) |
| `automation` | `screenshot_dir` | `"./screenshots"` | Directory for screenshots |
| `automation` | `stop_on_error` | `true` | Halt action sequence on first failure |
| `safety` | `allowed_domains` | `[]` | Suffix-match allow-list (empty = allow all) |
| `safety` | `denied_domains` | `[]` | Suffix-match deny-list (takes precedence) |
| `safety` | `safe_mode` | `false` | Block downloads, JS eval, submit clicks |
| `safety` | `max_pages_per_call` | `50` | Pages-per-call budget |
| `safety` | `max_chars_per_call` | `1000000` | Chars-per-call extraction budget |
| `safety` | `max_time_per_call_seconds` | `300` | Wall-clock budget per Agent call |
| `debug` | `enabled` | `false` | Auto-capture HTML/screenshot/JSON on failures |
| `debug` | `debug_dir` | `"./debug"` | Directory for failure artifacts |
| `debug` | `capture_html` | `true` | Save HTML snapshot on failure |
| `debug` | `capture_screenshot` | `true` | Save PNG snapshot on failure |

## API Reference

### Agent Methods

```python
class Agent:
    async def search_and_extract(query: str, max_results: int = None) -> AgentResult
    async def fetch_and_extract(url: str) -> ExtractionResult
    async def download(url: str, filename: str = None) -> DownloadResult
    async def screenshot(url: str, path: str = None, full_page: bool = False) -> ScreenshotResult
    async def interact(url: str, actions: list[Action], stop_on_error: bool = None) -> ActionSequenceResult
    async def save_results(result: AgentResult, output_path: str = None) -> Path
```

### Result Models

**AgentResult** (from `search_and_extract`):

```python
result.query           # str - original search query
result.search          # SearchResponse - search results with titles, URLs, snippets
result.pages           # list[ExtractionResult] - extracted page content
result.errors          # list[str] - any errors encountered
result.total_time_ms   # float - pipeline execution time
```

**ExtractionResult** (from `fetch_and_extract`):

```python
result.url               # str
result.title             # str | None
result.description       # str | None
result.author            # str | None
result.date              # str | None
result.sitename          # str | None
result.content           # str | None - main text content
result.language          # str | None
result.extraction_method # "trafilatura" | "bs4" | "raw" | "none"
result.content_length    # int
```

**DownloadResult** (from `download`):

```python
result.url          # str
result.filepath     # str - local file path
result.filename     # str
result.size_bytes   # int
result.content_type # str | None
result.status       # "success" | "timeout" | "http_error" | "network_error"
```

**ScreenshotResult** (from `screenshot`):

```python
result.url        # str
result.path       # str - file path
result.format     # "png" | "jpeg"
result.size_bytes # int
result.status     # "success" | "failed"
```

### Browser Automation Actions

12 action types composable into sequences:

| Action | Description | Key Fields |
|--------|-------------|------------|
| `ClickInput` | Click an element | `selector`, `button`, `double_click`, `modifiers` |
| `TypeInput` | Type text keystroke-by-keystroke | `selector`, `text`, `delay`, `clear_first` |
| `FillInput` | Set input value instantly | `selector`, `value` |
| `ScrollInput` | Scroll page or element | `direction`, `amount`, `infinite_scroll` |
| `ScreenshotInput` | Capture screenshot | `full_page`, `format`, `quality`, `path` |
| `NavigateInput` | Navigate to URL / back / forward | `url`, `navigate_action`, `wait_until` |
| `DialogInput` | Handle browser dialogs | `dialog_action` (accept/dismiss), `prompt_text` |
| `HoverInput` | Hover over element | `selector` |
| `SelectInput` | Select dropdown option | `selector`, `value` / `label` / `index` |
| `KeyboardInput` | Press keys or combos | `key` (e.g. `"Enter"`, `"Control+A"`), `repeat` |
| `WaitInput` | Wait for condition | `target` (selector/text/url/network_idle), `value` |
| `EvaluateInput` | Run JavaScript | `expression` |

#### Example: Form Automation

```python
from web_agent import Agent
from web_agent.models import (
    ClickInput, FillInput, WaitInput, ScreenshotInput, WaitTarget
)

async with Agent() as agent:
    result = await agent.interact("https://httpbin.org/forms/post", [
        WaitInput(target=WaitTarget.SELECTOR, value="input[name='custname']"),
        FillInput(selector="input[name='custname']", value="John Doe"),
        FillInput(selector="input[name='custemail']", value="john@example.com"),
        ClickInput(selector="button[type='submit']"),
        ScreenshotInput(full_page=True),
    ])
    print(f"Actions: {result.actions_succeeded}/{result.actions_total} succeeded")
```

#### Example: Action Sequence from JSON

Create `actions.json`:

```json
[
  {"action": "wait", "target": "selector", "value": "h1"},
  {"action": "evaluate", "expression": "document.title"},
  {"action": "screenshot", "full_page": true},
  {"action": "scroll", "direction": "down", "amount": 5},
  {"action": "evaluate", "expression": "window.scrollY"}
]
```

Run it:

```bash
python -m web_agent interact "https://example.com" --actions actions.json
```

## Architecture

```
Agent (orchestrator)
  |
  |-- SearchEngine          Chain: SearXNG -> DDGS -> Playwright
  |     |-- SearXNGProvider     httpx + self-hosted SearXNG JSON API
  |     |-- DDGSProvider        ddgs package (no browser needed)
  |     `-- PlaywrightProvider  Browser-driven Google + DDG HTML
  |
  |-- WebFetcher            Playwright page rendering + retry
  |-- ContentExtractor      trafilatura -> BS4 -> raw text
  |-- Downloader            httpx -> Playwright page save -> Playwright download
  |-- BrowserActions        12 action handlers with dispatch table
  |
  `-- BrowserManager        Chromium lifecycle, stealth, semaphore pool
```

**Smart routing:**

- **URL-as-query**: `agent.search_and_extract("https://example.com")` skips search entirely and fetches the URL directly
- **Search chain**: SearXNG (skipped silently if `searxng_base_url` not set) -> DDGS (skipped silently if `ddgs` not installed) -> Playwright (always available, slow fallback)
- File URLs (`.pdf`, `.xlsx`, `.zip`) are detected upfront and routed to the downloader
- Web page URLs (`.html`, `.htm`) use Playwright page save instead of download events
- `networkidle` timeouts automatically fall back to `load` wait state
- HTTP 4xx errors fail immediately; 5xx errors retry with exponential backoff

### Configuring the search chain

```python
from web_agent import Agent, AppConfig

# Use a self-hosted SearXNG as the primary source
config = AppConfig(search={
    "providers": ["searxng", "ddgs", "playwright"],
    "searxng_base_url": "http://localhost:8888",
    "searxng_timeout": 10.0,
})

# Or skip browser-based search entirely (faster, but no fallback if APIs fail)
config = AppConfig(search={"providers": ["searxng", "ddgs"]})

# Or only use the legacy Playwright path (1.3.0 behavior)
config = AppConfig(search={"providers": ["playwright"]})

async with Agent(config) as agent:
    result = await agent.search_and_extract("python web scraping")
```

The chain falls through on each provider's empty result or transient error. Pass `strict=True` to raise `SearchError` when the entire chain exhausts:

```python
result = await agent.search_and_extract("query", strict=True)
# raises SearchError if SearXNG, DDGS, and Playwright all fail
```

### Self-hosting SearXNG (recommended for the primary tier)

[SearXNG](https://github.com/searxng/searxng) is a privacy-respecting metasearch engine that aggregates ~80 search backends (Google, Bing, DuckDuckGo, Wikipedia, GitHub, arXiv, etc.) without tracking. Self-hosting gives you the fastest tier of the chain (no browser launch, no third-party rate limit) at the cost of running one container.

The repo ships a tuned config under [`docker/searxng/`](docker/searxng/):

- [`docker-compose.yml`](docker/searxng/docker-compose.yml) — pinned to the official `searxng/searxng:latest` image, bound to `127.0.0.1:8888` only.
- [`settings.yml`](docker/searxng/settings.yml) — JSON output enabled (required by web_agent), default engines, internal limiter disabled (web_agent rate-limits on its own).

Three steps from a fresh clone:

```bash
# 1. Generate a per-deployment secret key:
python -c "import secrets; print(secrets.token_hex(32))"
# ... paste the output into docker/searxng/settings.yml in place of
# the REPLACE_WITH_RANDOM_STRING placeholder.

# 2. Start the container:
docker compose -f docker/searxng/docker-compose.yml up -d

# 3. Smoke-test the JSON endpoint web_agent uses:
curl 'http://localhost:8888/search?q=python&format=json' | head -c 500
```

Then point web_agent at it:

```python
from web_agent import Agent, AppConfig

config = AppConfig(search={
    "providers": ["searxng", "ddgs", "playwright"],
    "searxng_base_url": "http://localhost:8888",
})
async with Agent(config) as agent:
    result = await agent.search_and_extract("python web scraping")
    # First provider in the chain (SearXNG) handles it -- no browser
    # launch, no DDGS network, sub-second latency.
```

To stop and clean up:

```bash
docker compose -f docker/searxng/docker-compose.yml down
```

**Production / shared use**: re-enable the SearXNG-level rate limiter (`server.limiter: true` in `settings.yml`), put it behind a reverse proxy with auth + TLS, and consider a Redis backend (see the [SearXNG admin docs](https://docs.searxng.org/admin/settings/settings_server.html)).

## Browser Sessions

Persistent browser sessions retain cookies, localStorage, and origin tokens across multiple Agent calls -- ideal for login flows, multi-step workflows, or any task that needs continuity.

```python
async with Agent() as agent:
    # Step 1: log in once
    sid = await agent.create_session(name="my-app")
    await agent.interact("https://app.example.com/login", [
        FillInput(selector="#user", value="me"),
        FillInput(selector="#pass", value="secret"),
        ClickInput(selector="button[type=submit]"),
    ], session_id=sid)

    # Step 2: subsequent calls reuse cookies
    dashboard = await agent.fetch_and_extract(
        "https://app.example.com/dashboard", session_id=sid
    )
    report = await agent.download(
        "https://app.example.com/reports/q4.pdf", session_id=sid
    )

    await agent.close_session(sid)
```

In MCP, the equivalent flow is `create_browser_session` -> `web_interact(...session_id=sid)` -> `web_fetch(...session_id=sid)` -> `close_browser_session`.

## Safety Controls

`SafetyConfig` provides multiple defense layers: domain allow/deny lists, granular feature flags, SSRF protection, and per-call budgets. Most defaults are secure-out-of-the-box (`block_private_ips=True`, `allow_js_evaluation=False`).

```python
from web_agent import Agent, AppConfig

config = AppConfig(safety={
    "allowed_domains": ["wikipedia.org", "arxiv.org"],
    "denied_domains": ["malicious.example.com"],
    "safe_mode": False,                   # master kill-switch
    "allow_js_evaluation": False,         # gates EvaluateInput (default False)
    "allow_downloads": True,              # gates file downloads
    "allow_form_submit": True,            # gates submit-button clicks
    "block_private_ips": True,            # SSRF protection (RFC1918 + IMDS)
    "max_pages_per_call": 10,
    "max_chars_per_call": 500_000,
    "max_time_per_call_seconds": 60.0,
})
async with Agent(config) as agent:
    ...
```

| Field | Default | Effect |
|---|---|---|
| `allowed_domains` | `[]` (allow all) | Suffix-match patterns; empty allows everything |
| `denied_domains` | `[]` | Always blocked, takes precedence over allow-list |
| `safe_mode` | `false` | Master kill-switch: forces all 3 `allow_*` to False |
| `allow_js_evaluation` | **`false`** | Gates `EvaluateInput` (LLM-supplied JS). Opt in explicitly. |
| `allow_downloads` | `true` | Gates file downloads via `agent.download()` |
| `allow_form_submit` | `true` | Gates clicks on submit-typed buttons (heuristic match) |
| `block_private_ips` | `true` | Blocks RFC1918, loopback, link-local (incl. AWS IMDS at 169.254.169.254) |
| `max_pages_per_call` | `50` | Stops fetching after N pages |
| `max_chars_per_call` | `1_000_000` | Stops extracting after total chars exceeded |
| `max_time_per_call_seconds` | `300` | Wall-clock cutoff per Agent call |
| `rate_limit_per_host_rps` | `2.0` | Per-host requests/second cap. Set to `0` to disable. |
| `respect_robots_txt` | `true` | Fetch and obey each host's robots.txt before requesting pages |
| `robots_user_agent` | `"web-agent-toolkit"` | UA token sent to robots.txt and matched against rule groups |

**Path traversal protection**: `Downloader.download(filename=...)` and `ScreenshotInput.path` reject `..` traversal and absolute paths. Filenames must resolve inside the configured `download_dir` / `screenshot_dir`.

**SSRF protection**: When `block_private_ips=True` (default), the toolkit blocks fetches/downloads to RFC1918 ranges, loopback, and link-local addresses. The `Downloader` re-validates every HTTP redirect target so a whitelisted host cannot bounce you to AWS IMDS or an internal-only URL.

**Politeness layer**: `RateLimiter` enforces a per-host minimum interval between requests so parallel fetches against a single host don't trip server-side throttles. `RobotsChecker` fetches each host's `robots.txt` once, caches it for an hour, and short-circuits any URL the rules disallow for our user-agent. Disallowed URLs return `FetchStatus.BLOCKED` with `error_message="robots.txt for {host} disallows ..."`.

Blocked URLs return `FetchStatus.BLOCKED` with a clear `error_message`. Budget exhaustion raises `BudgetExceededError` (caught and added to `errors[]` in `AgentResult`).

### Audit Log

`AuditConfig` enables an append-only JSONL log of every public Agent operation. Distinct from regular logging: structured (one JSON object per line), records only public method calls (start + end), and survives restarts.

```python
from web_agent import AppConfig, Agent

config = AppConfig(audit={"enabled": True, "audit_log_path": "./audit.jsonl"})
async with Agent(config) as agent:
    await agent.fetch_and_extract("https://example.com")
# audit.jsonl now contains one line:
# {"timestamp": "...", "correlation_id": "...", "method": "fetch_and_extract",
#  "args": {"url": "https://example.com"}, "status": "success", "elapsed_ms": 432.1}
```

Failures are logged with `"status": "error"` and `"error": "<repr(exc)>"`. The `correlation_id` field cross-references the entry with regular loguru logs.

### Cache (NEW in 1.5.0)

`CacheConfig` enables a disk-backed TTL cache for fetch results and search responses. Disabled by default. When enabled, every successful `agent.fetch_and_extract(url)` and `agent.search_and_extract(query)` writes its result to disk; subsequent calls within `ttl_seconds` short-circuit and return the cached payload (`from_cache=True`) without hitting the network.

```python
from web_agent import Agent, AppConfig

config = AppConfig(cache={
    "enabled": True,
    "cache_dir": "./cache",
    "ttl_seconds": 3600,    # 1 hour
    "max_cache_mb": 100,    # LRU-by-mtime eviction past this
})
async with Agent(config) as agent:
    page1 = await agent.fetch_and_extract("https://example.com")  # network
    page2 = await agent.fetch_and_extract("https://example.com")  # cache hit
    # page2.url == page1.url and the underlying FetchResult.from_cache == True
```

Cache keys: URL for fetches, `"search:<query>:<max_results>"` for searches. Only successful fetches and non-empty search responses are cached -- caching errors/empty would lock in transient failures across the TTL window.

Ordering: `robots.txt` check runs **before** the cache lookup so a host changing its `robots.txt` to disallow a path takes effect immediately, even for URLs we cached under more permissive rules. The robots check itself is cached per-host (1h TTL inside `RobotsChecker`), so the practical overhead on cache hits is near-zero. Cache hit -> skip rate-limit + network.

To clear the cache mid-run: `await agent._cache.clear()` (returns count removed). To extend the cache backend (e.g. Redis), implement the `Cache` ABC in `web_agent/cache.py` and pass it directly to `WebFetcher` / `SearchEngine`.

### Markdown Extraction (NEW in 1.5.0)

`ExtractionResult.markdown` is a markdown rendering of the page, populated automatically whenever `trafilatura` is the winning extractor (the most common path). Markdown preserves headings, lists, links, and emphasis, which most LLMs prefer to consume over plain text.

```python
async with Agent() as agent:
    page = await agent.fetch_and_extract("https://example.com")
    print(page.content)    # plain text, like before
    print(page.markdown)   # markdown rendering -- preserves structure
```

`markdown` stays `None` when the bs4 or raw-text fallback layers win (those layers don't have a markdown equivalent). The double-pass through trafilatura is cheap -- HTML is parsed twice, no extra network.

### Strict Mode

By default, all `Agent` methods return result models even on failure. Pass `strict=True` to convert failures into typed exceptions:

```python
from web_agent.exceptions import NavigationError, SearchError, DownloadError

async with Agent() as agent:
    try:
        page = await agent.fetch_and_extract(url, strict=True)
    except NavigationError as e:
        print(f"Failed: {e} (status={e.status_code})")

    try:
        result = await agent.search_and_extract(query, strict=True)
    except SearchError as e:
        print(f"Both Google and DuckDuckGo failed: {e}")
```

## High-Level Recipes

Three composite workflows AI agents can call directly without orchestrating primitives:

```python
# Recipe 1: search + rank + fetch top hit
result = await agent.search_and_open_best_result("FastAPI tutorial 2024")
print(result.title, result.content[:500])

# Recipe 2: search + locate file + download
dl = await agent.find_and_download_file(
    "Tesla 10-K annual report 2024", file_types=["pdf"]
)
print(f"Saved: {dl.filepath} ({dl.size_bytes} bytes)")

# Recipe 3: multi-page research with citations
research = await agent.web_research("vector databases comparison", max_pages=5)
for c in research.citations:
    print(f"[{c.relevance_score:.2f}] {c.title} -- {c.url}")
```

`search_and_open_best_result` ranking schemes:
- `default` -- query overlap + HTTPS bonus + well-known domain bonus + position tiebreaker
- `overlap` -- pure token overlap
- `position` -- inverse search engine rank

## Semantic Locators

Beyond CSS selectors, browser automation actions accept `LocatorSpec` for AI-friendly element targeting:

```python
from web_agent.models import ClickInput, FillInput, LocatorSpec

# All three are equivalent ways to target the same element:
ClickInput(selector="button.submit-btn")                          # CSS
ClickInput(selector=LocatorSpec(role="button", role_name="Submit"))  # ARIA role
ClickInput(selector=LocatorSpec(text="Submit"))                   # visible text

# More options:
FillInput(selector=LocatorSpec(label="Email"), value="me@example.com")
FillInput(selector=LocatorSpec(placeholder="Search..."), value="query")
ClickInput(selector=LocatorSpec(test_id="login-button"))
```

In JSON (and MCP `web_interact`), pass either a string or a `LocatorSpec` object:

```json
{"action": "click", "selector": {"role": "button", "role_name": "Submit"}}
```

Resolution priority: `role` > `test_id` > `label` > `placeholder` > `text` > `selector`.

## Retry Policies

Declarative retry profiles via `FetchConfig.retry_policy`:

| Policy | Retries | Base | Max | Use case |
|---|---|---|---|---|
| `fast` | 1 | 0.5s | 5s | Latency-sensitive flows; quick failure preferred |
| `balanced` (default) | 3 | 1s | 30s | General-purpose |
| `paranoid` | 5 | 2s | 60s | Flaky targets where eventual success matters |

```python
config = AppConfig(fetch={"retry_policy": "paranoid"})
```

Numeric overrides win over the policy: `AppConfig(fetch={"retry_policy": "fast", "max_retries": 7})` keeps `fast`'s base/max delays but uses 7 retries.

## Debug Mode

When enabled, every fetch/action/download failure auto-captures HTML, a screenshot, and an error JSON to `debug_dir/{correlation_id}/{timestamp}-{label}.{ext}`:

```python
config = AppConfig(debug={"enabled": True, "debug_dir": "/tmp/web_agent_debug"})
async with Agent(config) as agent:
    result = await agent.fetch_and_extract("https://flaky-site.example.com")
    if result.extraction_method == "none":
        print("Failed; debug artifacts:", result.debug_artifacts)
```

Result models gain a `debug_artifacts: list[str]` field with the saved file paths.

## Correlation IDs

Every public Agent method generates a UUID4 correlation id. The id is:

- Echoed back on every result model (`result.correlation_id`)
- Auto-injected into every `loguru` log record's `extra["cid"]` field
- Carried through retries, fetches, extractions, and recipe sub-calls

To use it in your own log format:

```python
from loguru import logger
import sys
logger.remove()
logger.add(sys.stderr, format="{time} | {extra[cid]} | {message}")
```

## MCP Integration

Run `web_agent` as an MCP (Model Context Protocol) server so Claude Desktop, Claude Code, Cursor, and any other MCP-compatible AI client can use it directly as a tool. The browser stays warm across tool calls within a session (skips ~5-10s startup per call after the first).

### Starting the server

```bash
# Via module:
python -m web_agent.mcp_server

# Via installed script:
web-agent-mcp

# Via CLI subcommand:
python -m web_agent serve-mcp
```

The server uses stdio transport -- it's invoked by the MCP client, not run standalone.

### Exposed Tools (11 total)

**Single-shot tools** -- one URL or query, one result:

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `web_search` | Search the web and extract content from top results | `query`, `max_results=10`, `session_id=None` |
| `web_fetch` | Fetch a single URL and extract main content | `url`, `session_id=None` |
| `web_download` | Download a file or save a web page | `url`, `filename=None`, `session_id=None` |
| `web_screenshot` | Take a screenshot of a page | `url`, `full_page=False`, `path=None`, `session_id=None` |
| `web_interact` | Execute a browser action sequence | `url`, `actions: list[dict]`, `stop_on_error=True`, `session_id=None` |

**High-level recipes** -- composite workflows for common goals:

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `web_search_best` | Search, rank results, return extracted top hit | `query`, `ranking="default"`, `session_id=None` |
| `web_find_and_download` | Search and download first matching file | `query`, `file_types=["pdf"]`, `session_id=None` |
| `web_research` | Multi-page research with citations | `query`, `max_pages=5`, `depth=1`, `session_id=None` |

**Browser session management** -- retain cookies/login across calls:

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `create_browser_session` | Create a persistent browser session | `name=None` |
| `close_browser_session` | Close a session and free resources | `session_id` |
| `list_browser_sessions` | List all live sessions | -- |

All tools return structured Pydantic models that auto-serialize to JSON for the client. Every tool accepts an optional `session_id` to reuse a persistent browser context for cookie/login continuity.

### Claude Desktop Setup

Edit `claude_desktop_config.json`:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%AppData%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

Add:

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "python",
      "args": ["-m", "web_agent.mcp_server"]
    }
  }
}
```

Restart Claude Desktop. The 11 tools should appear in the tool picker.

### Claude Code Setup

```bash
claude mcp add web_agent -- python -m web_agent.mcp_server
```

Or manually edit `~/.claude.json` (or your project's `.mcp.json`):

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "python",
      "args": ["-m", "web_agent.mcp_server"]
    }
  }
}
```

### Cursor Setup

Edit `~/.cursor/mcp.json` (or project-local `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "python",
      "args": ["-m", "web_agent.mcp_server"]
    }
  }
}
```

### Custom Config via Environment Variables

All `AppConfig` fields are overridable via env vars in the MCP config:

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "python",
      "args": ["-m", "web_agent.mcp_server"],
      "env": {
        "WEB_AGENT_LOG_LEVEL": "INFO",
        "WEB_AGENT_BROWSER__HEADLESS": "true",
        "WEB_AGENT_SEARCH__MAX_RESULTS": "5",
        "WEB_AGENT_DOWNLOAD__DOWNLOAD_DIR": "/tmp/web_agent_downloads"
      }
    }
  }
}
```

See [sample_data/mcp_config_example.json](sample_data/mcp_config_example.json) for a complete example.

### Testing the Server

Use the MCP inspector (bundled with the `mcp` CLI):

```bash
mcp dev web_agent/mcp_server.py
```

This launches a web UI where you can invoke each tool with test inputs and see the results.

## Error Handling

All exceptions inherit from `WebAgentError`:

```python
from web_agent.exceptions import NavigationError, DownloadError, WebAgentError

async with Agent() as agent:
    try:
        result = await agent.fetch_and_extract(url)
    except NavigationError as e:
        print(f"Page failed: {e} (status={e.status_code})")
    except WebAgentError as e:
        print(f"Agent error: {e}")
```

Exception hierarchy:

```
WebAgentError
  |-- BrowserError              Browser launch/context failures
  |-- NavigationError           Page load, timeout, blocked
  |-- ExtractionError           Content extraction failures
  |-- SearchError               Search engine failures
  |-- DownloadError             File download failures
  |-- ActionError               Browser action failures
  |     |-- ActionTimeoutError
  |     `-- SelectorNotFoundError
  |-- DomainNotAllowedError     URL host not in allow-list / matches deny-list
  |-- BudgetExceededError       Per-call budget (pages/chars/time) hit
  |-- SafeModeBlockedError      Operation forbidden by safe_mode
  `-- ConfigError               Configuration validation failures
```

## Docker

```dockerfile
FROM python:3.13-slim

WORKDIR /app
COPY . .

RUN pip install -e . && playwright install --with-deps chromium

CMD ["python", "-m", "web_agent", "search", "example query"]
```

## Testing

```bash
# Run all 111 tests
python -m pytest -v

# Unit tests only (no network) -- 90 tests, runs in <1 second
python -m pytest tests/test_models.py tests/test_content_extractor.py \
                 tests/test_correlation.py tests/test_retry_policies.py \
                 tests/test_safety.py tests/test_locators.py \
                 tests/test_recipes.py -v

# Integration tests (requires network + Chromium) -- 21 tests, ~2 minutes
python -m pytest tests/test_agent.py tests/test_browser_actions.py -v
```

## Project Structure

```
web_agent/
  __init__.py            # v1.1.0, public API exports (49 names)
  py.typed               # PEP 561 type checking support
  exceptions.py          # Exception hierarchy (12 classes)
  config.py              # Programmatic + env var + YAML configuration
  models.py              # 30+ Pydantic v2 models (incl. LocatorSpec, SessionInfo, Citation, ResearchResult)
  utils.py               # Retry decorator, RetryPolicy, BudgetTracker, domain checks
  correlation.py         # ContextVar-based correlation IDs + loguru patcher
  debug.py               # DebugCapture for failure HTML/screenshot/JSON snapshots
  agent.py               # Main entry point (Agent class)
  browser_manager.py     # Chromium lifecycle, stealth, semaphore + persistent contexts
  browser_actions.py     # 12 automation action handlers + semantic locators
  session_manager.py     # Persistent named BrowserContext sessions
  search_engine.py       # Google + DuckDuckGo search
  web_fetcher.py         # Page fetching with retry, safety, debug, sessions
  content_extractor.py   # trafilatura -> BS4 -> raw fallback chain
  downloader.py          # Three-strategy file/page download with safety + sessions
  recipes.py             # High-level recipes (search_and_open_best, find_and_download, web_research)
  mcp_server.py          # FastMCP server with 11 tools
  main.py                # CLI (search, fetch, download, interact, screenshot, serve-mcp)
tests/                   # 111 tests (90 unit + 21 integration)
config.example.yaml      # Reference configuration
sample_data/             # Test fixtures and example action sequences
```

## License

Apache-2.0 license
