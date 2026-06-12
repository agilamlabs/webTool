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

    @pytest.mark.parametrize(
        "name",
        ["CON.pdf", "con.txt", "NUL", "AUX", "PRN", "COM1.log", "LPT9.dat",
         "sub/CON.csv", "CON.", "PRN "],
    )
    def test_rejects_windows_reserved_device_names(self, tmp_path: Path, name: str) -> None:
        # v1.6.16 deep-review fix: a web-derived filename like CON.pdf would
        # otherwise write to the Windows console device, not a file.
        with pytest.raises(ValueError, match=r"[Rr]eserved"):
            safe_join_path(tmp_path, name)

    @pytest.mark.parametrize(
        "name", ["console.txt", "common.csv", "lpt.txt", "com.txt", "report.pdf", "data/file.json"]
    )
    def test_allows_reserved_name_lookalikes(self, tmp_path: Path, name: str) -> None:
        # Regression: only the EXACT device stems are reserved.
        result = safe_join_path(tmp_path, name)
        assert result.is_relative_to(tmp_path.resolve())


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


class TestDenyListIPv6Canonicalization:
    """A deny-list IPv6 literal must match equivalent textual URL forms.

    IPv6 has many equivalent spellings (zero-compression, leading zeros,
    case). Before the fix, a non-canonical deny entry normalized to a raw
    string that compared unequal to the compressed host the comparator
    derived from the live URL -- the deny silently failed open.
    """

    def test_expanded_deny_matches_compressed_url(self) -> None:
        # 2001:db8::/32 is classified PRIVATE, so block_private_ips=False is
        # required to isolate the deny-list COMPARATOR -- otherwise the test
        # would pass via the private-IP gate even with the fix reverted.
        sc = SafetyConfig(
            denied_domains=["[2001:db8:0:0:0:0:0:1]"], block_private_ips=False
        )
        assert check_domain_allowed("http://[2001:db8::1]/x", sc) is False

    def test_compressed_deny_matches_expanded_url(self) -> None:
        sc = SafetyConfig(denied_domains=["[2001:db8::1]"], block_private_ips=False)
        assert (
            check_domain_allowed(
                "http://[2001:0db8:0000:0000:0000:0000:0000:1]/x", sc
            )
            is False
        )

    def test_uppercase_deny_matches_lowercase_url(self) -> None:
        sc = SafetyConfig(denied_domains=["[2001:DB8::1]"], block_private_ips=False)
        assert check_domain_allowed("http://[2001:db8::1]/x", sc) is False

    def test_public_ipv6_deny_matches_under_ssrf_gate(self) -> None:
        # A *public* IPv6 (Cloudflare DNS) is NOT caught by the private-IP
        # gate, so this exercises the comparator under the default, realistic
        # block_private_ips=True config -- the strongest form of the test.
        sc = SafetyConfig(denied_domains=["[2606:4700:4700:0:0:0:0:1111]"])
        assert check_domain_allowed("http://[2606:4700:4700::1111]/x", sc) is False

    def test_unbracketed_compressed_ipv6_deny_blocks(self) -> None:
        # CO-2 regression: the port-strip urlparse step split an UNBRACKETED
        # IPv6 deny entry on its first colon, truncating ``2001:db8::1`` to
        # ``2001`` so the deny silently failed open. The natural unbracketed
        # form must now block.
        sc = SafetyConfig(denied_domains=["2001:db8::1"], block_private_ips=False)
        assert check_domain_allowed("http://[2001:db8::1]/x", sc) is False

    def test_unbracketed_expanded_ipv6_deny_matches_compressed_url(self) -> None:
        sc = SafetyConfig(denied_domains=["2001:db8:0:0:0:0:0:1"], block_private_ips=False)
        assert check_domain_allowed("http://[2001:db8::1]/x", sc) is False

    def test_unbracketed_ipv6_allow_list_not_broken_closed(self) -> None:
        # The flip side: an unbracketed IPv6 ALLOW entry previously truncated
        # to ``2001`` and fail-CLOSED, blocking all traffic to the intended
        # host. It must now allow the matching host and still deny others.
        sc = SafetyConfig(allowed_domains=["2001:db8::1"], block_private_ips=False)
        assert check_domain_allowed("http://[2001:db8::1]/x", sc) is True
        assert check_domain_allowed("http://[2001:db8::2]/x", sc) is False

    def test_normal_hostname_deny_still_works(self) -> None:
        # Regression guard: plain-hostname deny semantics are unchanged.
        sc = SafetyConfig(denied_domains=["evil.com"])
        assert check_domain_allowed("https://evil.com/path", sc) is False
        assert check_domain_allowed("https://api.evil.com/path", sc) is False
