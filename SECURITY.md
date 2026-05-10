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
- **Disk-fill via large download.** All download paths enforce
  `DownloadConfig.max_file_size_mb`. The httpx streaming path aborts
  mid-stream; the Playwright paths pre-check `Content-Length` where
  available and unlink any oversize result.
- **Robots.txt non-compliance.** When `SafetyConfig.respect_robots_txt`
  is on (default: True), every fetch and download checks
  `robots.txt` against the configured user-agent before any network
  I/O.
- **Per-host abuse.** Per-host token-bucket rate limiting
  (`SafetyConfig.rate_limit_per_host_rps`) bounds outbound RPS per
  origin.
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
   allow/deny list, checked before any network I/O.
2. **`SafetyConfig.block_private_ips`** — RFC1918 / loopback /
   link-local / reserved address blocking via `is_private_address()`.
3. **Path traversal protection** — `safe_join_path()` rejects any
   absolute path on any major platform plus any traversal-by-
   `..` candidate before file write.
4. **`robots.txt`** — `RobotsChecker` honors the site's robots.txt
   for the configured user-agent.
5. **Per-host rate limit** — token-bucket gate per origin.
6. **Pre-fetch URL gate** — fetch / download / classify_url all
   call `check_domain_allowed()` before launching any I/O.
7. **Post-redirect re-validation** — every layer re-validates the
   final URL after redirects (fetcher, downloader, binary fetch,
   HEAD probe).
8. **Per-action drift detection** — browser automation aborts on
   the first action whose post-condition lands on a disallowed URL.
9. **Granular allow flags** — `allow_js_evaluation`,
   `allow_downloads`, `allow_form_submit` gate dangerous actions.
10. **Master kill-switch** — `safe_mode=True` forces all
    `allow_*` flags to False.
11. **Per-call budgets** — `max_pages_per_call`, `max_chars_per_call`,
    `max_time_per_call_seconds` bound resource use per request.
12. **Download size cap** — streaming paths enforce
    `max_file_size_mb` mid-stream; Playwright paths enforce it via
    `Content-Length` pre-check + post-save stat + unlink.

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
6. Mount `output_dir`, `audit_log_path`, `cache_dir`, `debug_dir`
   on encrypted volumes with restrictive ACLs.
7. Rotate audit logs and debug captures; they may contain
   sensitive URLs or cookies.
8. Pin the toolkit to a specific commit SHA, not a branch, until
   we publish to PyPI.
