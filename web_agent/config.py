"""Configuration management with programmatic construction, environment variables, and YAML.

Supports three configuration methods (in priority order):

1. **Programmatic** (recommended for AI agents)::

    from web_agent import AppConfig
    config = AppConfig(browser={"headless": False}, log_level="DEBUG")

2. **Environment variables** (prefix ``WEB_AGENT_``)::

    export WEB_AGENT_LOG_LEVEL=DEBUG
    export WEB_AGENT_BROWSER__HEADLESS=false

3. **YAML file** (optional)::

    config = AppConfig.from_yaml("/path/to/config.yaml")
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class BrowserConfig(BaseSettings):
    """Chromium browser launch and context settings."""

    headless: bool = True
    slow_mo: int = 0
    default_timeout: int = 30000
    navigation_timeout: int = 45000
    max_contexts: int = 3
    block_resources: list[str] = Field(
        default_factory=lambda: ["image", "font", "stylesheet", "media"]
    )
    user_data_dir: Optional[str] = None
    viewport_width: int = 1920
    viewport_height: int = 1080


class SearchConfig(BaseSettings):
    """Web search parameters (Google primary, DuckDuckGo fallback)."""

    max_results: int = 10
    search_url: str = "https://www.google.com/search"
    language: str = "en"
    region: str = "us"
    safe_search: bool = False


class FetchConfig(BaseSettings):
    """Page fetching, rendering wait conditions, and retry settings.

    Use ``retry_policy`` for declarative retry profiles
    (``"fast"`` | ``"balanced"`` | ``"paranoid"``).  When ``retry_policy``
    is set and the numeric retry fields (``max_retries``/``retry_base_delay``/
    ``retry_max_delay``) are left at their defaults, the policy values
    are applied automatically.  If the user explicitly sets any numeric
    retry field, it overrides the policy.
    """

    wait_until: str = "networkidle"
    wait_for_selector: Optional[str] = None
    extra_wait_ms: int = 0
    retry_policy: str = "balanced"  # fast | balanced | paranoid
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    non_retryable_status_codes: list[int] = Field(
        default_factory=lambda: [400, 401, 403, 404, 405, 410, 451]
    )

    @model_validator(mode="after")
    def _apply_retry_policy(self) -> FetchConfig:
        """Apply named retry policy unless user explicitly set numeric fields."""
        # Default policy values (matches BALANCED). If any numeric retry field
        # was explicitly set to something different, that means the user
        # provided it -- skip policy application to preserve their choice.
        explicit = self.model_fields_set
        numeric_keys = {"max_retries", "retry_base_delay", "retry_max_delay"}
        user_set_numeric = bool(explicit & numeric_keys)

        if not user_set_numeric and self.retry_policy != "balanced":
            # Lazy import to avoid circular dep
            from .utils import get_retry_policy

            kwargs = get_retry_policy(self.retry_policy)
            self.max_retries = int(kwargs["max_retries"])
            self.retry_base_delay = float(kwargs["base_delay"])
            self.retry_max_delay = float(kwargs["max_delay"])
        return self


class DownloadConfig(BaseSettings):
    """File download settings including size limits and allowed types."""

    download_dir: str = "./downloads"
    max_file_size_mb: int = 100
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            ".pdf", ".csv", ".xlsx", ".xls", ".zip", ".json", ".txt",
            ".doc", ".docx", ".ppt", ".pptx", ".png", ".jpg", ".jpeg",
            ".gif", ".svg", ".xml", ".html", ".htm", ".md", ".tar", ".gz",
        ]
    )


class ExtractionConfig(BaseSettings):
    """Content extraction settings for the trafilatura/BS4/raw fallback chain."""

    favor_precision: bool = False
    favor_recall: bool = True
    include_tables: bool = True
    include_links: bool = False
    include_comments: bool = False
    min_content_length: int = 50


class AutomationConfig(BaseSettings):
    """Browser automation action settings."""

    default_action_timeout: int = 10000
    screenshot_dir: str = "./screenshots"
    screenshot_format: str = "png"
    screenshot_quality: int = 80
    stop_on_error: bool = True
    slow_mo_actions: int = 0


class SafetyConfig(BaseSettings):
    """Domain allow/deny lists, granular allow_* flags, safe mode, and per-call budget knobs.

    Empty ``allowed_domains`` means all hosts are allowed (subject to deny-list).
    Domain patterns use suffix-match semantics: ``example.com`` matches
    ``api.example.com`` and ``www.example.com`` but not ``notexample.com``.

    Granular safety flags (each independently configurable):

    - ``allow_js_evaluation`` (default **False**): controls ``EvaluateInput``
      actions which run arbitrary JavaScript in the browser context. Default
      False because LLM-supplied JS can exfiltrate cookies / read DOM in
      authenticated sessions. Opt in explicitly when you need it.
    - ``allow_downloads`` (default True): controls file-download actions.
      Disable to enforce read-only browsing.
    - ``allow_form_submit`` (default True): controls clicks on submit-typed
      buttons (heuristic match against text/role/selector).
    - ``block_private_ips`` (default True): SSRF protection -- blocks RFC1918,
      loopback, link-local (incl. AWS IMDS at 169.254.169.254).

    ``safe_mode`` (default False) is a master kill-switch: when True it
    overrides the three ``allow_*`` flags to False (regardless of their
    explicit settings). ``block_private_ips`` is independent of safe_mode.

    Budget knobs limit the cost of a single Agent method call:

    - ``max_pages_per_call``: stops fetching after N pages.
    - ``max_chars_per_call``: stops extracting after total chars exceeded.
    - ``max_time_per_call_seconds``: wall-clock cutoff for the call.
    """

    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)
    safe_mode: bool = False
    allow_js_evaluation: bool = False
    allow_downloads: bool = True
    allow_form_submit: bool = True
    block_private_ips: bool = True
    max_pages_per_call: int = 50
    max_chars_per_call: int = 1_000_000
    max_time_per_call_seconds: float = 300.0

    @model_validator(mode="after")
    def _apply_safe_mode(self) -> SafetyConfig:
        """When safe_mode is True, force all allow_* flags to False."""
        if self.safe_mode:
            self.allow_js_evaluation = False
            self.allow_downloads = False
            self.allow_form_submit = False
        return self


class DebugConfig(BaseSettings):
    """Auto-capture HTML/screenshot/error context on failures for debugging.

    When ``enabled`` is True, every fetch/action/download failure dumps a
    snapshot to ``debug_dir/{correlation_id}/{timestamp}-{label}.{html|png|json}``
    so the failure can be reproduced and diagnosed offline.
    """

    enabled: bool = False
    debug_dir: str = "./debug"
    capture_html: bool = True
    capture_screenshot: bool = True
    max_artifacts_per_call: int = 5


class AppConfig(BaseSettings):
    """Top-level configuration for the web_agent toolkit.

    All sub-configs use sensible defaults, so ``AppConfig()`` works out of
    the box with no file or environment variables required.

    Args:
        browser: Chromium browser settings.
        search: Web search parameters.
        fetch: Page fetching and retry settings.
        download: File download settings.
        extraction: Content extraction settings.
        automation: Browser automation action settings.
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR).
        output_dir: Directory for saving pipeline JSON results.
        base_dir: Base directory for resolving relative paths. Defaults to CWD.

    Example::

        from web_agent import Agent, AppConfig

        # All defaults - no config file needed:
        async with Agent() as agent:
            result = await agent.fetch_and_extract("https://example.com")

        # Custom config:
        config = AppConfig(
            browser={"headless": False},
            log_level="DEBUG",
            output_dir="/tmp/results",
        )
        async with Agent(config) as agent:
            ...
    """

    model_config = {"env_prefix": "WEB_AGENT_"}

    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    log_level: str = "INFO"
    output_dir: str = "./output"
    base_dir: str = Field(default=".", description="Base directory for resolving relative paths")

    @model_validator(mode="after")
    def _resolve_paths(self) -> AppConfig:
        """Resolve relative paths against base_dir to produce absolute paths."""
        base = Path(self.base_dir).resolve()

        def _resolve(p: str) -> str:
            path = Path(p)
            if not path.is_absolute():
                return str(base / path)
            return p

        self.output_dir = _resolve(self.output_dir)
        self.download.download_dir = _resolve(self.download.download_dir)
        self.automation.screenshot_dir = _resolve(self.automation.screenshot_dir)
        self.debug.debug_dir = _resolve(self.debug.debug_dir)
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        """Load configuration from a YAML file.

        Args:
            path: Absolute or relative path to the YAML config file.

        Returns:
            AppConfig populated from the YAML data with defaults for missing keys.

        Raises:
            FileNotFoundError: If the YAML file does not exist.
            ConfigError: If the YAML cannot be parsed or its values fail
                Pydantic validation (wraps yaml.YAMLError + pydantic.ValidationError).
        """
        from .exceptions import ConfigError

        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        try:
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse YAML config {p}: {exc}") from exc
        if "base_dir" not in data:
            data["base_dir"] = str(p.parent.resolve())
        try:
            return cls(**data)
        except Exception as exc:
            raise ConfigError(
                f"Config validation failed for {p}: {exc}"
            ) from exc
