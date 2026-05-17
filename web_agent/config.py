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
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings


def _normalize_domain_patterns(value: Any) -> Any:
    """Normalize a list of domain allow/deny patterns at config-load time.

    Accepts user-supplied strings like ``"https://Evil.com/"`` and
    coerces them to bare hostnames (``"evil.com"``) before the pattern
    is consulted by ``check_domain_allowed``. Without this, malformed
    entries would silently never match anything (the previous v1.6.4
    behavior). Non-list values pass through unchanged so pydantic's
    own type validation can fire.
    """
    if not isinstance(value, list):
        return value
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            # Let pydantic surface the type error naturally.
            out.append(raw)
            continue
        s = raw.strip().lower()
        if "://" in s:
            s = s.split("://", 1)[1]
        # Strip path / query / fragment
        for sep in ("/", "?", "#"):
            if sep in s:
                s = s.split(sep, 1)[0]
        s = s.strip().lstrip(".")
        if s:
            out.append(s)
    return out


class BrowserConfig(BaseSettings):
    """Chromium browser launch and context settings.

    v1.6.6 additions:

    * **Isolation profile** -- ``isolation_mode`` + ``profile_mode`` +
      ``profile_dir`` + ``cleanup_on_exit`` let webTool launch Chromium
      against its own dedicated user-data-dir, isolating cookies /
      localStorage / cache / downloads from the user's real Chrome.
      Defaults: ``isolation_mode=False`` (preserves v1.6.5 launch path).
    * **CDP attach** -- ``cdp_enabled`` + ``cdp_host`` + ``cdp_port`` +
      ``backend`` let external tools observe a webTool-launched browser
      over the Chrome DevTools Protocol. CDP requires isolation
      (DevToolsActivePort lives under the user-data-dir). Defaults:
      ``cdp_enabled=False``. ``attach_existing_browser=True`` is
      explicitly rejected -- webTool only controls browsers it launched.
    """

    headless: bool = True
    slow_mo: int = 0
    default_timeout: int = 30000
    navigation_timeout: int = 45000
    max_contexts: int = 3
    block_resources: list[str] = Field(
        default_factory=lambda: ["image", "font", "stylesheet", "media"]
    )
    user_data_dir: Optional[str] = Field(
        default=None,
        description=(
            "DEPRECATED in v1.6.6 -- use ``profile_dir`` + "
            "``isolation_mode=True`` instead. Retained for backward "
            "compatibility; if both ``user_data_dir`` and ``profile_dir`` "
            "are set, ``profile_dir`` wins."
        ),
    )
    viewport_width: int = 1920
    viewport_height: int = 1080

    # --- v1.6.6: Isolation profile launcher -----------------------------
    isolation_mode: bool = Field(
        default=False,
        description=(
            "Launch Chromium with a dedicated user-data-dir so cookies / "
            "localStorage / cache / downloads are isolated from the "
            "user's real Chrome. Required when ``cdp_enabled=True``."
        ),
    )
    profile_mode: Literal["ephemeral", "named"] = Field(
        default="ephemeral",
        description=(
            "Ephemeral profiles are auto-generated tempdirs deleted on "
            "Agent exit (when ``cleanup_on_exit=True``). Named profiles "
            "persist across runs at ``profile_dir`` for logged-in "
            "workflows. Only consulted when ``isolation_mode=True``."
        ),
    )
    profile_dir: Optional[str] = Field(
        default=None,
        description=(
            "Profile directory path. Resolved against ``AppConfig.base_dir`` "
            "when relative. Required when ``profile_mode='named'``; ignored "
            "when ``profile_mode='ephemeral'`` (tempdir is auto-generated "
            "under ``base_dir/.webtool/browser-profiles/``)."
        ),
    )
    cleanup_on_exit: bool = Field(
        default=True,
        description=(
            "If True and ``profile_mode='ephemeral'``, the auto-generated "
            "profile dir is removed on Agent exit. No-op for named profiles."
        ),
    )

    # --- v1.6.6: CDP attach to webTool-launched browser -----------------
    # v1.6.8 widened the Literal to include ``"remote_cdp"`` -- existing
    # configs with ``playwright`` or ``cdp_owned`` keep working unchanged;
    # the new value is opt-in only.
    backend: Literal["playwright", "cdp_owned", "remote_cdp"] = Field(
        default="playwright",
        description=(
            "Browser control backend. ``playwright`` (default) drives "
            "Chromium directly via Playwright's CDP. ``cdp_owned`` is a "
            "forward-compat label indicating CDP must be enabled; today "
            "it's identical to ``playwright`` with ``cdp_enabled=True``. "
            "``remote_cdp`` (v1.6.8) attaches to an externally-launched "
            "browser via ``chromium.connect_over_cdp(remote_cdp_url)``."
        ),
    )
    remote_cdp_url: Optional[str] = Field(
        default=None,
        description=(
            "WebSocket URL for an externally-launched CDP browser. Required "
            "when ``backend='remote_cdp'``. Example: "
            "``ws://127.0.0.1:9222/devtools/browser/<uuid>``. Must be a "
            "loopback address -- non-loopback URLs are rejected as a "
            "security foot-gun (same rule as ``cdp_host``)."
        ),
    )
    cdp_enabled: bool = Field(
        default=False,
        description=(
            "Launch Chromium with ``--remote-debugging-port`` so external "
            "tools can observe via CDP. Requires ``isolation_mode=True``. "
            "Never attaches to existing/personal browsers -- webTool only "
            "exposes CDP on browsers it launched itself."
        ),
    )
    cdp_host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address for the remote debugging endpoint. Must be a "
            "loopback address; non-loopback bindings are rejected as a "
            "security foot-gun."
        ),
    )
    cdp_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description=(
            "Remote debugging port. ``0`` means OS-assigned (recommended); "
            "the actual port is discovered from ``DevToolsActivePort`` "
            "written into the user-data-dir after launch."
        ),
    )
    launch_owned_cdp_browser: bool = Field(
        default=True,
        description=(
            "When ``cdp_enabled=True``, controls whether webTool launches "
            "its own browser. Always True today -- attaching to existing "
            "browsers is explicitly disallowed (see ``attach_existing_browser``)."
        ),
    )
    attach_existing_browser: bool = Field(
        default=False,
        description=(
            "ALWAYS REJECTED if True. webTool never attaches to a "
            "user's existing/personal Chrome -- that would expose real "
            "cookies, history, and logged-in accounts. CDP control is "
            "only available against webTool-launched browsers."
        ),
    )

    @model_validator(mode="after")
    def _validate_isolation_and_cdp(self) -> BrowserConfig:
        """Enforce safety rules around isolation and CDP."""
        from .exceptions import ConfigError

        if self.attach_existing_browser:
            raise ConfigError(
                "BrowserConfig.attach_existing_browser=True is not "
                "supported. webTool only controls browsers it launched "
                "itself; attaching to an existing/personal Chrome would "
                "expose the user's cookies, history, and logged-in "
                "accounts. Use cdp_enabled=True with isolation_mode=True "
                "for CDP control of a webTool-launched browser."
            )

        if self.backend == "cdp_owned" and not self.cdp_enabled:
            raise ConfigError(
                "BrowserConfig.backend='cdp_owned' implies cdp_enabled=True. "
                "Either set cdp_enabled=True or switch backend to 'playwright'."
            )

        if self.cdp_enabled and self.cdp_host not in {"127.0.0.1", "localhost"}:
            raise ConfigError(
                f"BrowserConfig.cdp_host={self.cdp_host!r} is not a loopback "
                "address. Binding the Chrome DevTools Protocol port to a "
                "non-loopback interface would expose browser control to the "
                "network. Use '127.0.0.1' or 'localhost'."
            )

        if self.cdp_enabled and not self.isolation_mode:
            raise ConfigError(
                "BrowserConfig.cdp_enabled=True requires isolation_mode=True. "
                "CDP discovery reads DevToolsActivePort from the user-data-dir, "
                "which only exists when isolation_mode is enabled."
            )

        if self.profile_mode == "named" and not self.profile_dir:
            raise ConfigError(
                "BrowserConfig.profile_mode='named' requires profile_dir to be "
                "set. Either set profile_dir to a directory path or switch to "
                "profile_mode='ephemeral'."
            )

        # --- v1.6.8: remote CDP backend ---
        if self.backend == "remote_cdp":
            if not self.remote_cdp_url:
                raise ConfigError(
                    "BrowserConfig.backend='remote_cdp' requires remote_cdp_url. "
                    "Set remote_cdp_url to a ws://127.0.0.1:<port>/devtools/browser/<uuid> "
                    "endpoint exposed by an externally-launched Chromium."
                )
            parsed = urlparse(self.remote_cdp_url)
            if parsed.scheme not in {"ws", "wss"}:
                raise ConfigError(
                    f"BrowserConfig.remote_cdp_url must use ws:// or wss://, "
                    f"got scheme {parsed.scheme!r}."
                )
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ConfigError(
                    f"BrowserConfig.remote_cdp_url host {parsed.hostname!r} is "
                    "not loopback. Connecting to a non-loopback CDP endpoint "
                    "would let an external process pose as the local browser. "
                    "Use 127.0.0.1, localhost, or ::1."
                )
            # remote_cdp is incompatible with the owned-launch knobs -- we
            # don't own the user-data-dir on a remote browser, and there's
            # no DevToolsActivePort file to discover the port from.
            if self.isolation_mode:
                raise ConfigError(
                    "BrowserConfig.backend='remote_cdp' is incompatible with "
                    "isolation_mode=True. Isolation mode owns a user-data-dir; "
                    "the remote browser already has its own profile."
                )
            if self.cdp_enabled:
                raise ConfigError(
                    "BrowserConfig.backend='remote_cdp' is incompatible with "
                    "cdp_enabled=True. cdp_enabled triggers a launch flow with "
                    "DevToolsActivePort discovery; remote_cdp connects to an "
                    "already-running browser instead."
                )
        elif self.remote_cdp_url is not None:
            # User set remote_cdp_url without flipping backend -- surface
            # the no-op rather than letting it silently disappear.
            raise ConfigError(
                "BrowserConfig.remote_cdp_url was set but backend is "
                f"{self.backend!r}. Set backend='remote_cdp' to use it, or "
                "clear remote_cdp_url to silence this error."
            )

        return self


