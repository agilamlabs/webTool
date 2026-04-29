"""Correlation ID propagation via contextvars + loguru patching.

Every public Agent method and MCP tool wraps its body in a
:func:`correlation_scope`. The generated UUID4 is auto-injected into all
loguru log records as the ``cid`` extra field, making it possible to trace
a single request across SearchEngine, WebFetcher, ContentExtractor,
Downloader, and BrowserActions.

Result models that embed ``correlation_id`` echo this value back to the
caller so the same identifier can be matched up with the produced data.

Example::

    from web_agent.correlation import correlation_scope, get_correlation_id

    with correlation_scope() as cid:
        logger.info("starting work")  # log record carries 'cid' extra
        do_thing()
        result.correlation_id = cid
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from loguru import logger

# ---------------------------------------------------------------------------
# Context variable
# ---------------------------------------------------------------------------

correlation_id_var: ContextVar[Optional[str]] = ContextVar(
    "correlation_id", default=None
)


def get_correlation_id() -> Optional[str]:
    """Return the correlation id for the current async/sync context, or None."""
    return correlation_id_var.get()


def new_correlation_id() -> str:
    """Generate a fresh UUID4 string."""
    return str(uuid.uuid4())


@contextmanager
def correlation_scope(cid: Optional[str] = None) -> Iterator[str]:
    """Push a correlation id for the duration of the block.

    Args:
        cid: Existing correlation id to reuse. If ``None``, a fresh UUID4 is
            generated.

    Yields:
        The correlation id active inside the block.

    Example::

        with correlation_scope() as cid:
            await do_request()
            result.correlation_id = cid
    """
    final_cid = cid or new_correlation_id()
    token = correlation_id_var.set(final_cid)
    try:
        yield final_cid
    finally:
        correlation_id_var.reset(token)


# ---------------------------------------------------------------------------
# Loguru patcher
# ---------------------------------------------------------------------------


_PATCHED = False


def patch_loguru() -> None:
    """Install a loguru patcher that injects ``cid`` into every log record.

    Idempotent -- safe to call multiple times. Existing log format strings
    don't need to reference ``cid`` explicitly; it lives in ``record["extra"]``
    and can be added to any custom format like ``{extra[cid]}``.
    """
    global _PATCHED
    if _PATCHED:
        return

    def _add_cid(record: dict) -> None:
        record["extra"]["cid"] = correlation_id_var.get() or "-"

    logger.configure(patcher=_add_cid)
    _PATCHED = True


# Auto-patch on import so any module that uses loguru after importing
# web_agent gets the cid extras for free.
patch_loguru()
