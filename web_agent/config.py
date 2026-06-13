"""Configuration management with programmatic construction, environment variables, and YAML.

Supports three configuration methods (in priority order):

1. **Programmatic** (recommended for AI agents)::

    from web_agent import AppConfig
    config = AppConfig(browser={"headless": False}, log_level="DEBUG")

2. **Environment variables** (prefix ``WEB_AGENT_``)::

    export WEB_AGENT_LOG_LEVEL=DEBUG
    export WEB_AGENT_BROWSER__HEADLESS=false

3. **YAML file** (optional)::

    config = AppConfig.from_yaml("/path/to/config.yaml")
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote, urlparse

import yaml
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


def _is_loopback_host(host: str | None) -> bool:
    """v1.6.8 (review C-3 fix): True iff *host* is unambiguously loopback.

    Accepts the entire ``127.0.0.0/8`` IPv4 block, ``::1`` (in any
    canonical form ``urlparse`` returns -- it strips the brackets), and
    the literal hostname ``localhost``. Returning False for everything
    else is the strict default we want for the ``remote_cdp_url`` gate;
    DNS-resolving private addresses (10/8, 192.168/16, etc.) is NOT
    loopback and remote_cdp must not accept them.

    v1.6.16 (review CO-8): obfuscated IPv4 loopback literals that the
    C resolver / Chromium happily dial -- octal ``0177.0.0.1``, decimal
    ``2130706433``, hex ``0x7f.0.0.1``, short-form ``127.1`` -- are
    rejected by ``ipaddress.ip_address`` but accepted by ``inet_aton``.
    The SSRF gate (``utils.is_private_address``) already normalises them
    through ``inet_aton``; reuse the SAME normalisation here so a remote
    CDP URL that points at loopback via an obfuscated literal is
    classified as loopback consistently (it would otherwise be rejected
    by the gate as "not loopback", which is the safe direction but
    diverges from how the rest of the toolkit treats these literals).
    """
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    # Strip IPv6 brackets if present (urlparse already does this, but
    # be defensive in case a caller passes a raw [::1] literal).
    h = host.strip("[]")
    try:
        ip = ipaddress.ip_address(h)
        return bool(ip.is_loopback)
    except ValueError:
        pass
    # v1.6.16 (review CO-8): normalise obfuscated IPv4 literals through
    # ``inet_aton`` (octal / decimal / hex / short-form), mirroring the
    # ``is_private_address`` path. ``inet_aton`` raises OSError for real
    # hostnames, so this branch only fires for numeric forms.
    try:
        normalized = socket.inet_ntoa(socket.inet_aton(h))
    except OSError:
        return False
    try:
        return bool(ipaddress.ip_address(normalized).is_loopback)
    except ValueError:
        return False


def _normalize_domain_patterns(value: Any) -> Any:
    """Normalize a list of domain allow/deny patterns at config-load time.

    Accepts user-supplied strings like ``"https://Evil.com/"`` and
    coerces them to bare hostnames (``"evil.com"``) before the pattern
    is consulted by ``check_domain_allowed``. Without this, malformed
    entries would silently never match anything (the previous v1.6.4
    behavior). Non-list values pass through unchanged so pydantic's
    own type validation can fire.
    """
    if not isinstance(value, list):
        return value
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            # Let pydantic surface the type error naturally.
            out.append(raw)
            continue
        s = raw.strip().lower()
        if "://" in s:
            s = s.split("://", 1)[1]
        # Strip path / query / fragment
        for sep in ("/", "?", "#"):
            if sep in s:
                s = s.split(sep, 1)[0]
        s = s.strip().lstrip(".")
        # v1.6.16 (review CO-2): strip the port and IPv6 brackets the SAME
        # way the match-time comparator does. ``check_domain_allowed`` ->
        # ``utils._normalize_host`` runs ``urlparse(url).hostname``, which
        # is port-stripped and bracket-stripped. Without mirroring that
        # here, a natural deny entry like ``evil.com:8443`` or ``[::1]``
        # would keep its port/brackets and silently never match the bare
        # host the comparator produces (fail-open). Feed a synthetic
        # ``//host`` through urlparse so .hostname applies the identical
        # normalisation (lowercases, drops the port, unwraps ``[...]``).
        if s:
            # CO-2 fix: a BARE IP literal must NOT go through the urlparse
            # hostinfo step. urllib's _hostinfo splits on the FIRST colon, so
            # an unbracketed IPv6 like ``2001:db8::1`` would be truncated to
            # its first hextet (``2001``) and the deny/allow entry would then
            # silently never match the compressed host the comparator derives
            # (fail-open for deny, fail-closed breakage for allow). Bare
            # literals carry no port and need no bracket-stripping, so leave
            # them untouched for _canonicalize_ip_literal below. urlparse still
            # handles hostnames, host:port (``evil.com:8443``), and bracketed
            # IPv6 (``[::1]`` / ``[2001:db8::1]:8443``).
            try:
                ipaddress.ip_address(s)
            except ValueError:
                hostname = urlparse(f"//{s}").hostname
                s = (hostname or s).strip().lstrip(".")
        # Mirror the live-host normalisation: canonicalize IP-literals so a
        # non-canonical IPv6 pattern (e.g. ``2001:db8:0:0:0:0:0:1``) still
        # matches the compressed form the comparator derives from the URL.
        if s:
            from .utils import _canonicalize_ip_literal

            s = _canonicalize_ip_literal(s)
        if s:
            out.append(s)
    return out


class BrowserConfig(BaseSettings):
    """Chromium browser launch and context settings.

    v1.6.6 additions:

    * **Isolation profile** -- ``isolation_mode`` + ``profile_mode`` +
      ``profile_dir`` + ``cleanup_on_exit`` let webTool launch Chromium
      against its own dedicated user-data-dir, isolating cookies /
      localStorage / cache / downloads from the user's real Chrome.
      Defaults: ``isolation_mode=False`` (preserves v1.6.5 launch path).
    * **CDP attach** -- ``cdp_enabled`` + ``cdp_host`` + ``cdp_port`` +
      ``backend`` let external tools observe a webTool-launched browser
      over the Chrome DevTools Protocol. CDP requires isolation
      (DevToolsActivePort lives under the user-data-dir). Defaults:
      ``cdp_enabled=False``. ``attach_existing_browser=True`` is
      explicitly rejected -- webTool only controls browsers it launched.
    """

    headless: bool = True
    # v1.6.16 (review CO-7): throughput/timeout ints. ``slow_mo`` and the
    # timeouts are milliseconds and must not go negative (a negative
    # Playwright timeout is undefined behaviour); ``ge=0`` allows the
    # documented "0 = no artificial delay / use Playwright default" sentinel.
    slow_mo: int = Field(default=0, ge=0)
    default_timeout: int = Field(default=30000, ge=0)
    navigation_timeout: int = Field(default=45000, ge=0)
    # v1.6.16 (review CO-3): ``max_contexts`` feeds
    # ``asyncio.Semaphore(max_contexts)`` in BrowserManager. ``0`` builds a
    # zero-permit semaphore that deadlocks every ephemeral fetch forever;
    # a negative value raises a deep asyncio ValueError at construction.
    # ``ge=1`` turns both into a clean pydantic ConfigError at config time.
    max_contexts: int = Field(default=3, ge=1)
    # v1.6.14 C-4: cap on concurrent ``ctx.new_page()`` calls inside a
    # single :meth:`WebFetcher.fetch_many` invocation when a
    # ``session_id`` is supplied. The ephemeral path (no session_id) is
    # already gated by :class:`BrowserManager`'s context semaphore
    # (``max_contexts``), but the session path shares one
    # BrowserContext and bypasses that gate -- ~20+ concurrent
    # ``new_page()`` calls reproducibly crash Chromium's renderer.
    # Default 5 keeps per-session throughput healthy while staying well
    # below the empirically-observed crash threshold.
    max_pages_per_session_fetch: int = Field(
        default=5,
        ge=1,
        le=50,
        description=(
            "v1.6.14 C-4: maximum concurrent pages created inside one "
            ":meth:`WebFetcher.fetch_many` call when a ``session_id`` is "
            "supplied. Prevents Chromium renderer crashes from too many "
            "parallel ``ctx.new_page()`` calls sharing a single "
            "BrowserContext. The ephemeral (no-session) path is already "
            "gated by ``max_contexts`` via ``BrowserManager``."
        ),
    )
    block_resources: list[str] = Field(
        default_factory=lambda: ["image", "font", "stylesheet", "media"]
    )
    user_data_dir: Optional[str] = Field(
        default=None,
        description=(
            "DEPRECATED in v1.6.6 -- use ``profile_dir`` + "
            "``isolation_mode=True`` instead. Retained for backward "
            "compatibility; if both ``user_data_dir`` and ``profile_dir`` "
            "are set, ``profile_dir`` wins."
        ),
    )
    # v1.6.16 (review CO-7): a viewport dimension <= 0 is rejected by
    # Chromium at launch; surface it as a clean config error instead.
    viewport_width: int = Field(default=1920, gt=0)
    viewport_height: int = Field(default=1080, gt=0)

    # --- v1.6.6: Isolation profile launcher -----------------------------
    isolation_mode: bool = Field(
        default=False,
        description=(
            "Launch Chromium with a dedicated user-data-dir so cookies / "
            "localStorage / cache / downloads are isolated from the "
            "user's real Chrome. Required when ``cdp_enabled=True``."
        ),
    )
    profile_mode: Literal["ephemeral", "named"] = Field(
        default="ephemeral",
        description=(
            "Ephemeral profiles are auto-generated tempdirs deleted on "
            "Agent exit (when ``cleanup_on_exit=True``). Named profiles "
            "persist across runs at ``profile_dir`` for logged-in "
            "workflows. Only consulted when ``isolation_mode=True``."
        ),
    )
    profile_dir: Optional[str] = Field(
        default=None,
        description=(
            "Profile directory path. Resolved against ``AppConfig.base_dir`` "
            "when relative. Required when ``profile_mode='named'``; ignored "
            "when ``profile_mode='ephemeral'`` (tempdir is auto-generated "
            "under ``base_dir/.webtool/browser-profiles/``)."
        ),
    )
    cleanup_on_exit: bool = Field(
        default=True,
        description=(
            "If True and ``profile_mode='ephemeral'``, the auto-generated "
            "profile dir is removed on Agent exit. No-op for named profiles."
        ),
    )
    # --- v1.6.9: deterministic browser identity ---
    # Prior to v1.6.9 these were hardcoded in BrowserManager._build_context.
    # Defaults preserve the v1.6.8 behaviour (en-US / America/New_York /
    # random UA per context). Override for reproducible agents, locale-
    # specific testing, or to pin a stable User-Agent across runs.
    locale: str = Field(
        default="en-US",
        description=(
            "v1.6.9: browser locale string passed to "
            "``browser.new_context(locale=...)``. Default matches v1.6.8 "
            "(``en-US``)."
        ),
    )
    timezone_id: str = Field(
        default="America/New_York",
        description=(
            "v1.6.9: IANA timezone id passed to "
            "``browser.new_context(timezone_id=...)``. Default matches "
            "v1.6.8 (``America/New_York``). Set to ``UTC`` for "
            "reproducible agents."
        ),
    )
    user_agent_mode: Literal["random", "explicit", "playwright_default"] = Field(
        default="random",
        description=(
            "v1.6.9: how to populate ``new_context(user_agent=...)``. "
            "``random`` (default) picks one per context via the v1.6.x "
            "rotation pool. ``explicit`` uses ``user_agent`` (required). "
            "``playwright_default`` passes ``None`` so Playwright uses "
            "its bundled UA."
        ),
    )
    user_agent: Optional[str] = Field(
        default=None,
        description=(
            "v1.6.9: explicit User-Agent string. Required when "
            "``user_agent_mode='explicit'``; ignored otherwise."
        ),
    )
    # --- v1.6.9: sandbox auto-detect ---
    disable_chromium_sandbox: Optional[bool] = Field(
        default=None,
        description=(
            "v1.6.9: pass ``--no-sandbox`` to Chromium. ``None`` (default) "
            "auto-detects: enabled when running in CI (``CI=true`` or "
            "``GITHUB_ACTIONS=true``) or inside a container "
            "(``/.dockerenv`` exists). ``True`` always passes it. "
            "``False`` never passes it. Local dev keeps the sandbox "
            "enabled by default -- a deliberate hardening since the "
            "sandbox provides per-tab isolation against renderer "
            "exploits. Set ``True`` explicitly when running without "
            "kernel namespaces (Docker without --privileged, WSL, etc.)."
        ),
    )

    # --- v1.6.6: CDP attach to webTool-launched browser -----------------
    # v1.6.8 widened the Literal to include ``"remote_cdp"`` -- existing
    # configs with ``playwright`` or ``cdp_owned`` keep working unchanged;
    # the new value is opt-in only.
    backend: Literal["playwright", "cdp_owned", "remote_cdp"] = Field(
        default="playwright",
        description=(
            "Browser control backend. ``playwright`` (default) drives "
            "Chromium directly via Playwright's CDP. ``cdp_owned`` is a "
            "forward-compat label indicating CDP must be enabled; today "
            "it's identical to ``playwright`` with ``cdp_enabled=True``. "
            "``remote_cdp`` (v1.6.8) attaches to an externally-launched "
            "browser via ``chromium.connect_over_cdp(remote_cdp_url)``."
        ),
    )
    remote_cdp_url: Optional[str] = Field(
        default=None,
        description=(
            "WebSocket URL for an externally-launched CDP browser. Required "
            "when ``backend='remote_cdp'``. Example: "
            "``ws://127.0.0.1:9222/devtools/browser/<uuid>``. Must be a "
            "loopback address -- non-loopback URLs are rejected as a "
            "security foot-gun (same rule as ``cdp_host``)."
        ),
    )
    # --- v1.6.9: remote_cdp ownership proof -----------------------------
    # Loopback alone does not prove the target browser was launched by
    # webTool -- a user's personal Chrome can run on loopback too.
    # ``remote_cdp`` now requires the caller to present a token that
    # matches a file webTool wrote into the launcher's profile_dir.
    remote_cdp_ownership_token: Optional[str] = Field(
        default=None,
        description=(
            "v1.6.9: hex token matching ``<remote_cdp_profile_dir>/.webtool-ownership``. "
            "Required when ``backend='remote_cdp'``. webTool writes this "
            "file when it launches a browser with ``isolation_mode=True``; "
            "remote callers read it back via "
            "``OwnershipToken.read(profile_dir)`` and pass it here. "
            "Loopback alone is insufficient -- a user's personal Chrome "
            "can run on loopback too."
        ),
    )
    remote_cdp_profile_dir: Optional[str] = Field(
        default=None,
        description=(
            "v1.6.9: profile directory where the ownership token file "
            "lives. For loopback ``remote_cdp`` (the common case) this "
            "is the same path the launching Agent used. Required when "
            "``backend='remote_cdp'``."
        ),
    )
    cdp_enabled: bool = Field(
        default=False,
        description=(
            "Launch Chromium with ``--remote-debugging-port`` so external "
            "tools can observe via CDP. Requires ``isolation_mode=True``. "
            "Never attaches to existing/personal browsers -- webTool only "
            "exposes CDP on browsers it launched itself."
        ),
    )
    cdp_host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address for the remote debugging endpoint. Must be a "
            "loopback address; non-loopback bindings are rejected as a "
            "security foot-gun."
        ),
    )
    cdp_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description=(
            "Remote debugging port. ``0`` means OS-assigned (recommended); "
            "the actual port is discovered from ``DevToolsActivePort`` "
            "written into the user-data-dir after launch."
        ),
    )
    launch_owned_cdp_browser: bool = Field(
        default=True,
        description=(
            "DEPRECATED / RESERVED (v1.6.14, review D-3): currently a no-op. "
            "This field is never read by the launch path -- webTool ALWAYS "
            "launches its own browser when ``cdp_enabled=True`` and never "
            "attaches to an existing one (see ``attach_existing_browser``, "
            "which is hard-rejected). Setting it to False does NOT change "
            "behaviour. Retained as a forward-compat placeholder; do not "
            "rely on it to gate launching."
        ),
    )
    attach_existing_browser: bool = Field(
        default=False,
        description=(
            "ALWAYS REJECTED if True. webTool never attaches to a "
            "user's existing/personal Chrome -- that would expose real "
            "cookies, history, and logged-in accounts. CDP control is "
            "only available against webTool-launched browsers."
        ),
    )

    # --- v1.7.0: production lifecycle hardening --------------------------
    stealth_enabled: bool = Field(
        default=True,
        description=(
            "v1.7.0: apply playwright-stealth evasions to every browser "
            "context webTool creates (init scripts via "
            "``Stealth.apply_stealth_async``) plus the stealth-related "
            "Chromium launch args (``--disable-blink-features="
            "AutomationControlled``, ``--accept-lang``). ``False`` skips "
            "all stealth application -- useful for debugging raw browser "
            "behaviour or when a target site misbehaves under the evasions."
        ),
    )
    auto_relaunch: bool = Field(
        default=True,
        description=(
            "v1.7.0: when Chromium dies underneath a running Agent "
            "(renderer crash, OOM-kill, external taskkill), the next "
            "browser acquisition transparently relaunches it instead of "
            "failing every subsequent call until restart. ``False`` "
            "surfaces an immediate BrowserError so the operator restarts "
            "manually. Existing sessions on the dead browser are not "
            "resurrected -- they surface the established unknown-session "
            "path on next use."
        ),
    )
    relaunch_max_attempts: int = Field(
        default=3,
        ge=0,
        le=10,
        description=(
            "v1.7.0: bounded number of relaunch attempts after a crash "
            "before giving up with a BrowserError. ``0`` disables "
            "relaunching even when ``auto_relaunch=True``."
        ),
    )
    relaunch_backoff_base_s: float = Field(
        default=1.0,
        ge=0.1,
        le=30,
        description=(
            "v1.7.0: base delay between relaunch attempts. Attempt N "
            "waits ``base * 2**(N-1)`` seconds before retrying (1s / 2s / "
            "4s with the defaults)."
        ),
    )
    session_max_count: int = Field(
        default=32,
        ge=1,
        le=512,
        description=(
            "v1.7.0: hard cap on concurrently live persistent sessions. "
            "Each session pins a Chromium BrowserContext (memory + "
            "processes); unbounded growth is the classic long-running "
            "MCP-daemon leak. Exceeding the cap fails session creation "
            "with an actionable message."
        ),
    )
    session_idle_ttl_s: float = Field(
        default=1800.0,
        ge=0,
        description=(
            "v1.7.0: idle time-to-live for persistent sessions, in "
            "seconds. Sessions untouched for longer than this are closed "
            "by a lazy reaper sweep that runs on session create/list (no "
            "background tasks). ``0`` disables idle reaping."
        ),
    )
    profile_sweep_max_age_h: float = Field(
        default=24.0,
        ge=0,
        description=(
            "v1.7.0: on browser start, ephemeral profile directories "
            "under ``<base_dir>/.webtool/browser-profiles`` whose newest "
            "content is older than this many hours are removed "
            "(orphans from crashed runs). The live profile and anything "
            "that looks owned by a possibly-live process are skipped. "
            "``0`` disables the sweep."
        ),
    )
    # --- v1.7.0 (Wave 2F): fingerprint coherence --------------------------
    coherent_fingerprint: bool = Field(
        default=True,
        description=(
            "v1.7.0 (Wave 2F): keep the launched context's identity "
            "self-consistent. When True (default) and "
            "``user_agent_mode='random'``, the rotated User-Agent is drawn "
            "ONLY from the OS family implied by ``locale`` (e.g. a "
            "``de-DE`` / ``en-US`` context never advertises a Linux/macOS "
            "UA on a Windows-looking fingerprint), so the UA OS, "
            "navigator.platform, locale, and timezone no longer "
            "contradict each other. The GUARANTEE is intra-context "
            "coherence of (UA OS family <-> platform token) derived from "
            "the configured locale -- it is NOT a stealth/bypass claim. "
            "Set False to restore the pre-v1.7.0 cross-OS rotation. Has no "
            "effect for ``user_agent_mode='explicit'`` / "
            "``'playwright_default'`` (the UA is operator-pinned there)."
        ),
    )

    @model_validator(mode="after")
    def _validate_user_agent_mode(self) -> BrowserConfig:
        """v1.6.9: ``user_agent_mode='explicit'`` requires ``user_agent``."""
        from .exceptions import ConfigError

        if self.user_agent_mode == "explicit" and not self.user_agent:
            raise ConfigError(
                "BrowserConfig.user_agent_mode='explicit' requires user_agent "
                "to be set to a non-empty string. Use 'random' or "
                "'playwright_default' to avoid pinning a specific UA."
            )
        return self

    @model_validator(mode="after")
    def _validate_isolation_and_cdp(self) -> BrowserConfig:
        """Enforce safety rules around isolation and CDP."""
        from .exceptions import ConfigError

        if self.attach_existing_browser:
            raise ConfigError(
                "BrowserConfig.attach_existing_browser=True is not "
                "supported. webTool only controls browsers it launched "
                "itself; attaching to an existing/personal Chrome would "
                "expose the user's cookies, history, and logged-in "
                "accounts. Use cdp_enabled=True with isolation_mode=True "
                "for CDP control of a webTool-launched browser."
            )

        if self.backend == "cdp_owned" and not self.cdp_enabled:
            raise ConfigError(
                "BrowserConfig.backend='cdp_owned' implies cdp_enabled=True. "
                "Either set cdp_enabled=True or switch backend to 'playwright'."
            )

        # v1.6.10: use the same loopback predicate as ``remote_cdp_url``
        # so 127.0.0.0/8 and ::1 are accepted (mirrors the v1.6.8 C-3 fix
        # that widened the remote_cdp loopback check beyond the literal
        # {127.0.0.1, localhost} set).
        if self.cdp_enabled and not _is_loopback_host(self.cdp_host):
            raise ConfigError(
                f"BrowserConfig.cdp_host={self.cdp_host!r} is not a loopback "
                "address. Binding the Chrome DevTools Protocol port to a "
                "non-loopback interface would expose browser control to the "
                "network. Accepted: 127.0.0.0/8, ::1, localhost."
            )

        if self.cdp_enabled and not self.isolation_mode:
            raise ConfigError(
                "BrowserConfig.cdp_enabled=True requires isolation_mode=True. "
                "CDP discovery reads DevToolsActivePort from the user-data-dir, "
                "which only exists when isolation_mode is enabled."
            )

        if self.profile_mode == "named" and not self.profile_dir:
            raise ConfigError(
                "BrowserConfig.profile_mode='named' requires profile_dir to be "
                "set. Either set profile_dir to a directory path or switch to "
                "profile_mode='ephemeral'."
            )

        # --- v1.6.8: remote CDP backend ---
        if self.backend == "remote_cdp":
            if not self.remote_cdp_url:
                raise ConfigError(
                    "BrowserConfig.backend='remote_cdp' requires remote_cdp_url. "
                    "Set remote_cdp_url to a ws://127.0.0.1:<port>/devtools/browser/<uuid> "
                    "endpoint exposed by an externally-launched Chromium."
                )
            parsed = urlparse(self.remote_cdp_url)
            if parsed.scheme not in {"ws", "wss"}:
                raise ConfigError(
                    f"BrowserConfig.remote_cdp_url must use ws:// or wss://, "
                    f"got scheme {parsed.scheme!r}."
                )
            if not _is_loopback_host(parsed.hostname):
                # v1.6.8 (review C-3): the old check used an exact-string
                # allowlist {127.0.0.1, localhost, ::1} that missed the
                # rest of the 127.0.0.0/8 range. Use ipaddress.ip_address
                # so 127.0.0.2 / 127.255.255.254 are also classified as
                # loopback while public IPs and DNS names (other than
                # 'localhost') stay rejected.
                raise ConfigError(
                    f"BrowserConfig.remote_cdp_url host {parsed.hostname!r} is "
                    "not loopback. Connecting to a non-loopback CDP endpoint "
                    "would let an external process pose as the local browser. "
                    "Use 127.0.0.0/8, ::1, or localhost."
                )
            # remote_cdp is incompatible with the owned-launch knobs -- we
            # don't own the user-data-dir on a remote browser, and there's
            # no DevToolsActivePort file to discover the port from.
            if self.isolation_mode:
                raise ConfigError(
                    "BrowserConfig.backend='remote_cdp' is incompatible with "
                    "isolation_mode=True. Isolation mode owns a user-data-dir; "
                    "the remote browser already has its own profile."
                )
            if self.cdp_enabled:
                raise ConfigError(
                    "BrowserConfig.backend='remote_cdp' is incompatible with "
                    "cdp_enabled=True. cdp_enabled triggers a launch flow with "
                    "DevToolsActivePort discovery; remote_cdp connects to an "
                    "already-running browser instead."
                )
            # v1.6.9: loopback alone does not prove ownership -- a user's
            # personal Chrome on 127.0.0.1:9222 looks identical to a
            # webTool-launched browser. Require the caller to present a
            # token matching a file webTool wrote into the launcher's
            # profile dir. Checked AFTER URL/scheme/loopback/isolation
            # checks so structural issues surface first.
            if not self.remote_cdp_ownership_token:
                raise ConfigError(
                    "BrowserConfig.backend='remote_cdp' requires "
                    "remote_cdp_ownership_token (v1.6.9). Read it from "
                    "<profile_dir>/.webtool-ownership of the webTool-launched "
                    "browser you want to attach to. See "
                    "web_agent.ownership.OwnershipToken.read."
                )
            if not self.remote_cdp_profile_dir:
                raise ConfigError(
                    "BrowserConfig.backend='remote_cdp' requires "
                    "remote_cdp_profile_dir (v1.6.9) so the ownership "
                    "token can be verified against the on-disk file."
                )
        elif self.remote_cdp_url is not None:
            # User set remote_cdp_url without flipping backend -- surface
            # the no-op rather than letting it silently disappear.
            raise ConfigError(
                "BrowserConfig.remote_cdp_url was set but backend is "
                f"{self.backend!r}. Set backend='remote_cdp' to use it, or "
                "clear remote_cdp_url to silence this error."
            )

        return self

    # v1.6.16 (review CO-1): scope env-var lookup to the WEB_AGENT_BROWSER__
    # namespace AppConfig already uses for nesting. Without this, a
    # standalone ``BrowserConfig()`` -- and, because AppConfig builds every
    # sub-config via ``default_factory``, even a default ``AppConfig()`` --
    # would read BARE unprefixed env vars (e.g. ``HEADLESS``), letting a
    # stray shell/CI var silently flip settings. Mirrors the
    # SkillsConfig/WorkspaceConfig fix (review I-2). The nested
    # ``WEB_AGENT_BROWSER__<FIELD>`` path via AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_BROWSER__"}


def _default_search_providers() -> list[Literal["searxng", "ddgs", "playwright"]]:
    """Default provider chain for :attr:`SearchConfig.providers`.

    A bare lambda would infer ``list[str]``, which mypy rejects against the
    Literal-typed field (list is invariant), so the factory is annotated.
    """
    return ["searxng", "ddgs", "playwright"]


class SearchConfig(BaseSettings):
    """Web search parameters and multi-provider chain configuration.

    Provider chain (NEW in 1.4.0): ``providers`` lists search backends
    in priority order. Each provider is tried until one returns
    results. Available providers:

    - ``"searxng"`` -- self-hosted SearXNG instance via JSON API (set
      ``searxng_base_url`` to enable, otherwise silently skipped).
    - ``"ddgs"`` -- DuckDuckGo via the ``ddgs`` package (silently
      skipped when the optional dependency is missing).
    - ``"playwright"`` -- browser-driven Google + DDG HTML scraping
      (always available; slow but reliable fallback).

    To use only browser-based search: ``providers=["playwright"]``.
    To skip Playwright entirely: ``providers=["searxng", "ddgs"]``.
    """

    # v1.6.16 (review CO-7): ``max_results`` flows into provider slices and
    # Google's ``num=`` query param. A value <= 0 silently yields no results
    # (and a negative value is a nonsensical slice bound). Require >= 1.
    max_results: int = Field(default=10, ge=1)
    search_url: str = "https://www.google.com/search"
    language: str = "en"
    region: str = "us"
    safe_search: bool = Field(
        default=False,
        description=(
            "Whether to request the provider's family-safe / explicit-content "
            "filter. Default **False** so research and OSINT workflows are not "
            "silently scrubbed of legitimate results (adult-adjacent news, "
            "security/malware analysis, medical topics). Set True to enable "
            "the provider's safe-search filter (SearXNG ``safesearch=1``, "
            "Google ``safe=active``) when results are surfaced to end users."
        ),
    )

    # Multi-provider chain (NEW in 1.4.0)
    # v1.6.16 deep-review fix: typed as a Literal so a typo'd provider name
    # ('duckduckgo', 'ddg') is rejected at config time. Previously an unknown
    # name was silently DROPPED from the chain (search_engine builds it with
    # ``if name in catalog``), degrading the chain -- or, if all names were
    # wrong, yielding an empty chain and a misleading "providers exhausted ([])"
    # error on every search.
    providers: list[Literal["searxng", "ddgs", "playwright"]] = Field(
        default_factory=_default_search_providers,
        description=(
            "Search providers tried in priority order. First non-empty "
            "result wins. Available: 'searxng', 'ddgs', 'playwright'."
        ),
    )
    searxng_base_url: Optional[str] = Field(
        default=None,
        description=(
            "Base URL of self-hosted SearXNG (e.g. http://localhost:8888). "
            "When None, the SearXNG provider is silently skipped."
        ),
    )
    searxng_timeout: float = Field(
        default=10.0,
        gt=0,
        description=(
            "HTTP timeout (seconds) for SearXNG JSON API calls. v1.6.16 "
            "(review CO-7): must be > 0 -- a zero/negative timeout is "
            "rejected by httpx / disables the timeout."
        ),
    )
    circuit_cooldown_s: float = Field(
        default=60.0,
        ge=0,
        description=(
            "v1.7.0 (Wave 2E): seconds a search provider is skipped after it "
            "blocks (CAPTCHA / rate-limit) or errors, before the chain "
            "re-probes it. A bounded per-provider circuit breaker so a "
            "just-blocked provider is not hammered on every subsequent search. "
            "0 disables the breaker (every provider tried every call)."
        ),
    )

    # v1.6.16 (review CO-1): scope env-var lookup to WEB_AGENT_SEARCH__ so a
    # bare ``REGION`` / ``LANGUAGE`` / ``MAX_RESULTS`` env var can't override
    # search settings. Nested ``WEB_AGENT_SEARCH__<FIELD>`` via AppConfig
    # still works.
    model_config = {"env_prefix": "WEB_AGENT_SEARCH__"}


class FetchConfig(BaseSettings):
    """Page fetching, rendering wait conditions, and retry settings.

    Use ``retry_policy`` for declarative retry profiles
    (``"fast"`` | ``"balanced"`` | ``"paranoid"``).  When ``retry_policy``
    is set and the numeric retry fields (``max_retries``/``retry_base_delay``/
    ``retry_max_delay``) are left at their defaults, the policy values
    are applied automatically.  If the user explicitly sets any numeric
    retry field, it overrides the policy.
    """

    # v1.6.16 deep-review fix: typed as a Literal of the values Playwright's
    # ``page.goto(wait_until=...)`` accepts, so a typo ('networkIdle') is
    # rejected at config time instead of failing EVERY fetch at runtime.
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(
        default="domcontentloaded",
        description=(
            "Playwright wait condition for page navigation. Default "
            "'domcontentloaded' (DOM parsed) is fast and robust against "
            "pages with analytics/polling that prevent 'networkidle' from "
            "ever firing. Set to 'networkidle' for JS-heavy sites where "
            "content arrives after the initial DOM, or pair with "
            "extra_wait_ms / wait_for_selector to give async hydration "
            "time. Options: 'domcontentloaded' | 'load' | 'networkidle'."
        ),
    )
    wait_for_selector: Optional[str] = None
    extra_wait_ms: int = 0
    # v1.6.14 (review D-8): constrained to the named profiles so an invalid
    # value is rejected by pydantic at field assignment (in addition to the
    # defensive ConfigError wrap in ``_apply_retry_policy`` -- review D-5).
    # Values mirror :class:`web_agent.utils.RetryPolicy`.
    retry_policy: Literal["fast", "balanced", "paranoid"] = "balanced"
    # v1.6.16 deep-review fix: ``utils.async_retry`` treats ``max_retries`` as
    # TOTAL attempts and raises ``ValueError('max_retries must be >= 1')`` at
    # decoration time. So ``max_retries=0`` (a natural "disable retries"
    # sentinel) passed config validation and then made EVERY fetch raise a raw
    # ValueError. ``ge=1`` rejects it at config time; the value is total
    # attempts (1 = no retries). Negative backoff delays are likewise invalid.
    max_retries: int = Field(default=3, ge=1)
    retry_base_delay: float = Field(default=1.0, ge=0)
    retry_max_delay: float = Field(default=30.0, gt=0)
    non_retryable_status_codes: list[int] = Field(
        default_factory=lambda: [400, 401, 403, 404, 405, 410, 451]
    )
    # v1.7.0: bot-challenge honesty. Interstitials (Cloudflare "Just a
    # moment...", DataDome, Akamai, PerimeterX, CAPTCHA walls) served with
    # HTTP 200 used to come back as FetchStatus.SUCCESS and get extracted
    # as if they were content; 403/503-wrapped JS challenges fast-failed
    # even though a real browser auto-passes them in a few seconds.
    challenge_detection_enabled: bool = Field(
        default=True,
        description=(
            "Detect bot-challenge / CAPTCHA interstitials in fetched HTML "
            "(structural markers only -- prose mentions of a vendor never "
            "trigger). Detected walls surface as FetchStatus.BLOCKED with "
            "FetchResult.challenge populated and actionable guidance in "
            "error_message, instead of silently returning interstitial "
            "HTML as SUCCESS. Set False to restore pre-v1.7.0 behaviour."
        ),
    )
    challenge_settle_ms: int = Field(
        default=3500,
        ge=0,
        le=30000,
        description=(
            "Milliseconds to wait before each re-check of an auto-settle-"
            "likely challenge (Cloudflare managed JS challenges typically "
            "clear in ~3-5s in a real browser). Applies per recheck round; "
            "CAPTCHA / block-page / rate-limit walls are never waited on."
        ),
    )
    challenge_max_rechecks: int = Field(
        default=2,
        ge=0,
        le=5,
        description=(
            "Maximum settle-and-recheck rounds for an auto-settle-likely "
            "challenge before giving up and returning BLOCKED. 0 disables "
            "the settle wait entirely (immediate BLOCKED on detection). "
            "Worst-case added latency is challenge_settle_ms * "
            "challenge_max_rechecks per challenged fetch."
        ),
    )

    @model_validator(mode="after")
    def _apply_retry_policy(self) -> FetchConfig:
        """Layer an explicit numeric override on top of the NAMED policy.

        v1.6.16 (review CO-5): the previous implementation was all-or-
        nothing -- setting ANY one numeric retry field skipped the entire
        policy block, so the OTHER two silently kept their class-level
        defaults (1.0 / 30.0), which equal BALANCED and NOT the requested
        policy. ``FetchConfig(retry_policy='paranoid', max_retries=9)`` thus
        produced paranoid's retry COUNT but balanced's BACKOFF -- the
        opposite of the 'paranoid' intent. Fix: resolve the named policy as
        the baseline, then overlay ONLY the numeric fields the caller
        explicitly set, so 'paranoid + my max_retries' = paranoid delays
        with the caller's retry count.
        """
        if self.retry_policy == "balanced":
            # BALANCED equals the class-level defaults, so there is nothing
            # to layer -- any user-set field already holds the intended
            # value and unset fields already hold the balanced default.
            return self

        explicit = self.model_fields_set

        # Lazy import to avoid circular dep
        from .exceptions import ConfigError
        from .utils import get_retry_policy

        try:
            kwargs = get_retry_policy(self.retry_policy)
        except ValueError as exc:
            # v1.6.14 (review D-5): surface a typed ConfigError (matching
            # the other config validators) instead of the bare ValueError
            # ``get_retry_policy`` raises. The Literal field (D-8) should
            # already block unknown values, so this is defense-in-depth
            # against a future widening of the field.
            raise ConfigError(
                f"FetchConfig.retry_policy={self.retry_policy!r} is not a "
                "valid retry policy. Choose from: 'fast', 'balanced', "
                "'paranoid'."
            ) from exc

        # Apply the named policy's value for every numeric field the caller
        # did NOT explicitly set; leave caller-set fields untouched.
        if "max_retries" not in explicit:
            self.max_retries = int(kwargs["max_retries"])
        if "retry_base_delay" not in explicit:
            self.retry_base_delay = float(kwargs["base_delay"])
        if "retry_max_delay" not in explicit:
            self.retry_max_delay = float(kwargs["max_delay"])
        return self

    # v1.6.16 (review CO-1): scope env-var lookup to WEB_AGENT_FETCH__ so a
    # bare ``MAX_RETRIES`` / ``WAIT_UNTIL`` env var can't override fetch
    # settings. Nested ``WEB_AGENT_FETCH__<FIELD>`` via AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_FETCH__"}


class DownloadConfig(BaseSettings):
    """File download settings including size limits and allowed types."""

    download_dir: str = "./downloads"
    # v1.6.16 (review CO-4): the streaming downloader computes
    # ``max_bytes = max_file_size_mb * 1024 * 1024`` raw (downloader.py).
    # A negative or zero value makes ``max_bytes`` <= 0, so the chunk guard
    # ``total + len(chunk) > max_bytes`` trips on the first byte and every
    # download is aborted (and the post-save guard deletes every file as
    # "oversize"). ``ge=1`` (the field is in MiB) rejects that at config
    # time. ``web_fetcher.py`` had a ``max(1, ...)`` band-aid at one call
    # site only; this is the single source of truth.
    max_file_size_mb: int = Field(default=100, ge=1)
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            ".pdf",
            ".csv",
            ".xlsx",
            ".xls",
            ".zip",
            ".json",
            ".txt",
            ".doc",
            ".docx",
            ".ppt",
            ".pptx",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".xml",
            ".html",
            ".htm",
            ".md",
            ".tar",
            ".gz",
        ]
    )

    @field_validator("allowed_extensions", mode="before")
    @classmethod
    def _normalize_allowed_extensions(cls, v: Any) -> Any:
        """v1.6.16 deep-review fix: normalize each entry to lowercase + a
        leading dot. The downloader gate compares ``filepath.suffix.lower()``
        against these entries, so a config value like ``'PDF'``, ``'pdf'`` (no
        dot), or ``'.XLSX'`` could never match and silently BLOCKED every
        download of that type (fail-closed). Mirrors the load-time normalization
        ``_normalize_domain_patterns`` already applies to domain lists. Empty
        entries are dropped; non-string entries pass through so pydantic can
        surface the type error naturally.
        """
        if not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if not isinstance(item, str):
                out.append(item)
                continue
            s = item.strip().lower()
            if not s:
                continue
            if not s.startswith("."):
                s = "." + s
            out.append(s)
        return out

    # v1.6.16 (review CO-1): scope env-var lookup to WEB_AGENT_DOWNLOAD__ so
    # a bare ``MAX_FILE_SIZE_MB`` / ``DOWNLOAD_DIR`` / ``ALLOWED_EXTENSIONS``
    # env var can't override download settings. Nested
    # ``WEB_AGENT_DOWNLOAD__<FIELD>`` via AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_DOWNLOAD__"}


class ExtractionConfig(BaseSettings):
    """Content extraction settings for the trafilatura/BS4/raw fallback chain.

    v1.7.0: ``default_max_chars`` caps **MCP-boundary responses only** --
    it is the per-call content window applied by the MCP server tools
    (web_fetch / web_search / web_search_best / web_research /
    web_fill_form_and_extract / web_observe) when the caller does not pass
    an explicit ``max_chars``. The Python API
    (:meth:`ContentExtractor.extract`, ``Agent.*``) stays unlimited by
    default; only an explicit ``max_chars=``/``offset=`` argument windows
    it there. Env override: ``WEB_AGENT_EXTRACTION__DEFAULT_MAX_CHARS``.
    """

    favor_precision: bool = False
    favor_recall: bool = True
    include_tables: bool = True
    include_links: bool = False
    include_comments: bool = False
    # v1.6.16 (review CO-7): minimum extracted-content length threshold in
    # chars. A negative threshold is meaningless (every result passes);
    # ``ge=0`` keeps the "accept any non-empty content" sentinel (0) valid.
    min_content_length: int = Field(default=50, ge=0)
    # v1.7.0: MCP-boundary default content window (chars). Applied per page
    # by the MCP server when the tool call carries no explicit max_chars.
    # Does NOT affect the Python API default (unlimited). 40k chars is
    # roughly 10k tokens -- large enough for most articles, small enough
    # that 2-3 tool calls don't blow out an LLM context window.
    default_max_chars: int = Field(
        default=40000,
        ge=1000,
        le=1_000_000,
        description=(
            "Default per-call content cap (chars) applied at the MCP "
            "boundary only, when the caller omits max_chars. The Python "
            "API remains unlimited unless max_chars is passed explicitly."
        ),
    )

    # v1.6.16 (review CO-1): scope env-var lookup to WEB_AGENT_EXTRACTION__
    # so bare ``INCLUDE_TABLES`` / ``FAVOR_RECALL`` env vars can't override
    # extraction settings. Nested ``WEB_AGENT_EXTRACTION__<FIELD>`` via
    # AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_EXTRACTION__"}


class AutomationConfig(BaseSettings):
    """Browser automation action settings."""

    # v1.6.16 (review CO-7): action timeout in ms; a negative timeout is
    # undefined behaviour in Playwright. ``ge=0`` keeps the "0 = no timeout"
    # sentinel valid.
    default_action_timeout: int = Field(default=10000, ge=0)
    screenshot_dir: str = "./screenshots"
    # v1.6.16 deep-review fix: Playwright's page.screenshot accepts only
    # 'png'/'jpeg'; a Literal rejects an invalid value at config time.
    screenshot_format: Literal["png", "jpeg"] = "png"
    # v1.6.16 (review CO-7): JPEG quality is a 0-100 percentage (ignored for
    # PNG). Constrain to that range so an out-of-band value can't reach
    # Playwright's ``page.screenshot(quality=...)``.
    screenshot_quality: int = Field(default=80, ge=0, le=100)
    stop_on_error: bool = True
    # v1.6.16 (review CO-7): per-action slow-mo delay in ms; never negative.
    slow_mo_actions: int = Field(default=0, ge=0)
    # v1.6.6: when False (default), session-owned ``execute_sequence`` calls
    # reuse the session's current tab (preserving scroll / viewport / cookies
    # across calls). Set True to restore v1.6.5 behavior where each
    # ``interact(url, ...)`` call against the same session_id opens a fresh
    # page within the session's BrowserContext.
    fresh_tab_per_call: bool = False

    # v1.6.16 (review CO-1): scope env-var lookup to WEB_AGENT_AUTOMATION__
    # so bare ``SCREENSHOT_DIR`` / ``STOP_ON_ERROR`` env vars can't override
    # automation settings. Nested ``WEB_AGENT_AUTOMATION__<FIELD>`` via
    # AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_AUTOMATION__"}


class SafetyConfig(BaseSettings):
    """Domain allow/deny lists, granular allow_* flags, safe mode, and per-call budget knobs.

    Empty ``allowed_domains`` means all hosts are allowed (subject to deny-list).
    Domain patterns use suffix-match semantics: ``example.com`` matches
    ``api.example.com`` and ``www.example.com`` but not ``notexample.com``.

    Granular safety flags (each independently configurable):

    - ``allow_js_evaluation`` (default **False**): controls ``EvaluateInput``
      actions which run arbitrary JavaScript in the browser context. Default
      False because LLM-supplied JS can exfiltrate cookies / read DOM in
      authenticated sessions. Opt in explicitly when you need it.
    - ``allow_downloads`` (default True): controls file-download actions.
      Disable to enforce read-only browsing.
    - ``allow_form_submit`` (default True): controls clicks on submit-typed
      buttons (heuristic match against text/role/selector).
    - ``block_private_ips`` (default True): SSRF protection -- blocks RFC1918,
      loopback, link-local (incl. AWS IMDS at 169.254.169.254).

    ``safe_mode`` (default False) is a master kill-switch: when True it
    overrides the three ``allow_*`` flags to False (regardless of their
    explicit settings). ``block_private_ips`` is independent of safe_mode.

    Budget knobs limit the cost of a single Agent method call:

    - ``max_pages_per_call``: stops fetching after N pages.
    - ``max_chars_per_call``: stops extracting after total chars exceeded.
    - ``max_time_per_call_seconds``: wall-clock cutoff for the call.
    """

    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)
    safe_mode: bool = False
    allow_js_evaluation: bool = False
    allow_downloads: bool = True
    allow_form_submit: bool = True
    block_private_ips: bool = True
    # v1.6.9: coordinate-click safety. Default True (CLICK_XY allowed).
    # When safe_mode=True or this is explicitly False, click_xy is
    # rejected outright. When True with allow_form_submit=False,
    # click_xy runs document.elementFromPoint(x, y) to inspect the
    # target and blocks clicks on submit/login/delete/pay controls.
    allow_coordinate_clicks: bool = True
    # v1.6.10: policy when click_xy's elementFromPoint inspection
    # returns [] (point lies outside any element) OR raises. "allow"
    # (default) clicks anyway -- matches the v1.6.9 permissive default
    # so existing callers see no behaviour change. "block" rejects --
    # right for strict callers who want "unknown == hostile".
    # Fires independently of ``allow_form_submit`` (review C-1 fix):
    # a caller running with form submits ALLOWED can still opt into
    # block-on-unknown by setting this knob to "block". safe_mode
    # forces "block" via ``_apply_safe_mode``.
    coordinate_click_unknown_policy: Literal["allow", "block"] = Field(
        default="allow",
        description=(
            "Click_xy policy when elementFromPoint inspection returns "
            "no element (point outside any element / JS error). 'allow' "
            "(default) lets the click proceed; 'block' rejects. Forced "
            "'block' in safe_mode. Independent of allow_form_submit -- "
            "fires whenever allow_coordinate_clicks=True."
        ),
    )
    # v1.6.7: upload-file safety. By default, ``UploadFileInput`` / the
    # top-level ``Agent.upload_file`` accepts only paths under
    # ``download.download_dir``. Without this fence, a prompt-injection
    # could call ``upload_file(selector=..., paths=["~/.ssh/id_rsa"])``
    # and exfiltrate arbitrary local files. Opt in to widen scope.
    allow_upload_outside_download_dir: bool = False

    # Normalize URLs / mixed-case input down to bare hostnames before any
    # ``check_domain_allowed`` consultation. Catches the common foot-gun
    # of passing ``"https://evil.com"`` as a deny pattern (which would
    # otherwise silently never match because the comparator looks at
    # parsed hostnames).
    _normalize_allowed = field_validator("allowed_domains", mode="before")(
        _normalize_domain_patterns
    )
    _normalize_denied = field_validator("denied_domains", mode="before")(_normalize_domain_patterns)
    probe_binary_urls: bool = Field(
        default=True,
        description=(
            "When True, fetch_and_extract sends a HEAD request for URLs "
            "that don't have a known download extension to detect "
            "extensionless PDFs / XLSX / DOCX served via Content-Type "
            "or Content-Disposition headers. Adds one round-trip per "
            "fetch but recovers many real-world document URLs. Disable "
            "to skip the probe and rely solely on URL extension."
        ),
    )
    # v1.6.14 (review D-6): budget knobs gate per-call cost. A negative (or
    # zero) value would make the very first page/char/second trip the budget
    # and raise BudgetExceededError immediately -- a self-inflicted DoS and a
    # prompt-injection foot-gun. Require >= 1 so the smallest valid budget is
    # "one unit".
    max_pages_per_call: int = Field(default=50, ge=1)
    max_chars_per_call: int = Field(default=1_000_000, ge=1)
    max_time_per_call_seconds: float = Field(default=300.0, gt=0)

    # --- Politeness layer (rate limit + robots.txt) ---
    rate_limit_per_host_rps: float = Field(
        default=2.0,
        ge=0,
        description=(
            "Per-host rate cap in requests/second. ``0`` disables rate "
            "limiting entirely (the documented sentinel). Negative values "
            "are rejected (v1.6.14, review D-7) -- previously a negative rps "
            "silently disabled limiting instead of erroring. Applies to "
            "fetch, download, and search operations."
        ),
    )
    respect_robots_txt: bool = Field(
        default=True,
        description=(
            "If True, fetch and obey each host's robots.txt before "
            "requesting pages. Missing or unreachable robots.txt is "
            "treated as allow-all."
        ),
    )
    robots_user_agent: str = Field(
        default="web-agent-toolkit",
        description=(
            "User-Agent token used when fetching robots.txt and matched "
            "against User-agent rule groups inside it."
        ),
    )

    @model_validator(mode="after")
    def _apply_safe_mode(self) -> SafetyConfig:
        """When safe_mode is True, force all allow_* flags to False.

        ``block_private_ips`` is intentionally NOT touched: it is a
        hardening flag (True = protection on), so safe_mode must never
        flip it off. Likewise ``probe_binary_urls`` is a behavioural
        knob, not a capability escape-hatch.
        """
        if self.safe_mode:
            self.allow_js_evaluation = False
            self.allow_downloads = False
            self.allow_form_submit = False
            self.allow_coordinate_clicks = False
            # v1.6.14 (review D-1): the previous kill-switch missed this
            # escape-hatch, so safe_mode left arbitrary-path uploads
            # enabled despite the "force all allow_* flags to False"
            # contract. Reset it too.
            self.allow_upload_outside_download_dir = False
            # v1.6.10: also pin the unknown-policy. Defensive only --
            # allow_coordinate_clicks=False already blocks click_xy --
            # but a caller mutating allow_coordinate_clicks back to True
            # at runtime should not silently revert to "allow".
            self.coordinate_click_unknown_policy = "block"
        return self

    # v1.6.16 (review CO-1): THE scariest case in the report. Without an
    # explicit prefix, a default ``AppConfig()`` (the documented
    # ``async with Agent() as agent:`` path) builds SafetyConfig via
    # ``default_factory``, which then reads BARE env vars: a stray
    # ``BLOCK_PRIVATE_IPS=false`` silently disabled SSRF protection and
    # ``ALLOW_UPLOAD_OUTSIDE_DOWNLOAD_DIR=true`` dropped the file-exfil
    # fence -- no error, no log. Scope lookup to the WEB_AGENT_SAFETY__
    # namespace AppConfig already uses for nesting; the nested
    # ``WEB_AGENT_SAFETY__<FIELD>`` path via AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_SAFETY__"}


class DebugConfig(BaseSettings):
    """Auto-capture HTML/screenshot/error context on failures for debugging.

    When ``enabled`` is True, every fetch/action/download failure dumps a
    snapshot to ``debug_dir/{correlation_id}/{timestamp}-{label}.{html|png|json}``
    so the failure can be reproduced and diagnosed offline.
    """

    enabled: bool = False
    debug_dir: str = "./debug"
    capture_html: bool = True
    capture_screenshot: bool = True
    # v1.6.16 (review CO-7): per-call artifact cap; never negative.
    max_artifacts_per_call: int = Field(default=5, ge=0)

    # v1.6.16 (review CO-1): scope env-var lookup to WEB_AGENT_DEBUG__ so a
    # bare ``ENABLED`` / ``DEBUG_DIR`` env var can't override debug settings.
    # Nested ``WEB_AGENT_DEBUG__<FIELD>`` via AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_DEBUG__"}


class AuditConfig(BaseSettings):
    """Append-only JSONL audit log of every Agent operation.

    Distinct from regular logging: only records public Agent calls
    (start + end + status + elapsed). Useful as a tamper-evident
    audit trail for AI-agent runs, separate from chatty internal logs.
    """

    enabled: bool = False
    audit_log_path: str = "./audit.jsonl"

    # v1.6.16 (review CO-1): scope env-var lookup to WEB_AGENT_AUDIT__ so a
    # bare ``ENABLED`` env var can't silently enable/disable the audit log.
    # Nested ``WEB_AGENT_AUDIT__<FIELD>`` via AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_AUDIT__"}


class CacheConfig(BaseSettings):
    """Disk-backed TTL cache for fetch results and search responses.

    When ``enabled``, every successful ``WebFetcher.fetch(url)`` and
    ``SearchEngine.search(query)`` writes its result to disk; subsequent
    calls within ``ttl_seconds`` return the cached payload without
    hitting the network. Best-effort LRU-by-mtime eviction keeps the
    cache directory under ``max_cache_mb``.

    Disabled by default -- enable explicitly when you want to avoid
    re-fetching the same pages across runs (research workflows,
    experiments, dev iteration).
    """

    enabled: bool = False
    cache_dir: str = "./cache"
    # v1.6.16 (review CO-7): TTL seconds must be positive; max cache size in
    # MiB must be >= 1 (a non-positive cap would evict everything / make the
    # cache unusable).
    ttl_seconds: float = Field(default=3600.0, gt=0)
    max_cache_mb: int = Field(default=100, ge=1)

    # v1.6.16 (review CO-1): scope env-var lookup to WEB_AGENT_CACHE__ so a
    # bare ``ENABLED`` / ``CACHE_DIR`` env var can't override cache settings.
    # Nested ``WEB_AGENT_CACHE__<FIELD>`` via AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_CACHE__"}


class SkillsConfig(BaseSettings):
    """v1.6.7: Domain Skills registry.

    A "skill" is a markdown file at
    ``<skill_dir>/<domain>/<name>.md`` with YAML frontmatter
    (name, domain, description, inputs, output_schema, runnable) plus
    structured sections (Use case / Recommended flow / Known selectors
    / Known traps). Skills make webTool accumulate reusable knowledge
    about specific websites instead of rediscovering quirks every run.

    Three skill directories with priority order:
      ``builtin`` (lowest) < ``workspace`` < ``project`` (highest)

    Bundled skills (under ``web_agent/builtin_skills/``) ship with a
    Python runner and are dispatchable via
    ``Agent.apply_domain_skill``. User markdown skills are
    informational only unless the workspace mode allows adjacent
    Python helpers.

    v1.6.9: ``enabled`` is **deprecated**. It only ever governed the
    project-tier load (not workspace or builtin), so the new canonical
    name is ``project_skills_enabled``. The old ``enabled`` alias keeps
    working via ``AliasChoices`` for one release and will be removed in
    v1.7.0; using it now emits a ``DeprecationWarning``.
    """

    project_skills_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("project_skills_enabled", "enabled"),
        description=(
            "v1.6.9: master switch for the *project-tier* skill load -- "
            "when False, ``skill_dirs`` are not scanned. Workspace and "
            "bundled skills are governed by ``workspace.enabled`` and "
            "``builtin_skills_enabled`` respectively, so "
            "``Agent.get_domain_skills`` can still return entries from "
            "those tiers when this flag is False. Old name ``enabled`` "
            "is accepted as a deprecated alias for one release."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _warn_deprecated_enabled(cls, data: Any) -> Any:
        """v1.6.9: emit DeprecationWarning when the old name is used."""
        if isinstance(data, dict) and "enabled" in data and "project_skills_enabled" not in data:
            import warnings

            warnings.warn(
                "SkillsConfig.enabled is deprecated in v1.6.9; use "
                "project_skills_enabled instead. The alias will be "
                "removed in v1.7.0.",
                DeprecationWarning,
                stacklevel=2,
            )
        return data

    skill_dirs: list[str] = Field(
        default_factory=lambda: ["./.webtool-skills"],
        description=(
            "Project skill directories (highest priority). Each entry is "
            "resolved against ``AppConfig.base_dir`` when relative. The "
            "first matching ``(domain, name)`` wins; later entries are "
            "overrides."
        ),
    )
    builtin_skills_enabled: bool = Field(
        default=True,
        description=(
            "Include bundled skills shipped under "
            "``web_agent/builtin_skills/``. Default True -- the bundled "
            "skills are the only runnable skills in v1.6.7. Disable to "
            "audit-only mode where only user-authored skills appear."
        ),
    )

    # v1.6.9 review (I-2): without an explicit env_prefix, a standalone
    # ``SkillsConfig()`` would pick up a bare ``ENABLED`` env var and
    # spuriously trigger the v1.6.9 deprecation warning. Scope env-var
    # lookup to the same WEB_AGENT_SKILLS__ namespace AppConfig already
    # uses for nesting.
    model_config = {"env_prefix": "WEB_AGENT_SKILLS__"}


class WorkspaceConfig(BaseSettings):
    """v1.6.7: Agent-editable workspace with safety modes.

    A workspace is a directory the agent reads from and (in some
    modes) writes to. Default layout::

        .webtool-workspace/
            domain-skills/    # user-authored markdown skills (auto-loaded)
            notes/            # agent-authored free-text notes
            helpers.py        # Python helpers (gated by mode)

    Default ``enabled=False`` for safety: the agent must explicitly opt
    in. When enabled, the default mode is ``markdown_skills_only`` --
    the agent can read and write ``.md`` files but cannot execute
    Python.
    """

    enabled: bool = Field(
        default=False,
        description="Master switch. When False, workspace is invisible to Agent.",
    )
    # NB: cannot be named ``path`` -- pydantic-settings on this BaseSettings
    # subclass would pull from the ``PATH`` environment variable (a real,
    # always-set foot-gun on every OS) before applying the field default.
    workspace_dir: str = Field(
        default="./.webtool-workspace",
        description=(
            "Workspace root. Resolved against ``AppConfig.base_dir`` when "
            "relative. Created on first write if missing."
        ),
    )
    mode: Literal[
        "read_only",
        "markdown_skills_only",
        "reviewed_python_helpers",
        "unsafe_python_helpers",
    ] = Field(
        default="markdown_skills_only",
        description=(
            "Safety mode for workspace writes. ``read_only`` blocks all "
            "writes. ``markdown_skills_only`` (default) allows .md files "
            "under domain-skills/ only. ``reviewed_python_helpers`` adds "
            "helpers.py writes but execution requires explicit opt-in. "
            "``unsafe_python_helpers`` removes all restrictions."
        ),
    )
    audit_helper_usage: bool = Field(
        default=True,
        description="Log every workspace write to the audit log when audit is enabled.",
    )
    execute_helpers: bool = Field(
        default=False,
        description=(
            "When mode is ``reviewed_python_helpers`` or "
            "``unsafe_python_helpers``, controls whether ``helpers.py`` is "
            "imported and made available to skills. Default False -- "
            "writing helpers is allowed, executing them is a second opt-in."
        ),
    )

    # v1.6.16 (review CO-1): a prior pass renamed ``path`` -> ``workspace_dir``
    # to dodge the always-set ``PATH`` env var, but the other fields still
    # read BARE env vars -- e.g. a stray ``EXECUTE_HELPERS=true`` would
    # silently enable Python-helper execution on a default ``AppConfig()``.
    # Scope lookup to the WEB_AGENT_WORKSPACE__ namespace AppConfig uses for
    # nesting, matching the other sub-configs. Nested
    # ``WEB_AGENT_WORKSPACE__<FIELD>`` via AppConfig still works.
    model_config = {"env_prefix": "WEB_AGENT_WORKSPACE__"}


class DiagnosticsConfig(BaseSettings):
    """v1.6.8: Network capture, download-intent capture, post-action
    screenshots, and session replay traces.

    All switches default False to match the v1.6.6/v1.6.7 opt-in posture
    -- existing callers see zero behavior change. Enable individually:

    - ``capture_network=True`` hooks ``page.on(request|response|requestfailed)``
      on every Page created by the Agent and surfaces them as
      ``FetchResult.network_events`` / ``ActionSequenceResult.network_events``.
    - ``capture_download_intents=True`` adds ``page.on("download")`` notification
      (separate from the downloader's explicit ``page.expect_download`` consumer)
      so the URL of any page-triggered download is recorded even when not saved.
    - ``screenshot_after_action=True`` captures a best-effort screenshot
      after every successful action in ``execute_sequence`` to
      ``automation.screenshot_dir`` (paths guarded by ``safe_join_path``).
    - ``trace_enabled=True`` writes a per-session JSONL action log under
      ``trace_dir`` so ``Agent.replay_trace(<file>)`` can re-execute.
    """

    capture_network: bool = Field(
        default=False,
        description=(
            "Hook ``page.on(request|response|requestfailed)`` on every Page "
            "the Agent creates. Off by default; opt in to populate "
            "``FetchResult.network_events`` and ``api_candidates``."
        ),
    )
    max_network_events: int = Field(
        default=500,
        ge=1,
        le=10000,
        description=(
            "Hard cap on retained events per Page. The per-Page deque uses "
            "``maxlen=max_network_events``; oldest events are evicted in "
            "O(1) when the cap is exceeded."
        ),
    )
    network_resource_types: list[str] = Field(
        default_factory=lambda: ["xhr", "fetch", "document"],
        description=(
            "Playwright ``request.resource_type`` values to record. Default "
            "skips image/font/stylesheet/media noise. Set to an empty list "
            "to record all resource types."
        ),
    )
    include_request_headers: bool = Field(
        default=False,
        description=(
            "Capture request headers on each NetworkEvent. Off by default "
            "because headers commonly contain Authorization / Cookie values "
            "an LLM consumer shouldn't see."
        ),
    )
    include_response_headers: bool = Field(
        default=False,
        description="Capture response headers on each NetworkEvent. Off by default.",
    )
    capture_response_bodies: bool = Field(
        default=False,
        description=(
            "v1.6.12: capture response body text into ``NetworkEvent.body_text`` "
            "for responses whose Content-Type matches ``body_capture_content_types``. "
            "Off by default because body capture costs memory (each event holds "
            "up to ``max_response_body_bytes`` bytes). Required for "
            "``ContentExtractor.extract(prefer_api=True)`` to find usable JSON "
            "payloads. Capture is async-scheduled from the response handler; "
            "callers needing bodies must ``await NetworkCollector.wait_for_pending_bodies()`` "
            "before snapshotting (already wired into :meth:`WebFetcher.fetch`)."
        ),
    )
    max_response_body_bytes: int = Field(
        default=262144,
        ge=1024,
        le=10 * 1024 * 1024,
        description=(
            "v1.6.12: per-response body cap in bytes. Bodies larger than this "
            "are truncated and ``NetworkEvent.body_truncated`` is set to True. "
            "Default 256 KiB -- enough for the typical API JSON payload, small "
            "enough that 500 events x 256 KiB = ~125 MiB worst-case memory use. "
            "Only consulted when ``capture_response_bodies=True``."
        ),
    )
    body_capture_content_types: list[str] = Field(
        default_factory=lambda: ["application/json", "application/ld+json", "text/json"],
        description=(
            "v1.6.12: Content-Type prefixes whose response bodies should be "
            "captured. Matched case-insensitively against the start of the "
            "response Content-Type (before any '; charset=...' suffix). Default "
            "covers JSON variants. Add 'text/html' (cautious -- HTML can be "
            "large) or other types for non-JSON capture."
        ),
    )
    capture_download_intents: bool = Field(
        default=False,
        description=(
            "Attach ``page.on('download')`` as a notification listener and "
            "record the download URL on every Page. The listener also calls "
            "``download.delete()`` so the tmpfile doesn't pile up when no "
            "explicit ``expect_download`` consumer is active."
        ),
    )
    screenshot_after_action: bool = Field(
        default=False,
        description=(
            "When True, ``BrowserActions.execute_sequence`` captures a "
            "best-effort PNG screenshot after each successful action under "
            "``automation.screenshot_dir`` (file name "
            "``verify-<correlation_id>-<index>.png``). Failures are logged "
            "at DEBUG and never fail the sequence."
        ),
    )
    trace_enabled: bool = Field(
        default=False,
        description=(
            "When True, every action executed inside an interactive Session "
            "appends a JSONL entry to "
            "``<trace_dir>/<session_id>.jsonl`` with "
            "``{ts, ordinal, session_id, correlation_id, method, args, "
            "status, elapsed_ms}``. Replayable via ``Agent.replay_trace``."
        ),
    )
    trace_dir: str = Field(
        default="./.webtool-audit/traces",
        description=(
            "Directory for per-session trace files. Resolved against "
            "``AppConfig.base_dir`` when relative. Created on first write."
        ),
    )

    # v1.6.16 (review CO-1): scope env-var lookup to WEB_AGENT_DIAGNOSTICS__
    # so bare env vars can't override diagnostics settings. Nested
    # ``WEB_AGENT_DIAGNOSTICS__<FIELD>`` via AppConfig still works (see
    # tests/test_v168_diagnostics_config.py).
    model_config = {"env_prefix": "WEB_AGENT_DIAGNOSTICS__"}


class ProxyConfig(BaseSettings):
    """v1.7.0 (Wave 2F): outbound proxy for every webTool egress path.

    The web is actively closing to agents (Cloudflare default-blocks AI
    crawlers; a vanilla local browser is flagged and blocked within
    seconds). A proxy is the single most common operator control that
    makes *compliant* access possible -- routing through a residential /
    datacenter egress the operator is authorised to use. This config is
    the one place that egress is set, threaded into BOTH the Playwright
    launch (``proxy={...}`` on ``chromium.launch`` /
    ``launch_persistent_context``) and the httpx side-paths (HEAD probe /
    binary fetch) so the browser and the bare-HTTP requests share one
    identity instead of leaking two different source IPs.

    This is NOT a stealth-bypass promise -- it is the control surface
    operators need. When ``server`` is unset (the default), every code
    path behaves exactly as pre-v1.7.0: no proxy kwarg is passed anywhere.

    Fields map to Playwright's proxy dict:

    * ``server`` -- ``"http://host:port"`` / ``"https://host:port"`` /
      ``"socks5://host:port"``. Scheme is validated when set.
    * ``username`` / ``password`` -- proxy auth (omitted when unset).
    * ``bypass`` -- comma-separated no-proxy hosts (Playwright
      ``bypass``); also fed to httpx as ``NO_PROXY`` semantics via the
      mounts the fetcher builds.

    Env loading (matches the sub-config discipline)::

        WEB_AGENT_PROXY__SERVER=http://127.0.0.1:8080
        WEB_AGENT_PROXY__USERNAME=alice
        WEB_AGENT_PROXY__PASSWORD=secret
        WEB_AGENT_PROXY__BYPASS=localhost,127.0.0.1,*.internal
    """

    server: Optional[str] = Field(
        default=None,
        description=(
            "Proxy server URL, e.g. ``http://host:port`` or "
            "``socks5://host:port``. When None (default), NO proxy is "
            "applied to any egress path -- pre-v1.7.0 behaviour. Scheme "
            "must be one of http / https / socks5 when set."
        ),
    )
    username: Optional[str] = Field(
        default=None,
        description="Proxy username for authenticated proxies. Omitted when None.",
    )
    password: Optional[str] = Field(
        default=None,
        description="Proxy password for authenticated proxies. Omitted when None.",
    )
    bypass: Optional[str] = Field(
        default=None,
        description=(
            "Comma-separated list of hosts that bypass the proxy "
            "(Playwright ``bypass``). Example: "
            "``localhost,127.0.0.1,*.internal``. Omitted when None."
        ),
    )

    @field_validator("server", mode="after")
    @classmethod
    def _validate_server_scheme(cls, v: Optional[str]) -> Optional[str]:
        """Reject a non-http/https/socks5 proxy scheme at config time.

        Playwright and httpx both accept only these schemes; an unknown
        scheme (or a bare ``host:port`` with no scheme) would fail deep
        inside the launch / client construction with an opaque error.
        Surface it as a clean ConfigError instead.
        """
        if v is None:
            return v
        s = v.strip()
        if not s:
            # An explicit empty string is a misconfiguration -- treat it
            # as "no proxy" so it cannot accidentally pass a blank server
            # string into Playwright (which rejects it).
            return None
        parsed = urlparse(s)
        allowed = {"http", "https", "socks5"}
        if parsed.scheme.lower() not in allowed:
            from .exceptions import ConfigError

            raise ConfigError(
                f"ProxyConfig.server={v!r} has unsupported scheme "
                f"{parsed.scheme!r}. Use one of: "
                "http://host:port, https://host:port, socks5://host:port."
            )
        if not parsed.hostname:
            from .exceptions import ConfigError

            raise ConfigError(
                f"ProxyConfig.server={v!r} is missing a host. Expected "
                "scheme://host:port, e.g. http://127.0.0.1:8080."
            )
        return s

    def is_active(self) -> bool:
        """True iff a proxy server is configured (so callers can gate the
        proxy kwarg without re-checking ``server is not None`` everywhere)."""
        return bool(self.server)

    def playwright_proxy(self) -> Optional[dict[str, str]]:
        """Build the Playwright ``proxy=`` dict, or ``None`` when inactive.

        Returns a dict with ``server`` always present and
        ``username`` / ``password`` / ``bypass`` included only when set,
        so callers pass ``proxy=cfg.playwright_proxy()`` and simply OMIT
        the kwarg entirely when this returns ``None`` (never pass
        ``proxy=None`` vs absent inconsistently).
        """
        if not self.server:
            return None
        proxy: dict[str, str] = {"server": self.server}
        if self.username is not None:
            proxy["username"] = self.username
        if self.password is not None:
            proxy["password"] = self.password
        if self.bypass is not None:
            proxy["bypass"] = self.bypass
        return proxy

    def httpx_proxy_url(self) -> Optional[str]:
        """Build the httpx ``proxy=`` URL (embedding auth), or ``None``.

        httpx 0.28 takes a single ``proxy=`` string/URL on
        ``AsyncClient``; credentials are carried in the URL userinfo
        (``scheme://user:pass@host:port``). Returns ``None`` when no proxy
        is configured so the fetcher omits the kwarg. ``bypass`` is not
        encodable in a single httpx proxy URL -- the fetcher only uses the
        side-path proxy for outbound document/binary requests, and the
        bypass list is honoured by the browser path (Playwright) where it
        matters most.
        """
        if not self.server:
            return None
        if self.username is None and self.password is None:
            return self.server
        parsed = urlparse(self.server)
        user = quote(self.username or "", safe="")
        pwd = quote(self.password or "", safe="")
        netloc_host = parsed.netloc.rsplit("@", 1)[-1]
        return f"{parsed.scheme}://{user}:{pwd}@{netloc_host}{parsed.path}"

    # v1.7.0 (Wave 2F): scope env-var lookup to WEB_AGENT_PROXY__ so a bare
    # ``SERVER`` / ``USERNAME`` env var can't override proxy settings and so
    # a default ``AppConfig()`` (which builds this via ``default_factory``)
    # does not read unprefixed vars. Nested ``WEB_AGENT_PROXY__<FIELD>`` via
    # AppConfig still works. Mirrors every other sub-config.
    model_config = {"env_prefix": "WEB_AGENT_PROXY__"}


class AppConfig(BaseSettings):
    """Top-level configuration for the web_agent toolkit.

    All sub-configs use sensible defaults, so ``AppConfig()`` works out of
    the box with no file or environment variables required.

    Args:
        browser: Chromium browser settings.
        search: Web search parameters.
        fetch: Page fetching and retry settings.
        download: File download settings.
        extraction: Content extraction settings.
        automation: Browser automation action settings.
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR).
        output_dir: Directory for saving pipeline JSON results.
        base_dir: Base directory for resolving relative paths. Defaults to CWD.

    Example::

        from web_agent import Agent, AppConfig

        # All defaults - no config file needed:
        async with Agent() as agent:
            result = await agent.fetch_and_extract("https://example.com")

        # Custom config:
        config = AppConfig(
            browser={"headless": False},
            log_level="DEBUG",
            output_dir="/tmp/results",
        )
        async with Agent(config) as agent:
            ...
    """

    # env_nested_delimiter is required for pydantic-settings v2 to parse
    # double-underscore nested env vars like WEB_AGENT_BROWSER__HEADLESS.
    # Without it, only top-level fields like WEB_AGENT_LOG_LEVEL apply --
    # the README + module docstring would silently lie about sub-config
    # support.
    model_config = {
        "env_prefix": "WEB_AGENT_",
        "env_nested_delimiter": "__",
    }

    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    # v1.6.7: domain skills + agent-editable workspace
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    # v1.6.8: network capture, download intents, post-action screenshots,
    # session replay traces
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    # v1.7.0 (Wave 2F): outbound proxy shared by the Playwright launch and
    # the httpx side-paths. Inactive by default (server unset -> no proxy
    # anywhere).
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    # v1.6.16 deep-review fix: a Literal of loguru's level names so an unknown
    # value is rejected at config time instead of failing when logging is wired
    # (loguru's level lookup is case-sensitive / uppercase).
    log_level: Literal[
        "TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"
    ] = "INFO"
    output_dir: str = "./output"
    base_dir: str = Field(default=".", description="Base directory for resolving relative paths")
    ranking_profiles: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "User-defined ranking profiles, merged with the built-in "
            "RANKING_PROFILES (official_sources / docs / research / "
            "news / files). Same shape as the built-in dict: "
            "{name: [host1, host2, ...]}. User-defined profiles "
            "OVERRIDE built-ins on name collision -- callers can "
            "redefine 'docs' for an internal docs portal, for example."
        ),
    )

    @model_validator(mode="after")
    def _resolve_paths(self) -> AppConfig:
        """Resolve relative paths against base_dir to produce absolute paths."""
        # v1.6.16 (review CO-9): use the cross-platform absolute-path
        # predicate the rest of the toolkit uses (``safe_join_path`` etc.)
        # instead of ``Path.is_absolute()``, which is host-OS dependent --
        # on a Linux host ``PurePosixPath('C:/x').is_absolute()`` is False,
        # so a Windows-rooted config path would be wrongly joined under
        # base_dir. ``_is_cross_platform_absolute`` recognises POSIX,
        # Windows-drive, and UNC absolutes regardless of the running OS.
        from .utils import _is_cross_platform_absolute

        base = Path(self.base_dir).resolve()

        def _resolve(p: str) -> str:
            if not _is_cross_platform_absolute(p):
                return str(base / Path(p))
            return p

        self.output_dir = _resolve(self.output_dir)
        self.download.download_dir = _resolve(self.download.download_dir)
        self.automation.screenshot_dir = _resolve(self.automation.screenshot_dir)
        self.debug.debug_dir = _resolve(self.debug.debug_dir)
        self.audit.audit_log_path = _resolve(self.audit.audit_log_path)
        self.cache.cache_dir = _resolve(self.cache.cache_dir)
        # v1.6.8: session replay traces live under base_dir by default
        self.diagnostics.trace_dir = _resolve(self.diagnostics.trace_dir)
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        """Load configuration from a YAML file.

        Args:
            path: Absolute or relative path to the YAML config file.

        Returns:
            AppConfig populated from the YAML data with defaults for missing keys.

        Raises:
            FileNotFoundError: If the YAML file does not exist.
            ConfigError: If the YAML cannot be parsed or its values fail
                Pydantic validation (wraps yaml.YAMLError + pydantic.ValidationError).
        """
        from .exceptions import ConfigError

        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        try:
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse YAML config {p}: {exc}") from exc
        # v1.6.16 deep-review fix: yaml.safe_load returns a list/scalar for a
        # typo'd config (e.g. a top-level ``- browser:`` sequence). The base_dir
        # injection + cls(**data) below then raised a raw TypeError, breaking the
        # documented ConfigError contract. Reject a non-mapping root explicitly.
        if not isinstance(data, dict):
            raise ConfigError(
                f"Config file {p} must contain a YAML mapping at the top level, "
                f"got {type(data).__name__}."
            )
        if "base_dir" not in data:
            data["base_dir"] = str(p.parent.resolve())
        try:
            return cls(**data)
        except Exception as exc:
            raise ConfigError(f"Config validation failed for {p}: {exc}") from exc
