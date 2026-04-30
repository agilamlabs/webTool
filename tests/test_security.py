"""Tests for security-critical helpers: path traversal + private IP detection."""

from __future__ import annotations

from pathlib import Path

import pytest
from web_agent.config import SafetyConfig
from web_agent.exceptions import DomainNotAllowedError
from web_agent.utils import (
    _is_private_ip,
    check_domain_allowed,
    is_private_address,
    safe_join_path,
)


class TestSafeJoinPath:
    def test_allows_simple_subpath(self, tmp_path: Path) -> None:
        result = safe_join_path(tmp_path, "report.pdf")
        assert result.parent == tmp_path.resolve()
        assert result.name == "report.pdf"

    def test_allows_nested_subpath(self, tmp_path: Path) -> None:
        result = safe_join_path(tmp_path, "sub/dir/file.pdf")
        assert result.is_relative_to(tmp_path.resolve())

    def test_rejects_dot_dot_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Path escapes"):
            safe_join_path(tmp_path, "../../etc/passwd")

    def test_rejects_deep_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Path escapes"):
            safe_join_path(tmp_path, "sub/../../escaped.txt")

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        # On POSIX "/etc/passwd" is absolute; on Windows it's relative-but-rooted
        # (no drive letter). Either way safe_join_path must reject it.
        with pytest.raises(ValueError):
            safe_join_path(tmp_path, "/etc/passwd")

    def test_rejects_windows_drive_absolute_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            safe_join_path(tmp_path, "C:\\Windows\\System32")

    def test_rejects_empty(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Empty"):
            safe_join_path(tmp_path, "")


class TestIsPrivateAddress:
    def test_blocks_aws_imds(self) -> None:
        assert is_private_address("169.254.169.254")

    def test_blocks_rfc1918_ranges(self) -> None:
        assert is_private_address("10.0.0.1")
        assert is_private_address("172.16.0.1")
        assert is_private_address("192.168.1.1")

    def test_blocks_loopback(self) -> None:
        assert is_private_address("127.0.0.1")
        assert is_private_address("::1")

    def test_blocks_unspecified(self) -> None:
        assert is_private_address("0.0.0.0")

    def test_allows_public_ips(self) -> None:
        assert not is_private_address("8.8.8.8")
        assert not is_private_address("1.1.1.1")

    def test_does_not_block_nat64(self) -> None:
        # NAT64 prefix addresses should NOT be flagged as private
        # (they're a public-traffic mechanism, not RFC1918)
        assert not is_private_address("64:ff9b::8.8.8.8")

    def test_handles_empty(self) -> None:
        assert not is_private_address("")


class TestCheckDomainBlocksPrivateIPs:
    def test_blocks_imds_url_when_block_private_ips(self) -> None:
        sc = SafetyConfig(block_private_ips=True)
        assert not check_domain_allowed("http://169.254.169.254/", sc)

    def test_allows_imds_when_block_private_ips_false(self) -> None:
        sc = SafetyConfig(block_private_ips=False)
        assert check_domain_allowed("http://169.254.169.254/", sc)

    def test_strict_raises_for_private_ip(self) -> None:
        sc = SafetyConfig(block_private_ips=True)
        with pytest.raises(DomainNotAllowedError):
            check_domain_allowed("http://10.0.0.1/", sc, strict=True)

    def test_strict_raises_for_denied_domain(self) -> None:
        sc = SafetyConfig(denied_domains=["evil.example.com"])
        with pytest.raises(DomainNotAllowedError):
            check_domain_allowed("https://api.evil.example.com/", sc, strict=True)


class TestPrivateIpClassifier:
    """Internal helper -- white-box test of the IP-level classifier."""

    def test_classifier_excludes_reserved_to_allow_nat64(self) -> None:
        import ipaddress

        nat64 = ipaddress.ip_address("64:ff9b::1")
        # is_reserved is True for NAT64; we deliberately don't block on it
        assert nat64.is_reserved
        assert not _is_private_ip(nat64)