class SearchConfig(BaseSettings):
    """Web search parameters and multi-provider chain configuration.

    Provider chain (NEW in 1.4.0): ``providers`` lists search backends
    in priority order. Each provider is tried until one returns
    results. Available providers:

    - ``"searxng"`` -- self-hosted SearXNG instance via JSON API (set
      ``searxng_base_url`` to enable, otherwise silently skipped).
    - ``"ddgs"`` -- DuckDuckGo via the ``ddgs`` package (silently
      skipped when the optional dependency is missing).
    - ``"playwright"`` -- browser-driven Google + DDG HTML scraping
      (always available; slow but reliable fallback).

    To use only browser-based search: ``providers=["playwright"]``.
    To skip Playwright entirely: ``providers=["searxng", "ddgs"]``.
    """

    max_results: int = 10
    search_url: str = "https://www.google.com/search"
    language: str = "en"
    region: str = "us"
    safe_search: bool = False

    # Multi-provider chain (NEW in 1.4.0)
    providers: list[str] = Field(
        default_factory=lambda: ["searxng", "ddgs", "playwright"],
        description=(
            "Search providers tried in priority order. First non-empty "
            "result wins. Available: 'searxng', 'ddgs', 'playwright'."
        ),
    )
    searxng_base_url: Optional[str] = Field(
        default=None,
        description=(
            "Base URL of self-hosted SearXNG (e.g. http://localhost:8888). "
            "When None, the SearXNG provider is silently skipped."
        ),
    )
    searxng_timeout: float = Field(
        default=10.0,
        description="HTTP timeout (seconds) for SearXNG JSON API calls.",
    )


