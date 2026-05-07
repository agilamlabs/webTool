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

        # High-level recipes
        best = await agent.search_and_open_best_result("FastAPI tutorial")
        research = await agent.web_research("vector databases", max_pages=3)

Custom configuration::

    from web_agent import Agent, AppConfig, RetryPolicy, SafetyConfig

    config = AppConfig(
        browser={"headless": False},
        log_level="DEBUG",
        fetch={"retry_policy": "fast"},
        safety={"allowed_domains": ["example.com"], "max_pages_per_call": 10},
        debug={"enabled": True},
    )
    async with Agent(config) as agent:
        ...
"""

__version__ = "1.6.1"

from .agent import Agent
from .audit import AuditLogger
from .cache import Cache, DiskCache
from .config import (
    AppConfig,
    AuditConfig,
    AutomationConfig,
    BrowserConfig,
    CacheConfig,
    DebugConfig,
    DownloadConfig,
    ExtractionConfig,
    FetchConfig,
    SafetyConfig,
    SearchConfig,
)
from .correlation import correlation_scope, get_correlation_id, new_correlation_id
from .exceptions import (
    ActionError,
    ActionTimeoutError,
    BrowserError,
    BudgetExceededError,
    ConfigError,
    DomainNotAllowedError,
    DownloadError,
    ExtractionError,
    NavigationError,
    SafeModeBlockedError,
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
    Citation,
    DownloadResult,
    ExtractionResult,
    FetchDiagnostic,
    FetchResult,
    FetchStatus,
    FormFilterSpec,
    LocatorSpec,
    ResearchResult,
    ScreenshotResult,
    SearchResponse,
    SearchResultItem,
    SelectorLike,
    SessionInfo,
)
from .rate_limiter import RateLimiter
from .recipes import Recipes
from .robots import RobotsChecker
from .search_providers import (
    DDGSProvider,
    PlaywrightProvider,
    SearchProvider,
    SearXNGProvider,
)
from .utils import BudgetTracker, RetryPolicy, get_retry_policy

__all__ = [
    # Version
    "__version__",
    # Core
    "Agent",
    "Recipes",
    # Configuration
    "AppConfig",
    "AuditConfig",
    "AutomationConfig",
    "BrowserConfig",
    "CacheConfig",
    "DebugConfig",
    "DownloadConfig",
    "ExtractionConfig",
    "FetchConfig",
    "SafetyConfig",
    "SearchConfig",
    # Correlation
    "correlation_scope",
    "get_correlation_id",
    "new_correlation_id",
    # Retry
    "RetryPolicy",
    "get_retry_policy",
    "BudgetTracker",
    # Politeness + audit
    "AuditLogger",
    "RateLimiter",
    "RobotsChecker",
    # Cache
    "Cache",
    "DiskCache",
    # Search providers
    "DDGSProvider",
    "PlaywrightProvider",
    "SearXNGProvider",
    "SearchProvider",
    # Exceptions
    "ActionError",
    "ActionTimeoutError",
    "BrowserError",
    "BudgetExceededError",
    "ConfigError",
    "DomainNotAllowedError",
    "DownloadError",
    "ExtractionError",
    "NavigationError",
    "SafeModeBlockedError",
    "SearchError",
    "SelectorNotFoundError",
    "WebAgentError",
    # Models
    "Action",
    "ActionResult",
    "ActionSequenceResult",
    "ActionStatus",
    "ActionType",
    "AgentResult",
    "Citation",
    "DownloadResult",
    "ExtractionResult",
    "FetchDiagnostic",
    "FetchResult",
    "FetchStatus",
    "FormFilterSpec",
    "LocatorSpec",
    "ResearchResult",
    "ScreenshotResult",
    "SearchResponse",
    "SearchResultItem",
    "SelectorLike",
    "SessionInfo",
]
