"""Pydantic v2 data models for all structured output."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


class SearchResultItem(BaseModel):
    """A single search result from any configured provider (SearXNG / DDGS / Playwright)."""

    position: int = Field(description="1-based rank position in results")
    title: str = Field(description="Result title text")
    url: str = Field(description="Target URL of the result")
    displayed_url: str = Field(default="", description="Green URL shown in snippet")
    snippet: str = Field(default="", description="Description snippet text")
    provider: str = Field(
        default="unknown",
        description=(
            "Search provider that surfaced this result: "
            "'searxng' | 'ddgs' | 'playwright' | 'unknown'. "
            "Populated by SearchEngine; useful for FetchDiagnostic."
        ),
    )


class SearchResponse(BaseModel):
    """Response from a search query (any provider)."""

    query: str = Field(description="Original search query")
    total_results: int = Field(default=0, description="Number of results parsed")
    results: list[SearchResultItem] = Field(default_factory=list)
    searched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    from_cache: bool = Field(
        default=False,
        description="True if this response was served from the local cache.",
    )


class FetchStatus(str, Enum):
    """Status of a fetch or download operation."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    NETWORK_ERROR = "network_error"
    BLOCKED = "blocked"


class NetworkEvent(BaseModel):
    """v1.6.8: a single Playwright network event captured during a page session.

    Surfaced on ``FetchResult.network_events`` and
    ``ActionSequenceResult.network_events`` when
    ``DiagnosticsConfig.capture_network=True``. The collector lives in
    ``web_agent.network_collector.NetworkCollector`` and writes events
    via the ``page.on('request' | 'response' | 'requestfailed')`` hooks.
    """

    event_type: Literal["request", "response", "requestfailed"]
    url: str
    method: str = Field(default="GET")
    resource_type: str = Field(
        default="",
        description=(
            "Playwright ``request.resource_type``: xhr | fetch | document | "
            "script | image | font | stylesheet | media | websocket | ..."
        ),
    )
    status_code: Optional[int] = Field(
        default=None,
        description="HTTP status code (response events only).",
    )
    content_type: Optional[str] = Field(
        default=None,
        description="Response Content-Type header (response events only).",
    )
    request_headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Request headers. Populated only when "
            "``DiagnosticsConfig.include_request_headers=True`` because "
            "Authorization / Cookie values are commonly sensitive."
        ),
    )
    response_headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Response headers. Populated only when "
            "``DiagnosticsConfig.include_response_headers=True``."
        ),
    )
    timing_ms: float = Field(
        default=0.0,
        description="Approximate timing (request->response) when measurable, else 0.",
    )
    failure_text: Optional[str] = Field(
        default=None,
        description=(
            "Playwright failure message (requestfailed events only) -- "
            "e.g. ``net::ERR_NAME_NOT_RESOLVED``."
        ),
    )
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: Optional[str] = Field(default=None)


class FetchResult(BaseModel):
    """Result of fetching a URL, before content extraction."""

    url: str
    final_url: str = Field(description="URL after redirects")
    status_code: Optional[int] = Field(default=None)
    status: FetchStatus
    html: Optional[str] = Field(default=None, description="Raw HTML content")
    binary: Optional[bytes] = Field(
        default=None,
        description=(
            "Raw binary payload for non-HTML resources (PDF, XLSX). "
            "Populated only when the binary fetch path is used (see "
            "Agent.search_and_extract(extract_files=True)). Mutually "
            "exclusive with html: when binary is set, html is None."
        ),
    )
    content_type: Optional[str] = Field(
        default=None,
        description="HTTP Content-Type header captured during binary fetch",
    )
    error_message: Optional[str] = Field(default=None)
    response_time_ms: float = Field(default=0.0)
    correlation_id: Optional[str] = Field(
        default=None, description="Request correlation id for tracing"
    )
    debug_artifacts: list[str] = Field(
        default_factory=list, description="File paths to debug snapshots, if captured"
    )
    from_cache: bool = Field(
        default=False,
        description="True if this fetch was served from the local cache.",
    )
    # v1.6.8: network diagnostics (populated only when
    # DiagnosticsConfig.capture_network=True / capture_download_intents=True)
    network_events: list[NetworkEvent] = Field(
        default_factory=list,
        description=(
            "Per-Page network events captured during the fetch. Empty unless "
            "``DiagnosticsConfig.capture_network=True``."
        ),
    )
    api_candidates: list[str] = Field(
        default_factory=list,
        description=(
            "URLs of XHR/fetch responses with JSON content-type, derived from "
            "``network_events``. De-duplicated, order-preserving."
        ),
    )
    download_candidates_runtime: list[str] = Field(
        default_factory=list,
        description=(
            "URLs the page tried to download during this fetch (via "
            "``page.on('download')`` notification). Empty unless "
            "``DiagnosticsConfig.capture_download_intents=True``. Named "
            "``_runtime`` to disambiguate from ``AgentResult.download_candidates`` "
            "(search-derived)."
        ),
    )


