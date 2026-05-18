# Security Policy

`web-agent-toolkit` runs a real Chromium browser under autonomous-agent
control, performs network I/O against arbitrary URLs, follows redirects,
saves files to disk, and (optionally) evaluates JavaScript supplied by
LLMs. The combination is a meaningful attack surface, so we publish a
threat model and mitigations explicitly.

## Reporting a Vulnerability

Please **do not** open public GitHub issues for suspected security
vulnerabilities.

Email **<solutionsteams@decimalpointanalytics.com>** with:

- A minimal reproduction (URL, config, version)
- Expected vs. observed behavior
- Severity assessment (your view)
- Whether you wish to be credited in the fix announcement

You can expect:

- Acknowledgement within 3 business days
- Initial triage within 7 days
- Patched release on the `main` branch with a CHANGELOG entry citing
  the report (without your contact info unless you opt in)

For the duration of the embargo, please do not share the
reproduction publicly.

## Supported Versions

Active maintenance is on `main`. Latest released version: see
[CHANGELOG.md](CHANGELOG.md). Security fixes target the latest 1.6.x
release; older 1.x versions are best-effort only.

## Threat Model

`web-agent-toolkit` is designed to be the network-touching layer under
an autonomous LLM agent. The threat model assumes:

- The toolkit is invoked by an agent or human operator who may pass
  attacker-controlled URLs, search queries, or filenames.
- The browser fetches and renders attacker-controlled HTML/JS.
- Downloaded files may be malicious documents.
- The host running the toolkit may have access to internal networks
  (cloud metadata services, RFC1918 subnets, intranet hosts).

### In scope

The following classes of attack are explicit goals to mitigate:

- **SSRF / private-network egress.** An attacker-supplied URL must not
  cause the toolkit to read AWS / GCP / Azure metadata, RFC1918
  subnets, link-local addresses, or loopback (`127.0.0.0/8`).
- **Redirect-based bypass.** A whitelisted host must not be able to
  301/302 us to a denied host or private IP. The fetcher, the
  downloader, the binary fetch path, and the HEAD probe all
  re-validate the post-redirect URL against the policy.
- **Path traversal in filenames.** Caller-supplied filenames passed
  to `Agent.download()` and screenshot paths must not escape the
  configured download / screenshot directory. Cross-platform: POSIX
  absolute, Windows drive-rooted (`C:\...`), and UNC (`\\server\...`)
  paths are all rejected even when the check runs on the "wrong" OS.
  This same `safe_join_path()` defence applies to v1.6.7 workspace
  writes and v1.6.8 verification-screenshot paths.
- **Disk-fill via large download.** All download paths enforce
  `DownloadConfig.max_file_size_mb`. The httpx streaming path aborts
  mid-stream; the Playwright paths pre-check `Content-Length` where
  available and unlink any oversize result.
- **Disk-fill via download-intent tmpfile pileup (v1.6.8).** When
  `diagnostics.capture_download_intents=True`, the
  `page.on('download')` notification listener calls `download.delete()`
  in a tracked task (`NetworkCollector._pending_deletes`) so each
  intent doesn't leak a Chromium tmpfile across long-running sessions.
- **Robots.txt non-compliance.** When `SafetyConfig.respect_robots_txt`
  is on (default: True), every fetch and download checks
  `robots.txt` against the configured user-agent before any network
  I/O.
- **Per-host abuse.** Per-host token-bucket rate limiting
  (`SafetyConfig.rate_limit_per_host_rps`) bounds outbound RPS per
  origin.
- **Cross-session cookie leakage (closed in v1.6.5).** Persistent
  browser sessions use a domain-aware `httpx.Cookies` jar in
  `WebFetcher._cookies_for_session`; cookies for `bank.com` no longer
  spill to `attacker.com` when both share a `session_id`. Playwright
  download paths also re-validate `page.url` / `download.url`
  post-redirect before any `save_as`.
- **Action drift in browser automation.** Per-action URL drift
  detection in `BrowserActions.execute_sequence` aborts the sequence
  if a click or JS-driven nav lands on a denied domain or private IP.
- **JS evaluation.** `EvaluateInput` actions are gated by
  `SafetyConfig.allow_js_evaluation` (default: False). LLMs cannot
  exfiltrate session cookies via injected JS without the operator
  explicitly opting in.
- **Form submission.** `BrowserActions` heuristically detects
  submit-typed elements and gates them on
  `SafetyConfig.allow_form_submit` (default: True; flip to False for
  read-only browsing).
