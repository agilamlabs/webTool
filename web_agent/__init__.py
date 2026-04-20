"""web_agent -- Agentic web search, fetch, download, and extraction toolkit.

A professional-grade Python toolkit for AI agents that need to search the web,
fetch and render JavaScript-heavy pages, extract structured content, download
files, and automate browser interactions using Playwright's headless Chromium.

Quick start::

    from web_agent import Agent

    async with Agent() as agent:
        # Search and extract content from top results
        result = await agent.search_and_extract("Python web scraping", max_results=5)

        # Fetch and extract a single page
        page = await agent.fetch_and_extract("https://example.com")

        # Download a file
        download = await agent.download("https://example.com/report.pdf")

        # Take a screenshot
        screenshot = await agent.screenshot("https://example.com", full_page=True)

Custom configuration::

    from web_agent import Agent, AppConfig

    config = AppConfig(
        browser={"headless": False},
        log_level="DEBUG",
        output_dir="/tmp/results",
    )
    async with Agent(config) as agent:
        ...
"""

__version__ = "1.0.0"

from .agent import Agent
from .config import (
    AppConfig,
    AutomationConfig,
    BrowserConfig,
    DownloadConfig,
    ExtractionConfig,
    FetchConfig,
    SearchConfig,
)
from .exceptions import (
    ActionError,
    ActionTimeoutError,
    BrowserError,
    ConfigError,
    DownloadError,
    ExtractionError,
    NavigationError,
    SearchError,
    SelectorNotFoundError,
    WebAgentError,
)
from .models import (
    Action,
    ActionResult,
    ActionSequenceResult,
    ActionStatus,
    ActionType,
    AgentResult,
    DownloadResult,
    ExtractionResult,
    FetchResult,
    FetchStatus,
    ScreenshotResult,
    SearchResponse,
    SearchResultItem,
)

__all__ = [
    # Version
    "__version__",
    # Core
    "Agent",
    # Configuration
    "AppConfig",
    "AutomationConfig",
    "BrowserConfig",
    "DownloadConfig",
    "ExtractionConfig",
    "FetchConfig",
    "SearchConfig",
    # Exceptions
    "ActionError",
    "ActionTimeoutError",
    "BrowserError",
    "ConfigError",
    "DownloadError",
    "ExtractionError",
    "NavigationError",
    "SearchError",
    "SelectorNotFoundError",
    "WebAgentError",
    # Models - Results
    "Action",
    "ActionResult",
    "ActionSequenceResult",
    "ActionStatus",
    "ActionType",
    "AgentResult",
    "DownloadResult",
    "ExtractionResult",
    "FetchResult",
    "FetchStatus",
    "ScreenshotResult",
    "SearchResponse",
    "SearchResultItem",
]
