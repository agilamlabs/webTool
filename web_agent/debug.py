"""Debug-mode artifact capture on failures.

When :class:`DebugConfig.enabled` is True, every fetch/action/download
failure dumps an HTML snapshot, a screenshot, and an error-context JSON
file to ``debug_dir/{correlation_id}/{timestamp}-{label}.{html|png|json}``.

The artifact paths are attached to the corresponding result model
(``debug_artifacts: list[str]``) so the caller can locate them after
the failure.
"""

from __future__ import annotations

import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from playwright.async_api import Page

from .config import AppConfig
from .correlation import get_correlation_id
from .utils import safe_page_content


class DebugCapture:
    """Persists failure artifacts (HTML, screenshot, error JSON) for offline diagnosis.

    Args:
        config: AppConfig, used to read ``config.debug``.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._capture_count = 0

    @property
    def enabled(self) -> bool:
        """Whether debug capture is currently enabled."""
        return self._config.debug.enabled

    def _next_artifact_path(self, label: str, suffix: str) -> Path:
        """Build a unique path under ``debug_dir/{cid}/{timestamp}-{label}.{suffix}``."""
        raw_cid = get_correlation_id() or "no-cid"
        # v1.6.14 E-5: the correlation id can be attacker-controlled via the
        # public ``correlation_scope(cid=...)`` API (e.g. fed from an inbound
        # X-Correlation-ID header) and is used here as a path component.
        # Without sanitisation a cid like ``../../etc`` would redirect
        # artifact writes outside debug_dir. Replace anything outside a safe
        # charset with ``_`` (slashes/backslashes can no longer traverse);
        # the only traversal forms left use the allowed ``.`` char, so reject
        # bare ``.`` / ``..`` explicitly.
        cid = re.sub(r"[^A-Za-z0-9._-]", "_", raw_cid)
        if cid in {".", ".."} or not cid:
            cid = "no-cid"
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        out_dir = Path(self._config.debug.debug_dir) / cid
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{ts}-{label}.{suffix}"

    def _under_limit(self) -> bool:
        return self._capture_count < self._config.debug.max_artifacts_per_call

    async def capture_page(
        self,
        page: Page,
        error: Exception,
        label: str,
        context: Optional[dict[str, Any]] = None,
    ) -> list[str]:
        """Save HTML + screenshot + error JSON for a failed page operation.

        Args:
            page: The Playwright Page where the failure occurred.
            error: The exception being handled.
            label: Short label like ``"fetch"`` or ``"click"`` used in filenames.
            context: Extra context to include in the error JSON.

        Returns:
            List of file paths written. Empty if debug is disabled or limit hit.
        """
        if not self.enabled or not self._under_limit():
            return []

        artifacts: list[str] = []
        try:
            if self._config.debug.capture_html:
                html_path = self._next_artifact_path(label, "html")
                try:
                    # v1.6.13: 3-tier capture -- debug snapshots are
                    # *especially* prone to the navigation-race error
                    # because they fire mid-failure when the page is
                    # often already redirecting away. We accept a "" +
                    # "navigating" tuple silently and skip the write.
                    html, html_source = await safe_page_content(page)
                    if html:
                        html_path.write_text(html, encoding="utf-8")
                        artifacts.append(str(html_path))
                        if html_source != "content":
                            logger.debug(
                                "Debug HTML captured via {src} tier",
                                src=html_source,
                            )
                except Exception as exc:
                    logger.debug("Debug HTML capture failed: {e}", e=exc)

            if self._config.debug.capture_screenshot:
                png_path = self._next_artifact_path(label, "png")
                try:
                    await page.screenshot(path=str(png_path), full_page=False)
                    artifacts.append(str(png_path))
                except Exception as exc:
                    logger.debug("Debug screenshot capture failed: {e}", e=exc)

            json_path = self._next_artifact_path(label, "json")
            try:
                page_url = page.url
            except Exception:
                page_url = ""
            payload = {
                "correlation_id": get_correlation_id(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "label": label,
                "page_url": page_url,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "traceback": traceback.format_exception(type(error), error, error.__traceback__),
                "context": context or {},
            }
            json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            artifacts.append(str(json_path))
        except Exception as outer:
            logger.warning("DebugCapture.capture_page failed: {e}", e=outer)

        self._capture_count += len(artifacts)
        if artifacts:
            logger.info(
                "Debug capture saved {n} artifact(s) for {label}",
                n=len(artifacts),
                label=label,
            )
        return artifacts

    def capture_no_page(
        self,
        error: Exception,
        label: str,
        context: Optional[dict[str, Any]] = None,
    ) -> list[str]:
        """Save error JSON for failures that have no Page (e.g. httpx download).

        Args:
            error: The exception being handled.
            label: Short label used in the filename.
            context: Extra context (URL, headers, etc.) to include.

        Returns:
            List of file paths written. Empty if disabled.
        """
        if not self.enabled or not self._under_limit():
            return []

        try:
            json_path = self._next_artifact_path(label, "json")
            payload = {
                "correlation_id": get_correlation_id(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "label": label,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "traceback": traceback.format_exception(type(error), error, error.__traceback__),
                "context": context or {},
            }
            json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            self._capture_count += 1
            logger.info("Debug capture saved error JSON for {label}", label=label)
            return [str(json_path)]
        except Exception as exc:
            logger.warning("DebugCapture.capture_no_page failed: {e}", e=exc)
            return []

    def reset(self) -> None:
        """Reset the per-call artifact counter (call at the start of each Agent method)."""
        self._capture_count = 0