- **Arbitrary-file upload via prompt injection (closed in v1.6.7).**
  `Agent.upload_file()` and `UploadFileInput` only accept paths under
  `DownloadConfig.download_dir` unless the operator explicitly sets
  `SafetyConfig.allow_upload_outside_download_dir=True`. Without that
  fence, a prompt injection could trick the agent into uploading
  `~/.ssh/id_rsa`.

### Browser-control attack surface (v1.6.6 / v1.6.8 / v1.6.9)

The CDP attach + remote-CDP features introduce a second class of
attack surface around browser process control:

- **Never attaches to user's existing Chrome.**
  `BrowserConfig.attach_existing_browser=True` is **explicitly rejected
  at config validation time.** webTool only controls browsers it
  launched itself. The rationale: attaching to a user's personal
  Chrome would expose real cookies, history, and logged-in accounts.
- **CDP bind is loopback-only.** `BrowserConfig.cdp_host` must be a
  loopback address (`127.0.0.1` / `localhost`); non-loopback values
  are rejected at validation. The `--remote-debugging-port` is bound
  to `127.0.0.1` so external network observers can't attach.
- **CDP requires isolation.** `cdp_enabled=True` requires
  `isolation_mode=True`. The remote-debugging-port discovery reads
  `DevToolsActivePort` from the webTool-owned user-data-dir; without
  isolation we'd be reading the wrong process's file.
- **`remote_cdp` validator uses `ipaddress.is_loopback` (v1.6.8).**
  `backend="remote_cdp"` + `remote_cdp_url` accepts the entire
  `127.0.0.0/8` range plus `::1` plus `localhost`, and rejects every
  other host (public, private RFC1918, link-local). The check uses
  `ipaddress.ip_address(host).is_loopback` rather than an exact-string
  allowlist so `ws://127.0.0.2:9222/...` is correctly classified as
  loopback and `ws://10.0.0.1:9222/...` is rejected.
- **Remote-CDP is incompatible with isolation + cdp_enabled.** The
  validator rejects these combinations: `remote_cdp` connects to a
  pre-existing browser whose profile we don't own.
- **`remote_cdp` ownership tokens (v1.6.9).** Loopback alone does not
  prove ownership -- a user's personal Chrome can run on
  `127.0.0.1:9222` too. v1.6.9 adds filesystem-anchored ownership
  proofs: every isolated launch writes a 64-char hex token
  (`secrets.token_hex(32)`) to `<profile_dir>/.webtool-ownership`
  (chmod 0o600 best-effort). `remote_cdp` callers must present a
  matching token via `BrowserConfig.remote_cdp_ownership_token` and
  `remote_cdp_profile_dir`; verification uses
  `secrets.compare_digest`. The validator + `BrowserManager.start()`
  check both happen BEFORE any CDP connection is opened. This makes
  it impossible to "stumble into" a foreign loopback browser by
  guessing a port.
- **Coordinate-click form-submit safety (v1.6.9 + v1.6.10).** Prior to
  v1.6.9, `Agent.click_xy(x, y)` logged a warning under `safe_mode` and
  clicked anyway -- there was no element to inspect. v1.6.9 adds
  `SafetyConfig.allow_coordinate_clicks` (default True, **forced
  False** in `safe_mode`) plus a `document.elementFromPoint(x, y)`
  inspector that walks up to 5 ancestors and blocks clicks on
  submit / login / register / delete / pay / accept / consent
  controls when `allow_form_submit=False`. This closes the
  prompt-injection vector where an attacker tricks the agent into
  using coord clicks to bypass the selector-path heuristic. v1.6.10
  adds `SafetyConfig.coordinate_click_unknown_policy` (`"allow"` |
  `"block"`, default `"allow"`, **forced `"block"`** in `safe_mode`):
  when `"block"`, an empty / failed `elementFromPoint` inspection
  rejects the click instead of allowing it. Strict callers running
  with `allow_coordinate_clicks=True` AND `allow_form_submit=False`
  can opt into "unknown == hostile" semantics without the broader
  `safe_mode` clamp.

### Workspace + diagnostic data (v1.6.7 / v1.6.8)

- **Workspace mode gates.** The agent-editable workspace defaults to
  `mode="markdown_skills_only"` — only `.md` files under
  `domain-skills/`. The two python-enabled modes
  (`reviewed_python_helpers` / `unsafe_python_helpers`) are second
  opt-ins; even when `mode=reviewed_python_helpers`,
  `workspace.execute_helpers` defaults to `False` so the file may
  exist but is not imported.
- **Helpers-file mode is strict.** `reviewed_python_helpers` only
  matches the literal root-level `helpers.py`; `subdir/helpers.py` is
  rejected (closed in v1.6.7 review pass).
