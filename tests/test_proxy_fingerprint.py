"""v1.7.0 (Wave 2F): proxy support + fingerprint coherence tests.

Fully offline. The browser tests patch ``async_playwright`` in the
``web_agent.browser_manager`` namespace (the NEW v1.7.0 launch_persistent_context
pattern from tests/test_v166_isolation.py); the fetcher tests patch
``httpx.AsyncClient`` in the ``web_agent.web_fetcher`` namespace. Nothing
here hits the network or launches a real Chromium.

Coverage:
- ProxyConfig: scheme validation (good http/socks5 accepted; bad rejected)
  + env-var loading via WEB_AGENT_PROXY__.
- Launch proxy: server set -> chromium.launch / launch_persistent_context
  received the proxy kwarg with server+username+password+bypass; server
  unset -> NO proxy kwarg passed.
- httpx proxy: fetcher's httpx client constructed with the proxy when
  configured; absent when not.
- Fingerprint coherence: the chosen UA OS matches the locale-derived
  platform family (no cross-OS contradiction); coherent_fingerprint=False
  restores the cross-OS pool.
- Header coherence: the httpx side-paths send a browser-consistent
  User-Agent + Accept-Language.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web_agent.browser_manager import BrowserManager, _resolve_user_agent
from web_agent.config import AppConfig, BrowserConfig, ProxyConfig
from web_agent.exceptions import ConfigError
from web_agent.utils import (
    UA_FAMILIES,
    get_random_user_agent,
    locale_os_family,
    ua_os_family,
)
from web_agent.web_fetcher import WebFetcher

# ======================================================================
# ProxyConfig: scheme validation
# ======================================================================


@pytest.mark.parametrize(
    "server",
    [
        "http://127.0.0.1:8080",
        "https://proxy.example.com:3128",
        "socks5://10.0.0.5:1080",
    ],
)
def test_proxy_good_scheme_accepted(server: str) -> None:
    cfg = ProxyConfig(server=server)
    assert cfg.server == server
    assert cfg.is_active() is True


@pytest.mark.parametrize(
    "server",
    [
        "ftp://host:21",
        "socks4://host:1080",  # only socks5 supported
        "host:8080",  # no scheme
        "tcp://host:9000",
    ],
)
def test_proxy_bad_scheme_rejected(server: str) -> None:
    with pytest.raises(ConfigError):
        ProxyConfig(server=server)


def test_proxy_missing_host_rejected() -> None:
    with pytest.raises(ConfigError):
        ProxyConfig(server="http://")


def test_proxy_empty_string_normalized_to_none() -> None:
    """An explicit blank server is treated as "no proxy" so a blank value
    can't slip into Playwright (which rejects an empty server string)."""
    cfg = ProxyConfig(server="   ")
    assert cfg.server is None
    assert cfg.is_active() is False
    assert cfg.playwright_proxy() is None
    assert cfg.httpx_proxy_url() is None


def test_proxy_default_is_inactive() -> None:
    cfg = ProxyConfig()
    assert cfg.server is None
    assert cfg.is_active() is False
    assert cfg.playwright_proxy() is None
    assert cfg.httpx_proxy_url() is None


# ======================================================================
# ProxyConfig: env-var loading via WEB_AGENT_PROXY__
# ======================================================================


def test_proxy_env_loading_standalone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_AGENT_PROXY__SERVER", "http://127.0.0.1:8888")
    monkeypatch.setenv("WEB_AGENT_PROXY__USERNAME", "alice")
    monkeypatch.setenv("WEB_AGENT_PROXY__PASSWORD", "s3cret")
    monkeypatch.setenv("WEB_AGENT_PROXY__BYPASS", "localhost,127.0.0.1")

    cfg = ProxyConfig()
    assert cfg.server == "http://127.0.0.1:8888"
    assert cfg.username == "alice"
    assert cfg.password == "s3cret"
    assert cfg.bypass == "localhost,127.0.0.1"


