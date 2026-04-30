"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
from web_agent.config import AppConfig

SAMPLE_DATA_DIR = Path(__file__).parent.parent / "sample_data"


@pytest.fixture
def app_config() -> AppConfig:
    """Default AppConfig with no YAML file (uses all defaults)."""
    return AppConfig()


@pytest.fixture
def sample_article_html() -> str:
    """Load the sample article HTML fixture."""
    path = SAMPLE_DATA_DIR / "sample_article.html"
    return path.read_text(encoding="utf-8")


@pytest.fixture
def minimal_html() -> str:
    """Minimal HTML with almost no content (for fallback testing)."""
    return "<html><head><title>Tiny</title></head><body><p>Hi</p></body></html>"


@pytest.fixture
def empty_html() -> str:
    """Empty HTML document."""
    return "<html><head></head><body></body></html>"