- **Network header capture defaults off.** `NetworkEvent.request_headers`
  and `response_headers` are populated only when
  `diagnostics.include_request_headers` / `include_response_headers`
  are explicitly True. Default off because `Authorization` and
  `Cookie` headers are commonly sensitive.
- **Trace-file path traversal.** `SessionTraceRecorder` validates
  every `session_id` against `^[A-Za-z0-9._-]+$` before using it as
  a filename. Sessions are minted internally, but defence-in-depth.
- **Skill query operator sanitization.** Bundled search-driven
  skills (e.g. `github_release_download`) strip search-engine
  operator metacharacters (`"`, `'`, `(`, `)`, `[`, `]`, `|`) from
  user-supplied query terms before composing `site:github.com ...`
  queries, so a prompt injection like `"x OR site:evil.com"` can't
  break out of the intended scope.

### Known limitations / out of scope

These are documented limitations. PRs welcome.

- **DNS rebinding.** `check_domain_allowed()` resolves DNS once at
  policy-check time; Playwright resolves DNS again at navigation.
  A targeted attacker who controls a DNS server can flip the A
  record between the two resolves to bypass the private-IP block.
  Mitigations on the roadmap:
  - Pin DNS at pre-check via `socket.gethostbyname()` and pass an
    IP-based URL to Playwright (medium effort, may break SNI).
  - Use Playwright request interception to re-validate every
    request (high effort).
- **Sandboxing.** The toolkit runs Playwright in default config.
  For untrusted pages we recommend running the host process in a
  container with no access to the cloud metadata service and a
  network policy that blocks egress to RFC1918 / 169.254.0.0/16.
- **Supply chain.** We pin direct dependencies in `pyproject.toml`
  with conservative version ranges but do not ship a lockfile.
  Consumers that need reproducible builds should pin transitively.
- **Side-channel data leakage.** The audit log, debug captures, and
  cache directory all write to disk in plaintext. Treat the
  `output_dir`, `audit_log_path`, `cache_dir`, and `debug_dir` as
  containing potentially sensitive data and protect them
  accordingly.
- **Crypto-grade tamper resistance.** The audit log is JSONL
  append-only by convention but not cryptographically signed.
- **Captcha / bot-management bypass is NOT a goal.** If a site
  serves a CAPTCHA, the search engine returns no results and the
  toolkit reports `SearchError` (in strict mode) or empty results.
  We do not attempt to defeat CAPTCHAs.

## Defense-in-Depth Layers

The current safety layers, in order of when they apply:

1. **`SafetyConfig.allowed_domains` / `denied_domains`** — domain
   allow/deny list, checked before any network I/O. URL-shaped
   patterns auto-normalised (`"https://Evil.com/"` → `"evil.com"`).
2. **`SafetyConfig.block_private_ips`** — RFC1918 / loopback /
   link-local / reserved address blocking via `is_private_address()`
   (DNS cached via a 2048-entry LRU since v1.6.5).
3. **Per-host cookie isolation** (v1.6.5) — `WebFetcher._cookies_for_session`
   returns a domain-aware `httpx.Cookies` jar so cross-domain leakage
   inside a shared `session_id` is impossible.
4. **Path traversal protection** — `safe_join_path()` rejects any
   absolute path on any major platform plus any traversal-by-`..`
   candidate before file write. Applied to download paths, screenshot
   paths, workspace writes (v1.6.7), and verification screenshots (v1.6.8).
5. **`robots.txt`** — `RobotsChecker` honors the site's robots.txt
   for the configured user-agent.
6. **Per-host rate limit** — token-bucket gate per origin.
7. **Pre-fetch URL gate** — fetch / download / classify_url all
   call `check_domain_allowed()` before launching any I/O.
8. **Post-redirect re-validation** — every layer re-validates the
   final URL after redirects (fetcher, downloader, binary fetch,
   HEAD probe, Playwright `_do_save_page` / `_download_with_playwright`).
9. **Per-action drift detection** — browser automation aborts on
   the first action whose post-condition lands on a disallowed URL.
10. **Granular allow flags** — `allow_js_evaluation`,
    `allow_downloads`, `allow_form_submit`,
    `allow_upload_outside_download_dir` gate dangerous actions.
11. **Master kill-switch** — `safe_mode=True` forces all
    `allow_*` flags to False.
12. **Per-call budgets** — `max_pages_per_call`, `max_chars_per_call`,
    `max_time_per_call_seconds` bound resource use per request.