class FetchConfig(BaseSettings):
    """Page fetching, rendering wait conditions, and retry settings.

    Use ``retry_policy`` for declarative retry profiles
    (``"fast"`` | ``"balanced"`` | ``"paranoid"``).  When ``retry_policy``
    is set and the numeric retry fields (``max_retries``/``retry_base_delay``/
    ``retry_max_delay``) are left at their defaults, the policy values
    are applied automatically.  If the user explicitly sets any numeric
    retry field, it overrides the policy.
    """

    wait_until: str = Field(
        default="domcontentloaded",
        description=(
            "Playwright wait condition for page navigation. Default "
            "'domcontentloaded' (DOM parsed) is fast and robust against "
            "pages with analytics/polling that prevent 'networkidle' from "
            "ever firing. Set to 'networkidle' for JS-heavy sites where "
            "content arrives after the initial DOM, or pair with "
            "extra_wait_ms / wait_for_selector to give async hydration "
            "time. Options: 'domcontentloaded' | 'load' | 'networkidle'."
        ),
    )
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
            ".pdf",
            ".csv",
            ".xlsx",
            ".xls",
            ".zip",
            ".json",
            ".txt",
            ".doc",
            ".docx",
            ".ppt",
            ".pptx",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".xml",
            ".html",
            ".htm",
            ".md",
            ".tar",
            ".gz",
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
    # v1.6.6: when False (default), session-owned ``execute_sequence`` calls
    # reuse the session's current tab (preserving scroll / viewport / cookies
    # across calls). Set True to restore v1.6.5 behavior where each
    # ``interact(url, ...)`` call against the same session_id opens a fresh
    # page within the session's BrowserContext.
    fresh_tab_per_call: bool = False


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
    # v1.6.7: upload-file safety. By default, ``UploadFileInput`` / the
    # top-level ``Agent.upload_file`` accepts only paths under
    # ``download.download_dir``. Without this fence, a prompt-injection
    # could call ``upload_file(selector=..., paths=["~/.ssh/id_rsa"])``
    # and exfiltrate arbitrary local files. Opt in to widen scope.
    allow_upload_outside_download_dir: bool = False

    # Normalize URLs / mixed-case input down to bare hostnames before any
    # ``check_domain_allowed`` consultation. Catches the common foot-gun
    # of passing ``"https://evil.com"`` as a deny pattern (which would
    # otherwise silently never match because the comparator looks at
    # parsed hostnames).
    _normalize_allowed = field_validator("allowed_domains", mode="before")(
        _normalize_domain_patterns
    )
    _normalize_denied = field_validator("denied_domains", mode="before")(
        _normalize_domain_patterns
    )
    probe_binary_urls: bool = Field(
        default=True,
        description=(
            "When True, fetch_and_extract sends a HEAD request for URLs "
            "that don't have a known download extension to detect "
            "extensionless PDFs / XLSX / DOCX served via Content-Type "
            "or Content-Disposition headers. Adds one round-trip per "
            "fetch but recovers many real-world document URLs. Disable "
            "to skip the probe and rely solely on URL extension."
        ),
    )
    max_pages_per_call: int = 50
    max_chars_per_call: int = 1_000_000
    max_time_per_call_seconds: float = 300.0

    # --- Politeness layer (rate limit + robots.txt) ---
    rate_limit_per_host_rps: float = Field(
        default=2.0,
        description=(
            "Per-host rate cap in requests/second. Set to 0 to disable. "
            "Applies to fetch, download, and search operations."
        ),
    )
    respect_robots_txt: bool = Field(
        default=True,
        description=(
            "If True, fetch and obey each host's robots.txt before "
            "requesting pages. Missing or unreachable robots.txt is "
            "treated as allow-all."
        ),
    )
    robots_user_agent: str = Field(
        default="web-agent-toolkit",
        description=(
            "User-Agent token used when fetching robots.txt and matched "
            "against User-agent rule groups inside it."
        ),
    )

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


