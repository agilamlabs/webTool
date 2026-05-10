"""v1.6.5 high-severity fixes.

- H4: env_nested_delimiter must be set so WEB_AGENT_BROWSER__HEADLESS
  actually configures browser.headless. The doc claim was previously
  silently broken.
- H5: is_private_address now caches DNS resolution per host so the
  default-on private-IP gate doesn't re-resolve every request.
"""

from __future__ import annotations

import socket

import pytest
from web_agent.config import AppConfig
from web_agent.utils import _resolve_host_addresses, is_private_address

# ----------------------------------------------------------------------
# H4: nested env vars actually apply
# ----------------------------------------------------------------------


def test_env_var_top_level_string_applies(monkeypatch):
    """Top-level WEB_AGENT_LOG_LEVEL was already wired and must keep working."""
    monkeypatch.setenv("WEB_AGENT_LOG_LEVEL", "DEBUG")
    config = AppConfig()
    assert config.log_level == "DEBUG"


def test_env_var_nested_browser_headless_applies(monkeypatch):
    """The doc claim WEB_AGENT_BROWSER__HEADLESS=false must actually set browser.headless."""
    monkeypatch.setenv("WEB_AGENT_BROWSER__HEADLESS", "false")
    config = AppConfig()
    assert config.browser.headless is False


def test_env_var_nested_safety_block_private_ips_applies(monkeypatch):
    monkeypatch.setenv("WEB_AGENT_SAFETY__BLOCK_PRIVATE_IPS", "false")
    config = AppConfig()
    assert config.safety.block_private_ips is False


def test_env_var_nested_int_field_applies(monkeypatch):
    """Numeric nested fields parse correctly through the delimiter."""
    monkeypatch.setenv("WEB_AGENT_BROWSER__VIEWPORT_WIDTH", "1280")
    config = AppConfig()
    assert config.browser.viewport_width == 1280


def test_env_var_unset_uses_defaults():
    """Sanity: with no env vars set, defaults still apply."""
    config = AppConfig()
    # Defaults from BrowserConfig
    assert config.browser.headless is True
    assert config.safety.block_private_ips is True


# ----------------------------------------------------------------------
# H5: cached DNS resolution
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_dns_cache():
    """Clear the LRU cache between tests so they don't leak state."""
    _resolve_host_addresses.cache_clear()
    yield
    _resolve_host_addresses.cache_clear()


def test_dns_cache_dedupes_calls(monkeypatch):
    """Repeat calls to the same host hit the LRU cache, not the resolver."""
    call_count = {"n": 0}

    def fake_getaddrinfo(host, _port):
        call_count["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    # First call resolves
    addrs1 = _resolve_host_addresses("example.com")
    assert addrs1 == ("203.0.113.1",)
    assert call_count["n"] == 1

    # Second call hits the cache
    addrs2 = _resolve_host_addresses("example.com")
    assert addrs2 == ("203.0.113.1",)
    assert call_count["n"] == 1, "second lookup should hit LRU cache"


def test_dns_cache_distinguishes_hosts(monkeypatch):
    """Different hosts produce distinct cache entries."""
    calls: list[str] = []

    def fake_getaddrinfo(host, _port):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (f"203.0.113.{len(calls)}", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    a = _resolve_host_addresses("a.example.com")
    b = _resolve_host_addresses("b.example.com")
    assert a != b
    assert calls == ["a.example.com", "b.example.com"]


def test_dns_cache_returns_empty_on_gaierror(monkeypatch):
    """Resolution failure returns empty tuple, doesn't raise."""

    def failing_getaddrinfo(_host, _port):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", failing_getaddrinfo)
    assert _resolve_host_addresses("nonexistent.invalid") == ()


def test_is_private_address_uses_cache(monkeypatch):
    """is_private_address pulls from the cache for hostname inputs."""
    calls = {"n": 0}

    def fake_getaddrinfo(_host, _port):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    # Two calls for the same host: only one underlying resolution
    assert is_private_address("internal.corp") is True
    assert is_private_address("internal.corp") is True
    assert calls["n"] == 1


def test_is_private_address_literal_ip_skips_dns(monkeypatch):
    """Literal IP inputs never hit the resolver."""

    def should_not_be_called(*_a, **_k):
        raise AssertionError("DNS resolver was called for a literal IP")

    monkeypatch.setattr(socket, "getaddrinfo", should_not_be_called)

    assert is_private_address("169.254.169.254") is True
    assert is_private_address("8.8.8.8") is False
