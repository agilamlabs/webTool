"""v1.6.14 review-hardening follow-up tests.

Covers the full-codebase-review fixes folded into v1.6.14 (the
"review-hardening" pass). All unit-level -- no real browser, no real
network. Heavier paths (post-connect peer-IP guards, MCP clamps) are
exercised by the per-cluster source review + the existing integration
suite; these lock in the cheap-to-unit-test behaviours.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from web_agent import utils
from web_agent.config import AppConfig, FetchConfig, SafetyConfig
from web_agent.content_extractor import _extract_json_ld
from web_agent.models import LocatorSpec
from web_agent.utils import async_retry, is_private_address, parse_retry_after
from web_agent.workspace import Workspace, WorkspaceError


# ---------------------------------------------------------------------------
# C-8: obfuscated IPv4 literals normalised before the private-IP check
# ---------------------------------------------------------------------------
class TestC8ObfuscatedIPLiterals:
    @pytest.mark.parametrize(
        "host",
        [
            "0177.0.0.1",  # octal 127.0.0.1
            "2130706433",  # decimal 127.0.0.1
            "0x7f.0.0.1",  # hex 127.0.0.1
            "127.0.0.1",  # plain loopback (regression guard)
        ],
    )
    def test_obfuscated_loopback_detected_as_private(self, host: str) -> None:
        assert is_private_address(host) is True

    def test_public_literal_not_private(self) -> None:
        assert is_private_address("8.8.8.8") is False

    def test_genuine_hostname_does_not_false_positive_via_inet_aton(self) -> None:
        # inet_aton must reject a real hostname (OSError) and fall through to
        # DNS, not mis-normalise it. example.com never resolves private.
        assert is_private_address("example.com") is False


# ---------------------------------------------------------------------------
# C-1(a): DNS resolution cache now has a TTL (was unbounded lru_cache)
# ---------------------------------------------------------------------------
class TestC1DnsCacheTTL:
    def test_cache_clear_shim_present(self) -> None:
        # Back-compat with the old lru_cache API.
        assert hasattr(utils._resolve_host_addresses, "cache_clear")
        utils._resolve_host_addresses.cache_clear()

    def test_resolution_cached_then_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        utils._resolve_host_addresses.cache_clear()
        calls: list[str] = []

        def fake_getaddrinfo(host: str, port: object) -> list:
            calls.append(host)
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(utils.socket, "getaddrinfo", fake_getaddrinfo)

        a1 = utils._resolve_host_addresses("ttl.example.test")
        a2 = utils._resolve_host_addresses("ttl.example.test")  # served from cache
        assert a1 == a2 == ("93.184.216.34",)
        assert len(calls) == 1

        # Force expiry by rewriting the entry's deadline into the past.
        _expiry, addrs = utils._dns_cache["ttl.example.test"]
        utils._dns_cache["ttl.example.test"] = (0.0, addrs)
        utils._resolve_host_addresses("ttl.example.test")  # must re-resolve
        assert len(calls) == 2
        utils._resolve_host_addresses.cache_clear()


# ---------------------------------------------------------------------------
# E-1 / E-2: parse_retry_after + async_retry hardening
# ---------------------------------------------------------------------------
class TestRetryHelpers:
    def test_parse_retry_after_giant_integer_returns_none(self) -> None:
        # >308-digit integer overflows float(); must be caught, not raised.
        assert parse_retry_after("9" * 400) is None

    def test_parse_retry_after_normal(self) -> None:
        assert parse_retry_after("120") == 120.0
        assert parse_retry_after(None) is None
        assert parse_retry_after("garbage") is None

    def test_async_retry_rejects_non_exception_type(self) -> None:
        with pytest.raises(TypeError):
            async_retry(non_retryable_exceptions=(BaseException,))

    def test_async_retry_accepts_exception_subclass(self) -> None:
        # Should not raise at decoration time.
        deco = async_retry(non_retryable_exceptions=(ValueError,))
        assert callable(deco)


# ---------------------------------------------------------------------------
# D-1 / D-2 / D-6 / D-7 / D-8: config + model hardening
# ---------------------------------------------------------------------------
class TestConfigHardening:
    def test_safe_mode_resets_upload_escape_hatch(self) -> None:
        s = SafetyConfig(safe_mode=True, allow_upload_outside_download_dir=True)
        assert s.allow_upload_outside_download_dir is False

    def test_safe_mode_resets_all_capability_flags(self) -> None:
        s = SafetyConfig(
            safe_mode=True,
            allow_js_evaluation=True,
            allow_downloads=True,
            allow_form_submit=True,
            allow_coordinate_clicks=True,
        )
        assert not s.allow_js_evaluation
        assert not s.allow_downloads
        assert not s.allow_form_submit
        assert not s.allow_coordinate_clicks

    def test_locator_spec_role_name_only_not_empty(self) -> None:
        assert LocatorSpec(role_name="Submit").is_empty() is False
        assert LocatorSpec().is_empty() is True

    def test_negative_budget_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SafetyConfig(max_pages_per_call=-1)
        with pytest.raises(ValidationError):
            SafetyConfig(max_chars_per_call=0)

    def test_negative_rps_rejected_zero_allowed(self) -> None:
        with pytest.raises(ValidationError):
            SafetyConfig(rate_limit_per_host_rps=-1.0)
        assert SafetyConfig(rate_limit_per_host_rps=0.0).rate_limit_per_host_rps == 0.0

    def test_retry_policy_literal_enforced(self) -> None:
        with pytest.raises(ValidationError):
            FetchConfig(retry_policy="nonsense")
        assert FetchConfig(retry_policy="paranoid").retry_policy == "paranoid"

    def test_self_hosted_searxng_localhost_allowed(self) -> None:
        # C-2 reconciliation: a loopback SearXNG base_url is the recommended
        # deployment and must construct without error (the over-aggressive
        # validator that rejected it was reverted).
        cfg = AppConfig(search={"searxng_base_url": "http://localhost:8888"})
        assert cfg.search.searxng_base_url == "http://localhost:8888"


# ---------------------------------------------------------------------------
# E-3: JSON-LD @graph block cap
# ---------------------------------------------------------------------------
class TestE3JsonLdCap:
    def test_giant_graph_capped(self) -> None:
        big = {"@graph": [{"position": i} for i in range(3000)]}
        html = f'<script type="application/ld+json">{json.dumps(big)}</script>'
        blocks = _extract_json_ld(html)
        assert len(blocks) <= 500

    def test_normal_jsonld_still_parsed(self) -> None:
        one = {"@type": "Article", "headline": "Hi"}
        html = f'<script type="application/ld+json">{json.dumps(one)}</script>'
        assert _extract_json_ld(html) == [one]


# ---------------------------------------------------------------------------
# E-5: debug correlation-id path traversal containment
# ---------------------------------------------------------------------------
class TestE5DebugCidContainment:
    def test_traversal_cid_stays_under_debug_dir(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web_agent import debug as debug_mod

        cfg = AppConfig(debug={"enabled": True, "debug_dir": str(tmp_path)})
        dc = debug_mod.DebugCapture(cfg)
        monkeypatch.setattr(debug_mod, "get_correlation_id", lambda: "../../../etc/evil")
        p = dc._next_artifact_path("snap", "html")
        # The malicious cid must not escape debug_dir.
        assert tmp_path.resolve() in p.resolve().parents


# ---------------------------------------------------------------------------
# F-6: absolute workspace_dir rejected at consumption
# ---------------------------------------------------------------------------
class TestF6WorkspaceContainment:
    def test_absolute_workspace_dir_rejected(self) -> None:
        cfg = AppConfig(workspace={"enabled": True, "workspace_dir": "/etc"})
        ws = Workspace(cfg)
        with pytest.raises(WorkspaceError):
            ws.root()

    def test_relative_workspace_dir_ok(self, tmp_path) -> None:
        cfg = AppConfig(
            base_dir=str(tmp_path),
            workspace={"enabled": True, "workspace_dir": "ws"},
        )
        ws = Workspace(cfg)
        root = ws.root()
        assert (
            tmp_path.resolve() in root.resolve().parents
            or root.resolve() == (tmp_path / "ws").resolve()
        )


# ---------------------------------------------------------------------------
# B-4: interaction-action models importable from the package root
# ---------------------------------------------------------------------------
class TestB4PublicExports:
    def test_interaction_models_importable(self) -> None:
        from web_agent import (  # noqa: F401
            ClickInput,
            DialogInput,
            DialogResponse,
            FillInput,
            KeyboardInput,
            MouseButton,
            ScrollInput,
            SelectInput,
            WaitInput,
        )

        assert ClickInput is not None and FillInput is not None


# ---------------------------------------------------------------------------
# C-2: SearXNG result-URL private-IP filter (base_url block reverted)
# ---------------------------------------------------------------------------
class TestC2SearxngResultFilter:
    @pytest.mark.asyncio
    async def test_private_ip_results_dropped_localhost_base_ok(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from web_agent.search_providers import SearXNGProvider

        # localhost base_url must work (C-2 revert); private RESULT urls dropped.
        p = SearXNGProvider(base_url="http://localhost:8888")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {"url": "https://example.com/ok", "title": "ok", "content": ""},
                {"url": "http://127.0.0.1/admin", "title": "evil", "content": ""},
                {"url": "http://169.254.169.254/latest/meta-data/", "title": "imds", "content": ""},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("web_agent.search_providers.httpx.AsyncClient", return_value=mock_client):
            resp = await p.search("q", max_results=5)

        urls = [r.url for r in resp.results]
        assert "https://example.com/ok" in urls
        assert "http://127.0.0.1/admin" not in urls
        assert "http://169.254.169.254/latest/meta-data/" not in urls
        assert resp.total_results == 1
