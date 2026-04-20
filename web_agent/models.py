"""Pydantic v2 data models for all structured output."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


class SearchResultItem(BaseModel):
    """A single Google search result."""

    position: int = Field(description="1-based rank position in results")
    title: str = Field(description="Result title text")
    url: str = Field(description="Target URL of the result")
    displayed_url: str = Field(default="", description="Green URL shown in snippet")
    snippet: str = Field(default="", description="Description snippet text")


class SearchResponse(BaseModel):
    """Response from a Google search query."""

    query: str = Field(description="Original search query")
    total_results: int = Field(default=0, description="Number of results parsed")
    results: list[SearchResultItem] = Field(default_factory=list)
    searched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class FetchStatus(str, Enum):
    """Status of a fetch or download operation."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    NETWORK_ERROR = "network_error"
    BLOCKED = "blocked"


class FetchResult(BaseModel):
    """Result of fetching a URL, before content extraction."""

    url: str
    final_url: str = Field(description="URL after redirects")
    status_code: Optional[int] = Field(default=None)
    status: FetchStatus
    html: Optional[str] = Field(default=None, description="Raw HTML content")
    error_message: Optional[str] = Field(default=None)
    response_time_ms: float = Field(default=0.0)


class ExtractionResult(BaseModel):
    """Extracted content from a single web page."""

    url: str = Field(description="URL that was fetched")
    title: Optional[str] = Field(default=None, description="Page title")
    description: Optional[str] = Field(default=None, description="Meta description")
    author: Optional[str] = Field(default=None, description="Author if found")
    date: Optional[str] = Field(default=None, description="Publication date if found")
    sitename: Optional[str] = Field(default=None, description="Site name")
    content: Optional[str] = Field(default=None, description="Main text content")
    language: Optional[str] = Field(default=None, description="Detected language")
    extraction_method: str = Field(
        default="none",
        description="Which extractor succeeded: trafilatura|bs4|raw|none",
    )
    content_length: int = Field(
        default=0, description="Character count of extracted content"
    )
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
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


class AgentResult(BaseModel):
    """Full pipeline result: search + fetch + extract."""

    query: str
    search: SearchResponse
    pages: list[ExtractionResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    total_time_ms: float = Field(default=0.0)


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
# Action Input Models (discriminated union on 'action' field)
# ---------------------------------------------------------------------------


class ClickInput(BaseModel):
    """Click an element by CSS selector."""

    action: Literal["click"] = "click"
    selector: str = Field(description="CSS selector of element to click")
    timeout: Optional[int] = Field(default=None, description="Override timeout in ms")
    button: MouseButton = Field(default=MouseButton.LEFT)
    double_click: bool = Field(default=False)
    modifiers: list[str] = Field(
        default_factory=list, description="Modifier keys: Shift, Control, Alt, Meta"
    )


class TypeInput(BaseModel):
    """Type text into an element keystroke-by-keystroke."""

    action: Literal["type"] = "type"
    selector: str = Field(description="CSS selector of input element")
    timeout: Optional[int] = Field(default=None)
    text: str = Field(description="Text to type")
    delay: int = Field(default=0, description="Delay in ms between key presses")
    clear_first: bool = Field(default=False, description="Clear field before typing")


class FillInput(BaseModel):
    """Fill an input element with a value (instant, no keystrokes)."""

    action: Literal["fill"] = "fill"
    selector: str = Field(description="CSS selector of input element")
    timeout: Optional[int] = Field(default=None)
    value: str = Field(description="Value to fill")


class ScrollInput(BaseModel):
    """Scroll the page or an element."""

    action: Literal["scroll"] = "scroll"
    selector: Optional[str] = Field(default=None, description="Element to scroll into view")
    timeout: Optional[int] = Field(default=None)
    direction: ScrollDirection = Field(default=ScrollDirection.DOWN)
    amount: int = Field(default=3, description="Scroll ticks")
    infinite_scroll: bool = Field(default=False, description="Auto-scroll until no new content")
    infinite_scroll_max: int = Field(default=10, description="Max iterations for infinite scroll")
    infinite_scroll_delay_ms: int = Field(default=1000)


class ScreenshotInput(BaseModel):
    """Take a screenshot of the page or a specific element."""

    action: Literal["screenshot"] = "screenshot"
    selector: Optional[str] = Field(default=None, description="Element to screenshot (or full page)")
    timeout: Optional[int] = Field(default=None)
    path: Optional[str] = Field(default=None, description="Output file path (auto-generated if None)")
    full_page: bool = Field(default=False)
    format: ScreenshotFormat = Field(default=ScreenshotFormat.PNG)
    quality: Optional[int] = Field(default=None, description="JPEG quality 0-100")


class NavigateInput(BaseModel):
    """Navigate to a URL or go back/forward/reload."""

    action: Literal["navigate"] = "navigate"
    selector: Optional[str] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    url: Optional[str] = Field(default=None, description="URL to navigate to (for goto)")
    navigate_action: NavigateDirection = Field(default=NavigateDirection.GOTO)
    wait_until: str = Field(default="networkidle")


class DialogInput(BaseModel):
    """Configure how to handle the next browser dialog (alert/confirm/prompt)."""

    action: Literal["dialog"] = "dialog"
    selector: Optional[str] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    dialog_action: DialogResponse = Field(default=DialogResponse.ACCEPT)
    prompt_text: Optional[str] = Field(default=None, description="Text for prompt dialogs")


class HoverInput(BaseModel):
    """Hover over an element."""

    action: Literal["hover"] = "hover"
    selector: str = Field(description="CSS selector of element to hover")
    timeout: Optional[int] = Field(default=None)


class SelectInput(BaseModel):
    """Select an option from a dropdown."""

    action: Literal["select"] = "select"
    selector: str = Field(description="CSS selector of <select> element")
    timeout: Optional[int] = Field(default=None)
    value: Optional[str] = Field(default=None, description="Option value attribute")
    label: Optional[str] = Field(default=None, description="Option visible text")
    index: Optional[int] = Field(default=None, description="Option index (0-based)")


class KeyboardInput(BaseModel):
    """Press a key or key combination."""

    action: Literal["keyboard"] = "keyboard"
    selector: Optional[str] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    key: str = Field(description="Key name or combo: 'Enter', 'Control+A', 'ArrowDown'")
    repeat: int = Field(default=1, description="Number of times to press")


class WaitInput(BaseModel):
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


class EvaluateInput(BaseModel):
    """Evaluate a JavaScript expression in the page context."""

    action: Literal["evaluate"] = "evaluate"
    selector: Optional[str] = Field(default=None)
    timeout: Optional[int] = Field(default=None)
    expression: str = Field(description="JavaScript expression to evaluate")


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
    data: Optional[dict[str, Any]] = Field(
        default=None, description="Action-specific return data"
    )


class ActionSequenceResult(BaseModel):
    """Result of executing a sequence of browser actions."""

    url: str
    actions_total: int = Field(default=0)
    actions_succeeded: int = Field(default=0)
    actions_failed: int = Field(default=0)
    results: list[ActionResult] = Field(default_factory=list)
    total_time_ms: float = Field(default=0.0)


class ScreenshotResult(BaseModel):
    """Result of a screenshot operation."""

    url: str
    path: str
    format: ScreenshotFormat
    size_bytes: int = Field(default=0)
    status: ActionStatus