def test_proxy_env_loading_nested_appconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nested WEB_AGENT_PROXY__<FIELD> path through AppConfig works."""
    monkeypatch.setenv("WEB_AGENT_PROXY__SERVER", "socks5://10.1.2.3:1080")
    cfg = AppConfig()
    assert cfg.proxy.server == "socks5://10.1.2.3:1080"
    assert cfg.proxy.is_active() is True


def test_proxy_bare_env_var_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare unprefixed SERVER env var must NOT leak into the proxy
    config (the sub-config env_prefix discipline)."""
    monkeypatch.setenv("SERVER", "http://evil.example:9999")
    cfg = AppConfig()
    assert cfg.proxy.server is None


# ======================================================================
# ProxyConfig: dict / url builders
# ======================================================================


def test_playwright_proxy_dict_full() -> None:
    cfg = ProxyConfig(
        server="http://host:8080",
        username="u",
        password="p",
        bypass="localhost,*.internal",
    )
    assert cfg.playwright_proxy() == {
        "server": "http://host:8080",
        "username": "u",
        "password": "p",
        "bypass": "localhost,*.internal",
    }


def test_playwright_proxy_dict_server_only() -> None:
    cfg = ProxyConfig(server="http://host:8080")
    # Only server present -- optional keys omitted (not None-valued).
    assert cfg.playwright_proxy() == {"server": "http://host:8080"}


def test_httpx_proxy_url_embeds_auth_url_encoded() -> None:
    cfg = ProxyConfig(server="http://host:8080", username="user@x", password="p:w/d")
    url = cfg.httpx_proxy_url()
    assert url is not None
    # userinfo is URL-encoded so reserved chars (@ : /) don't break parsing.
    assert url == "http://user%40x:p%3Aw%2Fd@host:8080"


def test_httpx_proxy_url_no_auth_returns_server() -> None:
    cfg = ProxyConfig(server="socks5://host:1080")
    assert cfg.httpx_proxy_url() == "socks5://host:1080"


# ======================================================================
# Fingerprint coherence: UA <-> locale/platform consistency
# ======================================================================


def test_coherent_ua_matches_locale_family() -> None:
    """With coherence on (default), the rotated UA's OS family equals the
    locale-derived family for many draws -- never a cross-OS contradiction."""
    for locale in ("en-US", "de-DE", "fr-FR", "ja-JP"):
        bcfg = BrowserConfig(locale=locale)
        family = locale_os_family(locale)
        for _ in range(50):
            ua = _resolve_user_agent(bcfg)
            assert ua is not None
            assert ua_os_family(ua) == family, (locale, ua)


def test_coherence_off_uses_full_pool() -> None:
    """coherent_fingerprint=False restores the cross-OS rotation: over many
    draws we see more than one OS family (the pre-v1.7.0 behaviour)."""
    bcfg = BrowserConfig(coherent_fingerprint=False)
    seen = {ua_os_family(_resolve_user_agent(bcfg) or "") for _ in range(200)}
    seen.discard(None)
    assert len(seen) > 1, seen


def test_explicit_ua_ignores_coherence() -> None:
    """user_agent_mode='explicit' is operator-pinned and untouched by the
    coherence path."""
    bcfg = BrowserConfig(
        user_agent_mode="explicit",
        user_agent="Mozilla/5.0 (X11; Linux x86_64) CustomAgent/1.0",
        locale="en-US",  # would imply windows under coherence
        coherent_fingerprint=True,
    )
    assert _resolve_user_agent(bcfg) == "Mozilla/5.0 (X11; Linux x86_64) CustomAgent/1.0"


def test_playwright_default_ua_is_none() -> None:
    bcfg = BrowserConfig(user_agent_mode="playwright_default")
    assert _resolve_user_agent(bcfg) is None


def test_get_random_user_agent_os_family_pins() -> None:
    for family in UA_FAMILIES:
        for _ in range(25):
            ua = get_random_user_agent(family)
            assert ua_os_family(ua) == family


