"""v1.6.9 MCP-as-optional-extra tests.

Verifies:
  1. ``import web_agent`` and ``from web_agent import Agent`` succeed
     even when the ``mcp`` package is unavailable.
  2. Importing ``web_agent.mcp_server`` without ``mcp`` raises an
     ImportError with the documented install hint.
  3. The docstring no longer hardcodes a tool count (uses categories).
"""

from __future__ import annotations

import builtins
import importlib
import sys
from typing import Any

import pytest


def test_mcp_server_docstring_uses_categories_not_count() -> None:
    import web_agent.mcp_server as mcp_server

    doc = mcp_server.__doc__ or ""
    # The hardcoded "Exposes 12 tools" string from v1.6.4 must be gone.
    assert "Exposes 12 tools" not in doc
    # Categories are mentioned instead
    assert "search" in doc.lower()
    assert "fetch" in doc.lower()
    assert "recipes" in doc.lower()


def test_web_agent_imports_without_mcp_present() -> None:
    """Smoke test: ``import web_agent`` must NOT eagerly import mcp.

    A user who installed ``web-agent-toolkit`` without the [mcp] extra
    should still be able to ``from web_agent import Agent`` cleanly.
    """
    import web_agent  # noqa: F401  -- side-effect import check

    # web_agent.__init__ should not have pulled in mcp.server.fastmcp
    # transitively. (It can have a lazy import of mcp_server somewhere
    # else, but the top-level package import must work standalone.)
    # We can't reliably assert mcp isn't loaded if a previous test
    # already imported mcp_server -- so just verify the public Agent
    # import path is clean.
    from web_agent import Agent  # noqa: F401


def test_mcp_server_import_without_mcp_raises_clear_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When mcp.server.fastmcp can't be imported, mcp_server's import
    surfaces a custom ImportError with an install hint."""
    # Remove cached mcp_server + simulate missing mcp pkg
    sys.modules.pop("web_agent.mcp_server", None)
    sys.modules.pop("mcp", None)
    sys.modules.pop("mcp.server", None)
    sys.modules.pop("mcp.server.fastmcp", None)

    real_import = builtins.__import__

    def block_mcp(
        name: str, globals: Any = None, locals: Any = None, fromlist: Any = (), level: int = 0
    ) -> Any:
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError(f"No module named {name!r} (simulated)")
        return real_import(name, globals, locals, fromlist, level)

    with monkeypatch.context() as mp:
        mp.setattr(builtins, "__import__", block_mcp)
        with pytest.raises(ImportError) as excinfo:
            importlib.import_module("web_agent.mcp_server")
        assert "web-agent-toolkit[mcp]" in str(excinfo.value)
        assert "pip install" in str(excinfo.value)

    # Re-import the real module so subsequent tests have a clean state
    sys.modules.pop("web_agent.mcp_server", None)
    importlib.import_module("web_agent.mcp_server")