class AuditConfig(BaseSettings):
    """Append-only JSONL audit log of every Agent operation.

    Distinct from regular logging: only records public Agent calls
    (start + end + status + elapsed). Useful as a tamper-evident
    audit trail for AI-agent runs, separate from chatty internal logs.
    """

    enabled: bool = False
    audit_log_path: str = "./audit.jsonl"


class CacheConfig(BaseSettings):
    """Disk-backed TTL cache for fetch results and search responses.

    When ``enabled``, every successful ``WebFetcher.fetch(url)`` and
    ``SearchEngine.search(query)`` writes its result to disk; subsequent
    calls within ``ttl_seconds`` return the cached payload without
    hitting the network. Best-effort LRU-by-mtime eviction keeps the
    cache directory under ``max_cache_mb``.

    Disabled by default -- enable explicitly when you want to avoid
    re-fetching the same pages across runs (research workflows,
    experiments, dev iteration).
    """

    enabled: bool = False
    cache_dir: str = "./cache"
    ttl_seconds: float = 3600.0
    max_cache_mb: int = 100


class SkillsConfig(BaseSettings):
    """v1.6.7: Domain Skills registry.

    A "skill" is a markdown file at
    ``<skill_dir>/<domain>/<name>.md`` with YAML frontmatter
    (name, domain, description, inputs, output_schema, runnable) plus
    structured sections (Use case / Recommended flow / Known selectors
    / Known traps). Skills make webTool accumulate reusable knowledge
    about specific websites instead of rediscovering quirks every run.

    Three skill directories with priority order:
      ``builtin`` (lowest) < ``workspace`` < ``project`` (highest)

    Bundled skills (under ``web_agent/builtin_skills/``) ship with a
    Python runner and are dispatchable via
    ``Agent.apply_domain_skill``. User markdown skills are
    informational only unless the workspace mode allows adjacent
    Python helpers.
    """

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the *project-tier* skill load -- when "
            "False, ``skill_dirs`` are not scanned. Workspace and bundled "
            "skills are governed by ``workspace.enabled`` and "
            "``builtin_skills_enabled`` respectively, so "
            "``Agent.get_domain_skills`` can still return entries from "
            "those tiers when this flag is False."
        ),
    )
    skill_dirs: list[str] = Field(
        default_factory=lambda: ["./.webtool-skills"],
        description=(
            "Project skill directories (highest priority). Each entry is "
            "resolved against ``AppConfig.base_dir`` when relative. The "
            "first matching ``(domain, name)`` wins; later entries are "
            "overrides."
        ),
    )
    builtin_skills_enabled: bool = Field(
        default=True,
        description=(
            "Include bundled skills shipped under "
            "``web_agent/builtin_skills/``. Default True -- the bundled "
            "skills are the only runnable skills in v1.6.7. Disable to "
            "audit-only mode where only user-authored skills appear."
        ),
    )