class ExtractionResult(BaseModel):
    """Extracted content from a single web page."""

    url: str = Field(description="URL that was fetched")
    title: Optional[str] = Field(default=None, description="Page title")
    description: Optional[str] = Field(default=None, description="Meta description")
    author: Optional[str] = Field(default=None, description="Author if found")
    date: Optional[str] = Field(default=None, description="Publication date if found")
    sitename: Optional[str] = Field(default=None, description="Site name")
    content: Optional[str] = Field(default=None, description="Main text content")
    markdown: Optional[str] = Field(
        default=None,
        description=(
            "Markdown rendering of the page (only populated when "
            "trafilatura succeeds with output_format='markdown'). "
            "Useful for LLM consumption -- preserves headings, lists, "
            "links, and emphasis without HTML noise."
        ),
    )
    language: Optional[str] = Field(default=None, description="Detected language")
    extraction_method: str = Field(
        default="none",
        description="Which extractor succeeded: trafilatura|bs4|raw|none",
    )
    content_length: int = Field(default=0, description="Character count of extracted content")
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: Optional[str] = Field(default=None)


class CdpConnectionInfo(BaseModel):
    """v1.6.10: structured CDP connection bundle for ``remote_cdp`` siblings.

    Returned by :meth:`Agent.get_owned_cdp_connection_info`. Lets a
    launching Agent hand a single object to a co-resident ``remote_cdp``
    Agent so the latter can attach to the same browser without having
    to call three separate ``BrowserManager`` getters.

    Use the values verbatim:

    - ``cdp_url`` -> :attr:`BrowserConfig.remote_cdp_url`
    - ``profile_dir`` -> :attr:`BrowserConfig.remote_cdp_profile_dir`
    - ``ownership_token`` -> :attr:`BrowserConfig.remote_cdp_ownership_token`
    """

    cdp_url: str = Field(description="ws:// CDP endpoint of the launched browser")
    profile_dir: str = Field(description="Absolute user-data-dir path of the launched browser")
    ownership_token: str = Field(
        description="64-char hex ownership token at <profile_dir>/.webtool-ownership"
    )


class DownloadResult(BaseModel):
    """Result of a file download."""

    url: str
    filepath: str = Field(description="Local path where file was saved")
    filename: str
    size_bytes: int = Field(default=0)
    content_type: Optional[str] = Field(default=None)
    status: FetchStatus
    error_message: Optional[str] = Field(default=None)
    correlation_id: Optional[str] = Field(default=None)
    debug_artifacts: list[str] = Field(default_factory=list)