def test_get_random_user_agent_unknown_family_falls_back() -> None:
    """An unrecognised os_family hint falls back to the full pool (no raise)."""
    ua = get_random_user_agent("plan9")
    assert isinstance(ua, str) and ua


def test_get_random_user_agent_backward_compatible() -> None:
    """The no-arg signature still works for existing callers."""
    ua = get_random_user_agent()
    assert isinstance(ua, str) and ua.startswith("Mozilla/5.0")


# ======================================================================
# Launch proxy wiring (browser_manager)
# ======================================================================


def _fake_pw(chromium: MagicMock) -> MagicMock:
    fake_pw = MagicMock(chromium=chromium)
    fake_pw_cm = MagicMock()
    fake_pw_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_pw_cm.__aexit__ = AsyncMock(return_value=False)
    return fake_pw_cm


@pytest.mark.asyncio
async def test_launch_no_proxy_when_unset(tmp_path: Path) -> None:
    """Default (no proxy): chromium.launch must NOT receive a proxy kwarg.

    v1.7.0: isolation defaults ON (-> launch_persistent_context); this test
    targets the raw chromium.launch path, so isolation is opted out (the
    persistent-context proxy path has its own tests)."""
    config = AppConfig(base_dir=str(tmp_path), browser=BrowserConfig(isolation_mode=False))
    assert config.proxy.is_active() is False

    bm = BrowserManager(config)
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_chromium = MagicMock()
    fake_chromium.launch = AsyncMock(return_value=fake_browser)
    with patch("web_agent.browser_manager.async_playwright", return_value=_fake_pw(fake_chromium)):
        await bm.start()

    fake_chromium.launch.assert_called_once()
    assert "proxy" not in fake_chromium.launch.call_args.kwargs
    await bm.stop()


@pytest.mark.asyncio
async def test_launch_proxy_passed_when_set(tmp_path: Path) -> None:
    """Proxy set -> chromium.launch receives proxy={server,username,password,bypass}.

    v1.7.0: isolation opted out so this exercises the raw chromium.launch path."""
    config = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(isolation_mode=False),
        proxy=ProxyConfig(
            server="http://127.0.0.1:8080",
            username="u",
            password="p",
            bypass="localhost",
        ),
    )
    bm = BrowserManager(config)
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_chromium = MagicMock()
    fake_chromium.launch = AsyncMock(return_value=fake_browser)
    with patch("web_agent.browser_manager.async_playwright", return_value=_fake_pw(fake_chromium)):
        await bm.start()

    proxy = fake_chromium.launch.call_args.kwargs["proxy"]
    assert proxy == {
        "server": "http://127.0.0.1:8080",
        "username": "u",
        "password": "p",
        "bypass": "localhost",
    }
    await bm.stop()


@pytest.mark.asyncio
async def test_launch_persistent_context_proxy_passed_named(tmp_path: Path) -> None:
    """Named-profile launch_persistent_context receives the proxy kwarg too."""
    config = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(
            isolation_mode=True,
            profile_mode="named",
            profile_dir="prof",
        ),
        proxy=ProxyConfig(server="socks5://10.0.0.5:1080"),
    )
    bm = BrowserManager(config)
    bm._apply_stealth = AsyncMock()  # type: ignore[method-assign]
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_ctx = MagicMock(name="PersistentContext")
    fake_ctx.browser = fake_browser
    fake_ctx.close = AsyncMock()
    fake_ctx.route = AsyncMock()
    fake_ctx.set_default_timeout = MagicMock()
    fake_ctx.set_default_navigation_timeout = MagicMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_ctx)
    fake_chromium.launch = AsyncMock(side_effect=AssertionError("named must use persistent context"))
    with patch("web_agent.browser_manager.async_playwright", return_value=_fake_pw(fake_chromium)):
        await bm.start()

    kwargs = fake_chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["proxy"] == {"server": "socks5://10.0.0.5:1080"}
    await bm.stop()