class WorkspaceConfig(BaseSettings):
    """v1.6.7: Agent-editable workspace with safety modes.

    A workspace is a directory the agent reads from and (in some
    modes) writes to. Default layout::

        .webtool-workspace/
            domain-skills/    # user-authored markdown skills (auto-loaded)
            notes/            # agent-authored free-text notes
            helpers.py        # Python helpers (gated by mode)

    Default ``enabled=False`` for safety: the agent must explicitly opt
    in. When enabled, the default mode is ``markdown_skills_only`` --
    the agent can read and write ``.md`` files but cannot execute
    Python.
    """

    enabled: bool = Field(
        default=False,
        description="Master switch. When False, workspace is invisible to Agent.",
    )
    # NB: cannot be named ``path`` -- pydantic-settings on this BaseSettings
    # subclass would pull from the ``PATH`` environment variable (a real,
    # always-set foot-gun on every OS) before applying the field default.
    workspace_dir: str = Field(
        default="./.webtool-workspace",
        description=(
            "Workspace root. Resolved against ``AppConfig.base_dir`` when "
            "relative. Created on first write if missing."
        ),
    )
    mode: Literal[
        "read_only",
        "markdown_skills_only",
        "reviewed_python_helpers",
        "unsafe_python_helpers",
    ] = Field(
        default="markdown_skills_only",
        description=(
            "Safety mode for workspace writes. ``read_only`` blocks all "
            "writes. ``markdown_skills_only`` (default) allows .md files "
            "under domain-skills/ only. ``reviewed_python_helpers`` adds "
            "helpers.py writes but execution requires explicit opt-in. "
            "``unsafe_python_helpers`` removes all restrictions."
        ),
    )
    audit_helper_usage: bool = Field(
        default=True,
        description="Log every workspace write to the audit log when audit is enabled.",
    )
    execute_helpers: bool = Field(
        default=False,
        description=(
            "When mode is ``reviewed_python_helpers`` or "
            "``unsafe_python_helpers``, controls whether ``helpers.py`` is "
            "imported and made available to skills. Default False -- "
            "writing helpers is allowed, executing them is a second opt-in."
        ),
    )