class ToolSeverity(str, Enum):
    """Severity level for ToolWarning / ToolError."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class ToolMessage(BaseModel):
    """Structured non-fatal message from any Agent operation.

    Lets agentic callers branch on a stable error code instead of
    parsing English-language strings. Fields:

    - ``code``: stable, lowercase, snake_case identifier (e.g.
      ``"domain_blocked"``, ``"download_skipped"``, ``"binary_size_cap"``).
    - ``message``: human-readable description.
    - ``url``: optional URL the message is about.
    - ``severity``: enum from :class:`ToolSeverity`.

    Used as the row type for both ``structured_warnings`` (severity
    INFO/WARNING) and ``structured_errors`` (severity ERROR/FATAL).
    The legacy ``errors``/``warnings`` string lists are populated from
    these via ``.message`` for backward compatibility.
    """

    code: str = Field(description="Stable, snake_case identifier")
    message: str = Field(description="Human-readable description")
    url: Optional[str] = Field(default=None, description="URL the message is about, if any")
    severity: ToolSeverity = Field(default=ToolSeverity.WARNING)


# Aliases preserved for clarity at call sites; both point to ToolMessage.
ToolWarning = ToolMessage
ToolError = ToolMessage


class FetchDiagnostic(BaseModel):
    """Per-URL fetch outcome surfaced by AgentResult / ResearchResult.

    Lets callers programmatically inspect *why* each URL succeeded or
    failed without parsing free-form error strings. One diagnostic is
    emitted per URL the pipeline considered (including blocked / skipped
    ones), in the order the pipeline saw them.
    """

    url: str
    final_url: Optional[str] = Field(
        default=None, description="URL after redirects (None if never fetched)"
    )
    status: FetchStatus
    status_code: Optional[int] = Field(default=None)
    provider: str = Field(
        default="unknown",
        description="Search provider that surfaced the URL, or 'direct' for caller-supplied",
    )
    block_reason: Optional[str] = Field(
        default=None,
        description=(
            "Reason the URL was not extracted, when applicable: "
            "'domain_blocked' | 'robots_disallowed' | 'rate_limited' | "
            "'timeout' | 'http_error' | 'network_error' | "
            "'download_skipped' | None"
        ),
    )
    content_length: int = Field(
        default=0,
        description="Character count of extracted content, 0 if extraction did not run",
    )
    response_time_ms: float = Field(default=0.0)
    from_cache: bool = Field(default=False)


class AgentResult(BaseModel):
    """Full pipeline result: search + fetch + extract."""

    query: str
    search: SearchResponse
    pages: list[ExtractionResult] = Field(default_factory=list)
    errors: list[str] = Field(
        default_factory=list,
        description=(
            "Fatal issues that prevent the call from being usable: "
            "'No search results found', 'all fetches failed', etc. "
            "If non-empty the caller should treat the call as failed."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal issues that did not block the overall pipeline: "
            "'domain blocked', 'skipped download URL', 'partial fetch'. "
            "Informational only -- the call still produced usable output."
        ),
    )
    download_candidates: list[SearchResultItem] = Field(
        default_factory=list,
        description=(
            "Search results that point to downloadable files (PDF/XLSX/DOC/etc.) "
            "and were skipped by the HTML extraction pipeline. Pass each url to "
            "Agent.download(), or call search_and_extract(extract_files=True) to "
            "extract their text inline."
        ),
    )
    diagnostics: list[FetchDiagnostic] = Field(
        default_factory=list,
        description="Per-URL fetch outcomes (status, provider, block_reason, length).",
    )
    structured_warnings: list[ToolMessage] = Field(
        default_factory=list,
        description=(
            "Structured form of ``warnings`` -- each entry has a stable "
            "``code``, human ``message``, optional ``url``, and ``severity``. "
            "Lets agentic callers branch on the code instead of parsing "
            "the legacy string list."
        ),
    )
    structured_errors: list[ToolMessage] = Field(
        default_factory=list,
        description="Structured form of ``errors`` -- same shape as ``structured_warnings``.",
    )
    total_time_ms: float = Field(default=0.0)
    correlation_id: Optional[str] = Field(default=None)


# =========================================================================
# Browser Automation Models
# =========================================================================


class ActionType(str, Enum):
    """Types of browser automation actions."""

    CLICK = "click"
    TYPE = "type"
    FILL = "fill"
    SCROLL = "scroll"
    SCREENSHOT = "screenshot"
    NAVIGATE = "navigate"
    DIALOG = "dialog"
    HOVER = "hover"
    SELECT = "select"
    KEYBOARD = "keyboard"
    WAIT = "wait"
    EVALUATE = "evaluate"
    # v1.6.6: coordinate-level fallbacks for when selectors fail
    # (canvas apps, shadow DOM, cross-origin iframes, visual-only controls)
    CLICK_XY = "click_xy"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    # v1.6.7: interaction-skill library actions
    UPLOAD_FILE = "upload_file"
    IFRAME_CLICK = "iframe_click"
    SHADOW_DOM_CLICK = "shadow_dom_click"
    DRAG_AND_DROP = "drag_and_drop"


class ActionStatus(str, Enum):
    """Status of an individual action execution."""

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class MouseButton(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class ScrollDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


class NavigateDirection(str, Enum):
    GOTO = "goto"
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"


class DialogResponse(str, Enum):
    ACCEPT = "accept"
    DISMISS = "dismiss"


class WaitTarget(str, Enum):
    SELECTOR = "selector"
    TEXT = "text"
    URL = "url"
    NETWORK_IDLE = "network_idle"
    LOAD_STATE = "load_state"
    FUNCTION = "function"


class ScreenshotFormat(str, Enum):
    PNG = "png"
    JPEG = "jpeg"


# ---------------------------------------------------------------------------
# Semantic Locators (Phase 4)
# ---------------------------------------------------------------------------


class LocatorSpec(BaseModel):
    """Semantic locator using Playwright's role/text/label/test_id APIs.

    Provides AI-friendly element selection beyond raw CSS. At least one
    locator field must be set. Resolution priority (first non-None wins):
    role > test_id > label > placeholder > text > selector.

    Examples::

        # Find a button by accessible name:
        LocatorSpec(role="button", role_name="Submit")

        # Find an input by label:
        LocatorSpec(label="Customer name:")

        # Find by data-testid:
        LocatorSpec(test_id="login-form")

        # Fall back to a CSS selector:
        LocatorSpec(selector="button.primary")
    """

    selector: Optional[str] = Field(default=None, description="CSS selector")
    role: Optional[str] = Field(
        default=None,
        description="ARIA role: 'button', 'link', 'textbox', 'checkbox', etc.",
    )
    role_name: Optional[str] = Field(default=None, description="Accessible name filter for role")
    text: Optional[str] = Field(default=None, description="Visible text match")
    label: Optional[str] = Field(default=None, description="Form label association")
    placeholder: Optional[str] = Field(default=None, description="Placeholder attribute")
    test_id: Optional[str] = Field(default=None, description="data-testid value")

    def is_empty(self) -> bool:
        return not any(
            (
                self.selector,
                self.role,
                self.text,
                self.label,
                self.placeholder,
                self.test_id,
            )
        )


# Type alias for action selector fields. Pydantic v2 accepts either a plain
# string (existing CSS selector behavior) or a LocatorSpec dict, dispatching
# automatically. Existing JSON callers continue to work unchanged.
SelectorLike = Union[str, LocatorSpec]


# ---------------------------------------------------------------------------
# Action Input Models (discriminated union on 'action' field)
# ---------------------------------------------------------------------------


class BaseAction(BaseModel):
    """Common ancestor for every action input.

    v1.6.6 introduces ``tab_id`` -- an optional pointer used by the
    BrowserActions layer to route an action at a specific tab within a
    session. ``tab_id=None`` (default) preserves v1.6.5 behavior: the
    action runs against the session's current tab (or an ephemeral page
    when no session is set).

    The Pydantic v2 discriminated union (``Field(discriminator="action")``)
    dispatches on the ``action: Literal[...]`` field, not on class
    identity -- so adding this parent is transparent to existing JSON
    callers and to ``TypeAdapter[Action]`` parsing.
    """

    tab_id: Optional[str] = Field(
        default=None,
        description=(
            "Target tab for this action within the session. None = use "
            "the session's current tab. Ignored when no session_id is set."
        ),
    )


class ClickInput(BaseAction):
    """Click an element by CSS selector or semantic locator."""

    action: Literal["click"] = "click"
    selector: SelectorLike = Field(description="CSS selector or LocatorSpec for the target element")
    timeout: Optional[int] = Field(default=None, description="Override timeout in ms")
    button: MouseButton = Field(default=MouseButton.LEFT)
    double_click: bool = Field(default=False)
    modifiers: list[str] = Field(
        default_factory=list, description="Modifier keys: Shift, Control, Alt, Meta"
    )


class TypeInput(BaseAction):
    """Type text into an element keystroke-by-keystroke."""

    action: Literal["type"] = "type"
    selector: SelectorLike = Field(description="CSS selector or LocatorSpec for the input element")
    timeout: Optional[int] = Field(default=None)
    text: str = Field(description="Text to type")
    delay: int = Field(default=0, description="Delay in ms between key presses")
    clear_first: bool = Field(default=False, description="Clear field before typing")


class FillInput(BaseAction):
    """Fill an input element with a value (instant, no keystrokes)."""

    action: Literal["fill"] = "fill"
    selector: SelectorLike = Field(description="CSS selector or LocatorSpec for the input element")
    timeout: Optional[int] = Field(default=None)
    value: str = Field(description="Value to fill")


class ScrollInput(BaseAction):
    """Scroll the page or an element."""

    action: Literal["scroll"] = "scroll"
    selector: Optional[SelectorLike] = Field(
        default=None, description="Element to scroll into view (CSS or LocatorSpec)"
    )
    timeout: Optional[int] = Field(default=None)
    direction: ScrollDirection = Field(default=ScrollDirection.DOWN)
    amount: int = Field(default=3, description="Scroll ticks")
    infinite_scroll: bool = Field(default=False, description="Auto-scroll until no new content")
    infinite_scroll_max: int = Field(default=10, description="Max iterations for infinite scroll")
    infinite_scroll_delay_ms: int = Field(default=1000)


class ScreenshotInput(BaseAction):
    """Take a screenshot of the page or a specific element."""

    action: Literal["screenshot"] = "screenshot"
    selector: Optional[SelectorLike] = Field(
        default=None,
        description="Element to screenshot (CSS or LocatorSpec). None for full page.",
    )
    timeout: Optional[int] = Field(default=None)
    path: Optional[str] = Field(
        default=None, description="Output file path (auto-generated if None)"
    )
    full_page: bool = Field(default=False)
    format: ScreenshotFormat = Field(default=ScreenshotFormat.PNG)
    quality: Optional[int] = Field(default=None, description="JPEG quality 0-100")


class NavigateInput(BaseAction):
    """Navigate to a URL or go back/forward/reload."""

    action: Literal["navigate"] = "navigate"
    selector: Optional[str] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    url: Optional[str] = Field(default=None, description="URL to navigate to (for goto)")
    navigate_action: NavigateDirection = Field(default=NavigateDirection.GOTO)
    wait_until: str = Field(default="networkidle")


class DialogInput(BaseAction):
    """Configure how to handle the next browser dialog (alert/confirm/prompt)."""

    action: Literal["dialog"] = "dialog"
    selector: Optional[str] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    dialog_action: DialogResponse = Field(default=DialogResponse.ACCEPT)
    prompt_text: Optional[str] = Field(default=None, description="Text for prompt dialogs")


class HoverInput(BaseAction):
    """Hover over an element."""

    action: Literal["hover"] = "hover"
    selector: SelectorLike = Field(description="CSS selector or LocatorSpec for the target element")
    timeout: Optional[int] = Field(default=None)


class SelectInput(BaseAction):
    """Select an option from a dropdown."""

    action: Literal["select"] = "select"
    selector: SelectorLike = Field(
        description="CSS selector or LocatorSpec for the <select> element"
    )
    timeout: Optional[int] = Field(default=None)
    value: Optional[str] = Field(default=None, description="Option value attribute")
    label: Optional[str] = Field(default=None, description="Option visible text")
    index: Optional[int] = Field(default=None, description="Option index (0-based)")


class KeyboardInput(BaseAction):
    """Press a key or key combination."""

    action: Literal["keyboard"] = "keyboard"
    selector: Optional[str] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    key: str = Field(description="Key name or combo: 'Enter', 'Control+A', 'ArrowDown'")
    repeat: int = Field(default=1, description="Number of times to press")


class WaitInput(BaseAction):
    """Wait for a condition to be met."""

    action: Literal["wait"] = "wait"
    selector: Optional[str] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    target: WaitTarget = Field(default=WaitTarget.SELECTOR)
    value: Optional[str] = Field(
        default=None,
        description="Selector, URL pattern, text, load state, or JS function body",
    )
    state: str = Field(
        default="visible", description="For selector waits: visible|hidden|attached|detached"
    )


class EvaluateInput(BaseAction):
    """Evaluate a JavaScript expression in the page context."""

    action: Literal["evaluate"] = "evaluate"
    selector: Optional[str] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    expression: str = Field(description="JavaScript expression to evaluate")


# v1.6.6: Coordinate-level fallback actions (Feature 4)
# Useful when selectors fail: canvas apps, shadow DOM, cross-origin iframes,
# custom dropdowns, visual-only controls. Coord clicks bypass the
# _looks_like_submit heuristic -- there is no selector to inspect.


class ClickXYInput(BaseAction):
    """Click at viewport coordinates (CSS pixels, not device pixels).

    Used after :meth:`Agent.observe` returns ``device_pixel_ratio`` so
    callers can map screenshot pixels to click coordinates safely.
    """

    action: Literal["click_xy"] = "click_xy"
    x: float = Field(description="Viewport X coordinate (CSS pixels)")
    y: float = Field(description="Viewport Y coordinate (CSS pixels)")
    button: MouseButton = Field(default=MouseButton.LEFT)
    clicks: int = Field(default=1, ge=1, le=3, description="1=single, 2=double, 3=triple")
    delay: int = Field(default=0, description="ms between mousedown and mouseup")
    timeout: Optional[int] = Field(default=None)


class TypeTextInput(BaseAction):
    """Type text into whatever currently has keyboard focus.

    No selector resolution -- the page's current focus owns the keystrokes.
    Pair with a preceding click or focus action to direct the input.
    """

    action: Literal["type_text"] = "type_text"
    text: str = Field(description="Text to type into the current focus target")
    delay: int = Field(default=0, description="ms between key presses")


class PressKeyInput(BaseAction):
    """Press a single key (or key+modifiers combo) at page level.

    Like ``KeyboardInput`` but never resolves a selector -- the keypress
    is sent to whatever currently has keyboard focus.
    """

    action: Literal["press_key"] = "press_key"
    key: str = Field(description="Key name: 'Enter', 'Tab', 'ArrowDown', 'a', etc.")
    modifiers: list[str] = Field(
        default_factory=list,
        description="Modifier keys: 'Shift', 'Control', 'Alt', 'Meta'",
    )


# v1.6.7: Interaction-skill library Action types (Feature 5)
# Convenience surfaces for common patterns: file upload, iframe / shadow-DOM
# click, drag-and-drop. All inherit BaseAction so ``tab_id`` routing works.


class UploadFileInput(BaseAction):
    """Upload one or more files via an ``<input type="file">`` element.

    Calls Playwright's ``Locator.set_input_files``. Paths are validated
    against ``SafetyConfig`` -- by default callers may only upload files
    that live under ``download.download_dir`` to prevent prompt-injection
    from exfiltrating arbitrary files like ``~/.ssh/id_rsa``. Flip
    ``safety.allow_upload_outside_download_dir=True`` to opt in.
    """

    action: Literal["upload_file"] = "upload_file"
    selector: SelectorLike = Field(description="CSS selector or LocatorSpec for the file input")
    paths: list[str] = Field(description="Files to upload")
    timeout: Optional[int] = Field(default=None)


class IframeClickInput(BaseAction):
    """Click a button inside an iframe via Playwright's frame_locator.

    Required when the target lives in a same-origin iframe (Google
    consent dialog, payment provider widgets, embedded calendars).
    """

    action: Literal["iframe_click"] = "iframe_click"
    iframe_selector: str = Field(description="CSS selector for the <iframe> element")
    inner_selector: str = Field(description="CSS selector inside the iframe document")
    timeout: Optional[int] = Field(default=None)


class ShadowDomClickInput(BaseAction):
    """Click an element inside a shadow DOM tree.

    Playwright pierces shadow DOM automatically for CSS selectors when
    composed with the ``>>`` combinator. Pass the shadow-host selector
    and the inner-tree selector separately for clarity.
    """

    action: Literal["shadow_dom_click"] = "shadow_dom_click"
    host_selector: str = Field(description="CSS selector for the shadow host")
    inner_selector: str = Field(description="CSS selector for the target inside the shadow root")
    timeout: Optional[int] = Field(default=None)


class DragAndDropInput(BaseAction):
    """Drag an element from one selector and drop it on another.

    Calls Playwright's ``Page.drag_and_drop``.
    """

    action: Literal["drag_and_drop"] = "drag_and_drop"
    source: SelectorLike = Field(description="CSS selector or LocatorSpec for the source element")
    target: SelectorLike = Field(description="CSS selector or LocatorSpec for the drop target")
    timeout: Optional[int] = Field(default=None)


# Discriminated union of all action types
Action = Annotated[
    Union[
        ClickInput,
        TypeInput,
        FillInput,
        ScrollInput,
        ScreenshotInput,
        NavigateInput,
        DialogInput,
        HoverInput,
        SelectInput,
        KeyboardInput,
        WaitInput,
        EvaluateInput,
        ClickXYInput,
        TypeTextInput,
        PressKeyInput,
        # v1.6.7 interaction-skill library
        UploadFileInput,
        IframeClickInput,
        ShadowDomClickInput,
        DragAndDropInput,
    ],
    Field(discriminator="action"),
]


# ---------------------------------------------------------------------------
# Action Result Models
# ---------------------------------------------------------------------------


class ActionResult(BaseModel):
    """Result of a single browser action."""

    action: ActionType
    status: ActionStatus
    selector: Optional[str] = Field(default=None)
    duration_ms: float = Field(default=0.0)
    error_message: Optional[str] = Field(default=None)
    data: Optional[dict[str, Any]] = Field(default=None, description="Action-specific return data")
    debug_artifacts: list[str] = Field(default_factory=list)


class ActionSequenceResult(BaseModel):
    """Result of executing a sequence of browser actions."""

    url: str
    actions_total: int = Field(default=0)
    actions_succeeded: int = Field(default=0)
    actions_failed: int = Field(default=0)
    results: list[ActionResult] = Field(default_factory=list)
    total_time_ms: float = Field(default=0.0)
    correlation_id: Optional[str] = Field(default=None)
    debug_artifacts: list[str] = Field(default_factory=list)
    # v1.6.8: network diagnostics (populated only when
    # DiagnosticsConfig.capture_network=True / capture_download_intents=True)
    network_events: list[NetworkEvent] = Field(
        default_factory=list,
        description=(
            "Per-Page network events captured during the sequence. Empty "
            "unless ``DiagnosticsConfig.capture_network=True``."
        ),
    )
    api_candidates: list[str] = Field(
        default_factory=list,
        description=(
            "XHR/fetch JSON response URLs observed during the sequence. "
            "Derived from ``network_events``."
        ),
    )
    download_candidates: list[str] = Field(
        default_factory=list,
        description=(
            "URLs the page tried to download via ``page.on('download')``. "
            "Empty unless ``DiagnosticsConfig.capture_download_intents=True``."
        ),
    )
    verification_screenshots: list[str] = Field(
        default_factory=list,
        description=(
            "v1.6.8: one PNG path per successful action when "
            "``DiagnosticsConfig.screenshot_after_action=True``. May be "
            "shorter than ``results`` (failed actions skip the screenshot)."
        ),
    )


class ScreenshotResult(BaseModel):
    """Result of a screenshot operation."""

    url: str
    path: str
    format: ScreenshotFormat
    size_bytes: int = Field(default=0)
    status: ActionStatus
    error_message: Optional[str] = Field(
        default=None,
        description=(
            "Failure reason when status != SUCCESS (domain blocked, "
            "redirected to disallowed host, path traversal rejected, etc.)"
        ),
    )
    correlation_id: Optional[str] = Field(default=None)


# =========================================================================
# Browser Sessions (Phase 5)
# =========================================================================


class SessionInfo(BaseModel):
    """Metadata for a persistent browser session.

    Sessions retain cookies, localStorage, and origin tokens across multiple
    Agent method calls. Created via :meth:`Agent.create_session` and
    referenced by ``session_id`` in subsequent fetch/download/screenshot/
    interact calls.
    """

    session_id: str
    name: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    page_count: int = Field(default=0)
    user_agent: Optional[str] = None


# =========================================================================
# High-Level Recipe Results (Phase 6)
# =========================================================================


class Citation(BaseModel):
    """A research citation: URL plus extracted title/snippet and relevance score."""

    url: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    extraction_method: str = Field(default="none")
    relevance_score: float = Field(
        default=0.0,
        description="Score from the recipe ranker, higher is better",
    )


class ResearchResult(BaseModel):
    """Result of the multi-page web_research recipe.

    Returned by :meth:`Agent.web_research` and the matching MCP tool.
    """

    query: str
    citations: list[Citation] = Field(default_factory=list)
    summary_pages: list[ExtractionResult] = Field(default_factory=list)
    pages_visited: int = Field(default=0)
    chars_extracted: int = Field(default=0)
    errors: list[str] = Field(
        default_factory=list,
        description="Fatal issues that prevent the research call from being usable.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal issues (blocked domains, skipped downloads, partial fetches).",
    )
    download_candidates: list[SearchResultItem] = Field(
        default_factory=list,
        description="Downloadable file URLs surfaced by the search but skipped by extraction.",
    )
    diagnostics: list[FetchDiagnostic] = Field(
        default_factory=list,
        description="Per-URL fetch outcomes for the URLs the recipe attempted.",
    )
    structured_warnings: list[ToolMessage] = Field(
        default_factory=list,
        description="Structured form of ``warnings`` (code/message/url/severity).",
    )
    structured_errors: list[ToolMessage] = Field(
        default_factory=list,
        description="Structured form of ``errors`` (code/message/url/severity).",
    )
    correlation_id: Optional[str] = None
    total_time_ms: float = Field(default=0.0)


# =========================================================================
# Form-Filter Recipe (Phase 7 / v1.6.1)
# =========================================================================


class FormFilterSpec(BaseModel):
    """Declarative spec for filling a search/filter form before extracting content.

    Used by :meth:`Agent.fill_form_and_extract` to drive dynamic calendar
    pages (regulator filings, conference schedules, event listings) where
    content is gated behind a search box and/or filter controls. The
    caller supplies semantic locators; the recipe runs the actions and
    returns the extracted post-submit content.
    """

    query_selector: Optional[SelectorLike] = Field(
        default=None,
        description="Search-box locator. Skipped when None or query_value is None.",
    )
    query_value: Optional[str] = Field(
        default=None,
        description="Text to fill into query_selector.",
    )
    filters: list[tuple[SelectorLike, str]] = Field(
        default_factory=list,
        description=(
            "Ordered list of (locator, value) pairs to fill before submit. "
            "Locator may resolve to a <select>, <input>, or any focus-able "
            "control; the recipe auto-detects element type."
        ),
    )
    submit_selector: Optional[SelectorLike] = Field(
        default=None,
        description="Submit-button locator. When None, the recipe presses Enter on the query input.",
    )
    wait_for: Optional[SelectorLike] = Field(
        default=None,
        description="Locator that must appear after submit before extraction runs.",
    )
    wait_timeout_ms: int = Field(
        default=15000,
        description="Maximum time (ms) to wait for wait_for to appear.",
    )


# ---------------------------------------------------------------------------
# v1.6.6: Tab management (Feature 3)
# ---------------------------------------------------------------------------


class TabInfo(BaseModel):
    """Snapshot of one tab within a browser session.

    A tab is a single Playwright Page hosted inside the session's
    BrowserContext. The ``tab_id`` is opaque and stable for the lifetime
    of the page; popups opened by the page itself are auto-registered
    with a generated tab_id but do NOT become the session's current tab
    until an explicit ``switch_tab`` call.
    """

    tab_id: str = Field(description="Opaque per-session tab identifier")
    url: str = Field(default="", description="Current URL of the tab")
    title: Optional[str] = Field(
        default=None, description="Document title (may lag the URL during navigation)"
    )
    active: bool = Field(
        default=False,
        description="True iff this tab is the session's current target for new actions",
    )
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# v1.6.6: Observe mode (Feature 5)
# ---------------------------------------------------------------------------


class ObserveResult(BaseModel):
    """Snapshot of a page's visual + structural state for observe -> act -> verify loops.

    Coordinate-click callers MUST honor ``device_pixel_ratio`` when
    translating screenshot pixels to click coordinates. The viewport
    dimensions are CSS pixels (what Playwright's mouse API expects);
    multiply by DPR to map from a hi-DPI screenshot.
    """

    url: str = Field(description="URL captured (post-redirect)")
    title: Optional[str] = Field(default=None)
    screenshot_path: str = Field(description="Absolute path to the captured PNG")
    viewport_width: int = Field(description="CSS pixels")
    viewport_height: int = Field(description="CSS pixels")
    page_width: int = Field(description="Full document width in CSS pixels")
    page_height: int = Field(description="Full document height in CSS pixels")
    scroll_x: int = Field(description="Current horizontal scroll offset (CSS pixels)")
    scroll_y: int = Field(description="Current vertical scroll offset (CSS pixels)")
    device_pixel_ratio: float = Field(
        description=(
            "window.devicePixelRatio. Multiply CSS pixels by DPR to get screenshot pixels."
        )
    )
    visible_text: Optional[str] = Field(
        default=None,
        description=("Truncated document.body.innerText. None when include_text=False."),
    )
    aria_snapshot: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Accessibility tree. None by default (snapshots can be megabytes "
            "on complex pages); enable with include_aria=True."
        ),
    )
    tab_id: Optional[str] = Field(default=None)
    session_id: Optional[str] = Field(default=None)
    correlation_id: Optional[str] = Field(default=None)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# v1.6.6: Doctor command (Feature 6)
# ---------------------------------------------------------------------------


class DoctorCheck(BaseModel):
    """Result of one diagnostic probe."""

    name: str = Field(description="Probe identifier, e.g. 'chromium_installed'")
    status: Literal["ok", "warn", "fail", "skip"] = Field(
        description=(
            "ok = working; warn = soft missing (optional feature); "
            "fail = required component broken; skip = probe not applicable."
        )
    )
    message: str = Field(default="", description="Human-readable diagnostic message")
    duration_ms: float = Field(default=0.0)


class DoctorReport(BaseModel):
    """Aggregated diagnostic report from ``Agent.doctor()``."""

    summary: Literal["healthy", "usable_with_warnings", "unusable"] = Field(
        description=(
            "healthy = all checks ok; usable_with_warnings = some warns but no "
            "fails; unusable = at least one fail."
        )
    )
    web_agent_version: str
    python_version: str
    platform: str
    checks: list[DoctorCheck] = Field(default_factory=list)
    total_duration_ms: float = Field(default=0.0)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# v1.6.7: Domain Skills + Workspace (Features 1+2+3+4)
# ---------------------------------------------------------------------------


class SkillInputSpec(BaseModel):
    """One input field declared by a domain skill's YAML frontmatter.

    The skill author lists the inputs the skill expects under ``inputs:``
    in the frontmatter; each maps to one ``SkillInputSpec``.
    """

    type: Literal["str", "int", "float", "bool"] = Field(
        default="str", description="Pydantic-friendly scalar type"
    )
    required: bool = Field(default=False)
    default: Any = Field(default=None, description="Default value when not provided")
    description: Optional[str] = Field(default=None)


class DomainSkill(BaseModel):
    """A parsed markdown skill file describing how to handle a specific domain.

    Loaded by :class:`SkillRegistry` from one of three directories
    (priority: project > workspace > builtin). The frontmatter section
    populates the structured fields; the markdown body populates the
    free-text sections (use_case, recommended_flow, etc.).

    Bundled skills (``source="builtin"``) are runnable via
    :meth:`Agent.apply_domain_skill` -- they ship with a Python
    implementation alongside the markdown. User markdown skills are
    informational only unless the workspace mode permits adjacent Python.
    """

    name: str = Field(description="Skill name, unique within a domain")
    domain: str = Field(description="Host suffix this skill targets (e.g. 'sec.gov')")
    description: str = Field(description="One-line summary")
    runnable: bool = Field(
        default=False,
        description=(
            "True only for bundled skills with a Python runner. User "
            "markdown-only skills are informational; apply_domain_skill "
            "raises SkillNotRunnableError for them."
        ),
    )
    inputs: dict[str, SkillInputSpec] = Field(default_factory=dict)
    output_schema: dict[str, str] = Field(
        default_factory=dict,
        description="Type names per output field (e.g. {'filing_url': 'str'})",
    )
    # Free-text sections parsed from the markdown body
    use_case: Optional[str] = Field(default=None)
    recommended_flow: list[str] = Field(
        default_factory=list, description="Numbered steps from the '## Recommended flow' section"
    )
    known_selectors: dict[str, str] = Field(
        default_factory=dict,
        description="Selector hints parsed from '## Known selectors' bullets (label: selector)",
    )
    known_traps: list[str] = Field(
        default_factory=list,
        description="Bullet items from '## Known traps' (warnings to surface to the consumer)",
    )
    output_expectation: Optional[str] = Field(default=None)
    # Provenance
    source: Literal["builtin", "workspace", "project"] = Field(
        description="Which directory the skill came from"
    )
    source_path: str = Field(description="Absolute path to the .md file")


class SkillApplicationResult(BaseModel):
    """Result of running a bundled domain skill against a live URL."""

    skill_name: str
    domain: str
    url: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    succeeded: bool = Field(default=False)
    errors: list[ToolError] = Field(default_factory=list)
    warnings: list[ToolWarning] = Field(default_factory=list)
    correlation_id: Optional[str] = Field(default=None)
    duration_ms: float = Field(default=0.0)


# (interaction-library Action types are defined above, before the Action
# discriminated union -- see UploadFileInput / IframeClickInput /
# ShadowDomClickInput / DragAndDropInput around the BaseAction block.)
