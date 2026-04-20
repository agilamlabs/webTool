"""Public exception hierarchy for the web_agent toolkit.

All exceptions inherit from :class:`WebAgentError` so callers can catch
the base class for blanket handling or individual subclasses for precision.

Example::

    from web_agent import Agent
    from web_agent.exceptions import NavigationError, SearchError

    async with Agent() as agent:
        try:
            result = await agent.fetch_and_extract(url)
        except NavigationError as exc:
            print(f"Page failed to load: {exc}")
"""

from __future__ import annotations


class WebAgentError(Exception):
    """Base exception for all web_agent errors."""


class BrowserError(WebAgentError):
    """Browser launch, context creation, or lifecycle failure."""


class NavigationError(WebAgentError):
    """Page navigation failure (timeout, blocked, HTTP error)."""

    def __init__(self, message: str, url: str = "", status_code: int | None = None) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(message)


class ExtractionError(WebAgentError):
    """Content extraction failure across all extraction layers."""


class SearchError(WebAgentError):
    """Search engine failure (Google and DuckDuckGo both failed)."""


class DownloadError(WebAgentError):
    """File download failure."""

    def __init__(self, message: str, url: str = "") -> None:
        self.url = url
        super().__init__(message)


class ActionError(WebAgentError):
    """Browser automation action failure."""

    def __init__(self, message: str, action: str = "", selector: str | None = None) -> None:
        self.action = action
        self.selector = selector
        super().__init__(message)


class ActionTimeoutError(ActionError):
    """A browser action timed out waiting for a selector or condition."""


class SelectorNotFoundError(ActionError):
    """A CSS selector did not match any element on the page."""


class ConfigError(WebAgentError):
    """Configuration validation or loading failure."""