class DiagnosticsConfig(BaseSettings):
    """v1.6.8: Network capture, download-intent capture, post-action
    screenshots, and session replay traces.

    All switches default False to match the v1.6.6/v1.6.7 opt-in posture
    -- existing callers see zero behavior change. Enable individually:

    - ``capture_network=True`` hooks ``page.on(request|response|requestfailed)``
      on every Page created by the Agent and surfaces them as
      ``FetchResult.network_events`` / ``ActionSequenceResult.network_events``.
    - ``capture_download_intents=True`` adds ``page.on("download")`` notification
      (separate from the downloader's explicit ``page.expect_download`` consumer)
      so the URL of any page-triggered download is recorded even when not saved.
    - ``screenshot_after_action=True`` captures a best-effort screenshot
      after every successful action in ``execute_sequence`` to
      ``automation.screenshot_dir`` (paths guarded by ``safe_join_path``).
    - ``trace_enabled=True`` writes a per-session JSONL action log under
      ``trace_dir`` so ``Agent.replay_trace(<file>)`` can re-execute.
    """

    capture_network: bool = Field(
        default=False,
        description=(
            "Hook ``page.on(request|response|requestfailed)`` on every Page "
            "the Agent creates. Off by default; opt in to populate "
            "``FetchResult.network_events`` and ``api_candidates``."
        ),
    )
    max_network_events: int = Field(
        default=500,
        ge=1,
        le=10000,
        description=(
            "Hard cap on retained events per Page. The per-Page deque uses "
            "``maxlen=max_network_events``; oldest events are evicted in "
            "O(1) when the cap is exceeded."
        ),
    )
    network_resource_types: list[str] = Field(
        default_factory=lambda: ["xhr", "fetch", "document"],
        description=(
            "Playwright ``request.resource_type`` values to record. Default "
            "skips image/font/stylesheet/media noise. Set to an empty list "
            "to record all resource types."
        ),
    )
    include_request_headers: bool = Field(
        default=False,
        description=(
            "Capture request headers on each NetworkEvent. Off by default "
            "because headers commonly contain Authorization / Cookie values "
            "an LLM consumer shouldn't see."
        ),
    )
    include_response_headers: bool = Field(
        default=False,
        description="Capture response headers on each NetworkEvent. Off by default.",
    )
    capture_download_intents: bool = Field(
        default=False,
        description=(
            "Attach ``page.on('download')`` as a notification listener and "
            "record the download URL on every Page. The listener also calls "
            "``download.delete()`` so the tmpfile doesn't pile up when no "
            "explicit ``expect_download`` consumer is active."
        ),
    )
    screenshot_after_action: bool = Field(
        default=False,
        description=(
            "When True, ``BrowserActions.execute_sequence`` captures a "
            "best-effort PNG screenshot after each successful action under "
            "``automation.screenshot_dir`` (file name "
            "``verify-<correlation_id>-<index>.png``). Failures are logged "
            "at DEBUG and never fail the sequence."
        ),
    )
    trace_enabled: bool = Field(
        default=False,
        description=(
            "When True, every action executed inside an interactive Session "
            "appends a JSONL entry to "
            "``<trace_dir>/<session_id>.jsonl`` with "
            "``{ts, ordinal, session_id, correlation_id, method, args, "
            "status, elapsed_ms}``. Replayable via ``Agent.replay_trace``."
        ),
    )
    trace_dir: str = Field(
        default="./.webtool-audit/traces",
        description=(
            "Directory for per-session trace files. Resolved against "
            "``AppConfig.base_dir`` when relative. Created on first write."
        ),
    )


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

    # env_nested_delimiter is required for pydantic-settings v2 to parse
    # double-underscore nested env vars like WEB_AGENT_BROWSER__HEADLESS.
    # Without it, only top-level fields like WEB_AGENT_LOG_LEVEL apply --
    # the README + module docstring would silently lie about sub-config
    # support.
    model_config = {
        "env_prefix": "WEB_AGENT_",
        "env_nested_delimiter": "__",
    }

    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    # v1.6.7: domain skills + agent-editable workspace
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    # v1.6.8: network capture, download intents, post-action screenshots,
    # session replay traces
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    log_level: str = "INFO"
    output_dir: str = "./output"
    base_dir: str = Field(default=".", description="Base directory for resolving relative paths")
    ranking_profiles: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "User-defined ranking profiles, merged with the built-in "
            "RANKING_PROFILES (official_sources / docs / research / "
            "news / files). Same shape as the built-in dict: "
            "{name: [host1, host2, ...]}. User-defined profiles "
            "OVERRIDE built-ins on name collision -- callers can "
            "redefine 'docs' for an internal docs portal, for example."
        ),
    )

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
        self.audit.audit_log_path = _resolve(self.audit.audit_log_path)
        self.cache.cache_dir = _resolve(self.cache.cache_dir)
        # v1.6.8: session replay traces live under base_dir by default
        self.diagnostics.trace_dir = _resolve(self.diagnostics.trace_dir)
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
            raise ConfigError(f"Config validation failed for {p}: {exc}") from exc