@pytest.mark.asyncio
async def test_launch_persistent_context_no_proxy_named(tmp_path: Path) -> None:
    """Named-profile launch with no proxy omits the kwarg entirely."""
    config = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(isolation_mode=True, profile_mode="named", profile_dir="prof"),
    )
    bm = BrowserManager(config)
    bm._apply_stealth = AsyncMock()  # type: ignore[method-assign]
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_ctx = MagicMock(name="PersistentContext")
    fake_ctx.browser = fake_browser
    fake_ctx.close = AsyncMock()
    fake_ctx.route = AsyncMock()
    fake_ctx.set_default_timeout = MagicMock()
    fake_ctx.set_default_navigation_timeout = MagicMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_ctx)
    with patch("web_agent.browser_manager.async_playwright", return_value=_fake_pw(fake_chromium)):
        await bm.start()

    kwargs = fake_chromium.launch_persistent_context.call_args.kwargs
    assert "proxy" not in kwargs
    await bm.stop()


@pytest.mark.asyncio
async def test_launch_persistent_context_proxy_passed_ephemeral(tmp_path: Path) -> None:
    """Ephemeral isolation also routes through launch_persistent_context and
    must receive the proxy kwarg."""
    config = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(isolation_mode=True, profile_mode="ephemeral"),
        proxy=ProxyConfig(server="http://127.0.0.1:8080", username="x", password="y"),
    )
    bm = BrowserManager(config)
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_root_ctx = MagicMock(name="EphemeralRootContext")
    fake_root_ctx.browser = fake_browser
    fake_root_ctx.close = AsyncMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_root_ctx)
    fake_chromium.launch = AsyncMock(side_effect=AssertionError("ephemeral uses persistent context"))
    with patch("web_agent.browser_manager.async_playwright", return_value=_fake_pw(fake_chromium)):
        await bm.start()

    kwargs = fake_chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["proxy"] == {
        "server": "http://127.0.0.1:8080",
        "username": "x",
        "password": "y",
    }
    await bm.stop()


@pytest.mark.asyncio
async def test_launch_proxy_coherent_ua_at_launch(tmp_path: Path) -> None:
    """The named persistent context launch passes a UA whose OS family is
    coherent with the configured locale (de-DE -> windows under default
    coherence)."""
    config = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(
            isolation_mode=True,
            profile_mode="named",
            profile_dir="prof",
            locale="de-DE",
        ),
    )
    bm = BrowserManager(config)
    bm._apply_stealth = AsyncMock()  # type: ignore[method-assign]
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_ctx = MagicMock(name="PersistentContext")
    fake_ctx.browser = fake_browser
    fake_ctx.close = AsyncMock()
    fake_ctx.route = AsyncMock()
    fake_ctx.set_default_timeout = MagicMock()
    fake_ctx.set_default_navigation_timeout = MagicMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_ctx)
    with patch("web_agent.browser_manager.async_playwright", return_value=_fake_pw(fake_chromium)):
        await bm.start()

    kwargs = fake_chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["locale"] == "de-DE"
    assert ua_os_family(kwargs["user_agent"]) == locale_os_family("de-DE")
    await bm.stop()


# ======================================================================
# httpx side-path proxy + header coherence (web_fetcher)
# ======================================================================


class _FakeStreamResp:
    """Minimal async-CM stand-in for httpx streaming responses."""

    def __init__(self, *, url: str, headers: dict[str, str], status_code: int = 200) -> None:
        self.url = url
        self.headers = headers
        self.status_code = status_code

    async def __aenter__(self) -> _FakeStreamResp:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def aiter_bytes(self, chunk_size: int = 8192) -> Any:
        for chunk in (b"%PDF-1.7 fake",):
            yield chunk


class _FakeAsyncClient:
    """Captures AsyncClient constructor kwargs and yields a canned stream
    response. Records the last instance's kwargs on the class for asserts."""

    last_kwargs: ClassVar[dict[str, Any]] = {}
    stream_headers: ClassVar[dict[str, str]] = {"content-type": "application/pdf"}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def stream(self, method: str, url: str) -> _FakeStreamResp:
        return _FakeStreamResp(url=url, headers=type(self).stream_headers)


