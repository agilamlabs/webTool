# webTool

Professional agentic web search, fetch, download, extraction, and browser automation toolkit built on Playwright's headless Chromium.

Designed as a tool for AI agents that need to search the web, fetch JavaScript-heavy pages, extract structured content, download files, and automate browser interactions -- all through a clean async Python API.

## Features

- **Web Search** -- Google with automatic DuckDuckGo fallback (handles CAPTCHAs)
- **Page Fetching** -- Renders JavaScript, retries with exponential backoff, detects download URLs
- **Content Extraction** -- Three-tier fallback: trafilatura (F1 ~0.958) -> BeautifulSoup4 -> raw text
- **File Download** -- Three strategies: httpx streaming -> Playwright page save -> Playwright JS download
- **Browser Automation** -- 12 action types composable into scripted sequences
- **Screenshots** -- Viewport, full-page, or element-specific captures
- **Anti-Detection** -- playwright-stealth, user-agent rotation, resource blocking
- **Structured Output** -- All results are Pydantic v2 models serializable to JSON
- **MCP Server** -- Built-in MCP server exposes all capabilities to Claude Desktop, Claude Code, Cursor, and other MCP-compatible AI clients

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
| `fetch` | `max_retries` | `3` | Retry count for transient failures |
| `fetch` | `retry_base_delay` | `1.0` | Base delay (seconds) for exponential backoff |
| `download` | `download_dir` | `"./downloads"` | Directory for downloaded files |
| `download` | `max_file_size_mb` | `100` | Maximum file size limit |
| `extraction` | `favor_recall` | `true` | Prefer extracting more content vs precision |
| `extraction` | `include_tables` | `true` | Include table data in extraction |
| `extraction` | `min_content_length` | `50` | Minimum characters to accept extraction |
| `automation` | `default_action_timeout` | `10000` | Timeout per browser action (ms) |
| `automation` | `screenshot_dir` | `"./screenshots"` | Directory for screenshots |
| `automation` | `stop_on_error` | `true` | Halt action sequence on first failure |

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
  |-- SearchEngine          Google -> DuckDuckGo fallback
  |-- WebFetcher            Playwright page rendering + retry
  |-- ContentExtractor      trafilatura -> BS4 -> raw text
  |-- Downloader            httpx -> Playwright page save -> Playwright download
  |-- BrowserActions        12 action handlers with dispatch table
  |
  `-- BrowserManager        Chromium lifecycle, stealth, semaphore pool
```

**Smart routing:**

- File URLs (`.pdf`, `.xlsx`, `.zip`) are detected upfront and routed to the downloader
- Web page URLs (`.html`, `.htm`) use Playwright page save instead of download events
- `networkidle` timeouts automatically fall back to `load` wait state
- HTTP 4xx errors fail immediately; 5xx errors retry with exponential backoff

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

### Exposed Tools

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `web_search` | Search the web and extract content from top results | `query: str`, `max_results: int=10` |
| `web_fetch` | Fetch a single URL and extract main content | `url: str` |
| `web_download` | Download a file or save a web page | `url: str`, `filename: str=None` |
| `web_screenshot` | Take a screenshot of a page | `url: str`, `full_page: bool=False`, `path: str=None` |
| `web_interact` | Execute a browser action sequence | `url: str`, `actions: list[dict]`, `stop_on_error: bool=True` |

All tools return structured Pydantic models that auto-serialize to JSON for the client.

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

Restart Claude Desktop. The 5 tools should appear in the tool picker.

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
  |-- BrowserError           Browser launch/context failures
  |-- NavigationError        Page load, timeout, blocked
  |-- ExtractionError        Content extraction failures
  |-- SearchError            Search engine failures
  |-- DownloadError          File download failures
  |-- ActionError            Browser action failures
  |     |-- ActionTimeoutError
  |     `-- SelectorNotFoundError
  `-- ConfigError            Configuration validation failures
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
# Run all 46 tests
python -m pytest -v

# Unit tests only (no network)
python -m pytest tests/test_models.py tests/test_content_extractor.py -v

# Integration tests (requires network + Chromium)
python -m pytest tests/test_agent.py tests/test_browser_actions.py -v
```

## Project Structure

```
web_agent/
  __init__.py            # v1.0.0, public API exports
  py.typed               # PEP 561 type checking support
  exceptions.py          # Exception hierarchy
  config.py              # Programmatic + env var + YAML configuration
  models.py              # 25+ Pydantic v2 models
  agent.py               # Main entry point (Agent class)
  browser_manager.py     # Chromium lifecycle, stealth, semaphore pool
  browser_actions.py     # 12 automation action handlers
  search_engine.py       # Google + DuckDuckGo search
  web_fetcher.py         # Page fetching with retry + smart routing
  content_extractor.py   # trafilatura -> BS4 -> raw fallback chain
  downloader.py          # Three-strategy file/page download
  mcp_server.py          # FastMCP server exposing tools to AI clients
  main.py                # CLI (search, fetch, download, interact, screenshot, serve-mcp)
tests/                   # 46 tests (unit + integration)
config.example.yaml      # Reference configuration
sample_data/             # Test fixtures and example action sequences
```

## License

Apache-2.0 license