13. **Download size cap** — streaming paths enforce
    `max_file_size_mb` mid-stream; Playwright paths enforce it via
    `Content-Length` pre-check + post-save stat + unlink.
14. **Browser isolation profile** (v1.6.6) — `isolation_mode=True`
    launches Chromium against a webTool-owned `--user-data-dir` so
    cookies / localStorage / cache / downloads never touch the user's
    real Chrome profile.
15. **CDP loopback enforcement** (v1.6.6 + v1.6.8) — `cdp_host` and
    `remote_cdp_url` are validated as loopback-only; the
    `attach_existing_browser` knob is rejected unconditionally.
16. **Workspace mode gates** (v1.6.7) — default
    `markdown_skills_only` blocks Python writes; even
    `reviewed_python_helpers` requires a second opt-in
    (`execute_helpers=True`) before `helpers.py` is imported.
17. **Diagnostic-data minimisation** (v1.6.8) — network header
    capture and trace recording all default off; when enabled,
    `session_id` is regex-validated before being used as a filename.
18. **`remote_cdp` ownership token** (v1.6.9) — every isolated launch
    writes a 64-char random token to
    `<profile_dir>/.webtool-ownership`; `remote_cdp` attaches require
    a matching token (constant-time compare). Closes the
    "stumble into a foreign loopback Chrome" attack.
19. **Coordinate-click form-submit safety** (v1.6.9) —
    `safety.allow_coordinate_clicks` (forced False in `safe_mode`)
    plus a `document.elementFromPoint` inspector blocks submit /
    login / pay controls under `allow_form_submit=False`. Closes
    the click_xy bypass of the selector-path heuristic.
20. **Chromium sandbox kept by default** (v1.6.9) — `--no-sandbox` is
    no longer passed unconditionally. Local dev keeps the sandbox;
    CI / container auto-detected via `CI`, `GITHUB_ACTIONS`, or
    `/.dockerenv`. Per-tab renderer isolation against arbitrary-code
    exploits stays on for the common case.

## Hardening Recommendations for Production

If you're embedding `web-agent-toolkit` in a production agent:

1. Run in a container with `NetworkPolicy` denying egress to
   `169.254.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`,
   `192.168.0.0/16`, and `127.0.0.0/8`.
2. Set `safety.allowed_domains` to an explicit allowlist for your
   use case; do not rely solely on `denied_domains`.
3. Set `safety.safe_mode=True` if your agent never needs JS
   evaluation, downloads, or form submission.
4. Keep `safety.respect_robots_txt=True` (default).
5. Set a conservative `download.max_file_size_mb` (default 100 MB
   is fine for documents; lower for adversarial environments).
6. Mount `output_dir`, `audit_log_path`, `cache_dir`, `debug_dir`,
   `diagnostics.trace_dir`, `automation.screenshot_dir`, and any
   workspace dir on encrypted volumes with restrictive ACLs.
7. Rotate audit logs, debug captures, replay traces, and verification
   screenshots; they may contain sensitive URLs, cookies, headers
   (when `diagnostics.include_*_headers=True`), or page contents.
8. Pin the toolkit to a specific commit SHA, not a branch, until
   we publish to PyPI.
9. **Browser isolation** (v1.6.6): set `browser.isolation_mode=True`
   so cookies / cache / downloads stay in a webTool-owned tempdir.
   Pair with `browser.profile_mode="ephemeral"` +
   `cleanup_on_exit=True` for short-lived agents; use
   `profile_mode="named"` for logged-in workflows that should survive
   restarts.
10. **CDP attach** (v1.6.6 + v1.6.8): only enable when an external
    observer (debugger, browser-use, MCP client) genuinely needs CDP
    access. Keep `cdp_host="127.0.0.1"`; rely on the validator to
    reject anything else. When using `backend="remote_cdp"`, the
    remote browser process must already be locked down — webTool
    only inherits the security posture of the endpoint it connects to.
11. **Workspace** (v1.6.7): leave `workspace.enabled=False` unless the
    agent actually needs to read / write skill files. When enabled,
    `mode="markdown_skills_only"` (the default) is the safe choice;
    only flip to `reviewed_python_helpers` when a trusted human
    reviews the helper file, and even then keep `execute_helpers=False`
    until the helper is reviewed every change.
12. **Diagnostics** (v1.6.8): leave the whole `DiagnosticsConfig`
    off in production unless you're actively debugging. When
    `capture_network=True`, keep `include_request_headers=False` and
    `include_response_headers=False` (defaults) so cookies and bearer
    tokens don't land in `FetchResult.network_events`. Treat
    `diagnostics.trace_dir` as you would any audit log.