def _make_fetcher(**proxy_kwargs: Any) -> WebFetcher:
    proxy = ProxyConfig(**proxy_kwargs) if proxy_kwargs else ProxyConfig()
    config = AppConfig(proxy=proxy, browser=BrowserConfig(locale="en-US"))
    return WebFetcher(MagicMock(), config)


@pytest.mark.asyncio
async def test_classify_url_httpx_proxy_passed() -> None:
    fetcher = _make_fetcher(server="http://127.0.0.1:8080", username="u", password="p")
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "application/pdf"}
    with patch("web_agent.web_fetcher.httpx.AsyncClient", _FakeAsyncClient):
        kind = await fetcher.classify_url("https://example.com/doc")

    assert kind == "pdf"
    assert _FakeAsyncClient.last_kwargs.get("proxy") == "http://u:p@127.0.0.1:8080"


@pytest.mark.asyncio
async def test_classify_url_no_proxy_kwarg_when_unset() -> None:
    fetcher = _make_fetcher()
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "text/html"}
    with patch("web_agent.web_fetcher.httpx.AsyncClient", _FakeAsyncClient):
        await fetcher.classify_url("https://example.com/page")

    assert "proxy" not in _FakeAsyncClient.last_kwargs


@pytest.mark.asyncio
async def test_classify_url_coherent_headers() -> None:
    """The HEAD-probe client sends a browser-consistent UA + Accept-Language
    matching the configured locale."""
    fetcher = _make_fetcher()
    config = fetcher._config
    config.browser = BrowserConfig(locale="de-DE")
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "text/html"}
    with patch("web_agent.web_fetcher.httpx.AsyncClient", _FakeAsyncClient):
        await fetcher.classify_url("https://example.com/page")

    headers = _FakeAsyncClient.last_kwargs["headers"]
    assert headers["Accept-Language"] == "de-DE,de;q=0.9"
    assert "Windows" in headers["User-Agent"]  # de-DE -> windows family
    assert headers["Accept"].startswith("text/html")


@pytest.mark.asyncio
async def test_fetch_binary_httpx_proxy_and_headers() -> None:
    fetcher = _make_fetcher(server="socks5://10.0.0.5:1080")
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "application/pdf"}
    with patch("web_agent.web_fetcher.httpx.AsyncClient", _FakeAsyncClient):
        result = await fetcher.fetch_binary("https://example.com/file.pdf")

    assert result.binary == b"%PDF-1.7 fake"
    assert _FakeAsyncClient.last_kwargs.get("proxy") == "socks5://10.0.0.5:1080"
    headers = _FakeAsyncClient.last_kwargs["headers"]
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert headers["Accept-Language"].startswith("en-US")


@pytest.mark.asyncio
async def test_fetch_binary_no_proxy_kwarg_when_unset() -> None:
    fetcher = _make_fetcher()
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "application/pdf"}
    with patch("web_agent.web_fetcher.httpx.AsyncClient", _FakeAsyncClient):
        await fetcher.fetch_binary("https://example.com/file.pdf")

    assert "proxy" not in _FakeAsyncClient.last_kwargs


@pytest.mark.asyncio
async def test_fetch_binary_coherent_ua_matches_browser_path() -> None:
    """The side-path UA OS family matches what the browser path would emit
    for the same locale (no Python-identity / cross-OS leak)."""
    fetcher = _make_fetcher()
    fetcher._config.browser = BrowserConfig(locale="en-US")
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "application/pdf"}
    with patch("web_agent.web_fetcher.httpx.AsyncClient", _FakeAsyncClient):
        await fetcher.fetch_binary("https://example.com/file.pdf")

    ua = _FakeAsyncClient.last_kwargs["headers"]["User-Agent"]
    assert ua_os_family(ua) == locale_os_family("en-US")
