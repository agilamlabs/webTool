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

__version__ = "1.6.16"

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
    DiagnosticsConfig,
    DownloadConfig,
    ExtractionConfig,
    FetchConfig,
    SafetyConfig,
    SearchConfig,
    SkillsConfig,
    WorkspaceConfig,
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
    BaseAction,
    CdpConnectionInfo,
    ChallengeInfo,
    Citation,
    ClickInput,
    ClickXYInput,
    DialogInput,
    DialogResponse,
    DoctorCheck,
    DoctorReport,
    DomainSkill,
    DownloadResult,
    DragAndDropInput,
    EvaluateInput,
    ExtractionResult,
    FetchDiagnostic,
    FetchResult,
    FetchStatus,
    FillInput,
    FormFilterSpec,
    HoverInput,
    HtmlCaptureSource,
    IframeClickInput,
    KeyboardInput,
    LocatorSpec,
    MouseButton,
    NavigateDirection,
    NavigateInput,
    NetworkEvent,
    ObserveResult,
    PressKeyInput,
    ResearchResult,
    ScreenshotFormat,
    ScreenshotInput,
    ScreenshotResult,
    ScrollDirection,
    ScrollInput,
    SearchResponse,
    SearchResultItem,
    SelectInput,
    SelectorLike,
    SessionInfo,
    ShadowDomClickInput,
    SkillApplicationResult,
    SkillInputSpec,
    TabInfo,
    ToolError,
    ToolMessage,
    ToolSeverity,
    ToolWarning,
    TypeInput,
    TypeTextInput,
    UploadFileInput,
    WaitInput,
    WaitTarget,
)
from .network_collector import NetworkCollector
from .ownership import OwnershipToken
from .rate_limiter import RateLimiter
from .recipes import Recipes
from .robots import RobotsChecker
from .search_providers import (
    DDGSProvider,
    PlaywrightProvider,
    SearchProvider,
    SearXNGProvider,
)
from .trace_recorder import SessionTraceRecorder
from .utils import (
    BudgetTracker,
    RetryPolicy,
    get_retry_policy,
    parse_retry_after,
    safe_page_content,
)
from .web_fetcher import EXTRACTABLE_BINARY_KINDS, is_binary_kind, is_extractable_binary_kind

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
    "DiagnosticsConfig",
    "DownloadConfig",
    "ExtractionConfig",
    "FetchConfig",
    "SafetyConfig",
    "SearchConfig",
    "SkillsConfig",
    "WorkspaceConfig",
    # Correlation
    "correlation_scope",
    "get_correlation_id",
    "new_correlation_id",
    # Retry
    "RetryPolicy",
    "get_retry_policy",
    "BudgetTracker",
    # HTTP helpers (v1.6.12)
    "parse_retry_after",
    # Page-content capture helpers (v1.6.13)
    "safe_page_content",
    # Politeness + audit
    "AuditLogger",
    "RateLimiter",
    "RobotsChecker",
    # Cache
    "Cache",
    "DiskCache",
    # Classification helpers (v1.6.10 / v1.6.11)
    "is_binary_kind",
    "is_extractable_binary_kind",
    "EXTRACTABLE_BINARY_KINDS",
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
    "BaseAction",
    "CdpConnectionInfo",
    "ChallengeInfo",
    "Citation",
    "ClickInput",
    "ClickXYInput",
    "DialogInput",
    "DialogResponse",
    "DoctorCheck",
    "DoctorReport",
    "DomainSkill",
    "DownloadResult",
    "DragAndDropInput",
    "EvaluateInput",
    "ExtractionResult",
    "FetchDiagnostic",
    "FetchResult",
    "FetchStatus",
    "FillInput",
    "FormFilterSpec",
    "HoverInput",
    "HtmlCaptureSource",
    "IframeClickInput",
    "KeyboardInput",
    "LocatorSpec",
    "MouseButton",
    "NavigateDirection",
    "NavigateInput",
    "NetworkCollector",
    "NetworkEvent",
    "ObserveResult",
    "OwnershipToken",
    "PressKeyInput",
    "ResearchResult",
    "ScreenshotFormat",
    "ScreenshotInput",
    "ScreenshotResult",
    "ScrollDirection",
    "ScrollInput",
    "SearchResponse",
    "SearchResultItem",
    "SelectInput",
    "SelectorLike",
    "SessionInfo",
    "SessionTraceRecorder",
    "ShadowDomClickInput",
    "SkillApplicationResult",
    "SkillInputSpec",
    "TabInfo",
    "ToolError",
    "ToolMessage",
    "ToolSeverity",
    "ToolWarning",
    "TypeInput",
    "TypeTextInput",
    "UploadFileInput",
    "WaitInput",
    "WaitTarget",
]
