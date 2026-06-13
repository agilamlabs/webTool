# webTool

[![CI](https://github.com/agilamlabs/webTool/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/agilamlabs/webTool/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

Professional agentic web search, fetch, download, extraction, and browser automation toolkit built on Playwright's headless Chromium.

Designed as a tool for AI agents that need to search the web, fetch JavaScript-heavy pages, extract structured content, download files, and automate browser interactions -- all through a clean async Python API.

Slots in as a **local, no-API web backend** under autonomous agents like [OpenClaw](https://github.com/openclaw/openclaw), [LangGraph](https://github.com/langchain-ai/langgraph), and any MCP-compatible client (Claude Desktop, Claude Code, Cursor, OpenAI Codex). See [Using web_agent as a Backend for Local Agents](#using-web_agent-as-a-backend-for-local-agents).

> **What's new in 1.7.0** — *Real-world hardening — make the toolkit solve
> the problems autonomous agents actually hit on the live 2026 web.* Driven by
> a market-research + full-codebase gap analysis, shipped in waves (a Wave 3
> continuation folded in without a version bump), all **additive** (no breaking
> changes to documented Python APIs):
>
> * **Honest bot-wall detection** (new `web_agent/challenge.py`) — structural
>   (not prose-keyword) markers for Cloudflare / DataDome / Akamai / PerimeterX /
>   reCAPTCHA / hCaptcha. A challenge served on HTTP 200 now returns the new
>   `FetchStatus.BLOCKED` with an *actionable* `error_message` instead of
>   SUCCESS-with-garbage; a managed-JS challenge a real browser auto-passes is
>   settled-and-rechecked first. `ChallengeInfo` on `FetchResult.challenge`.
> * **Token-blowout + failure transparency** (the #1 agentic-tool complaint) —
>   a failed fetch now says *why* (`fetch_status` / `status_code` /
>   `error_message` / `failure_stage` on `ExtractionResult`), and MCP responses
>   return **one** representation (markdown default) capped at
>   `extraction.default_max_chars` (40000) with `offset`/`next_offset` paging —
>   no more content+markdown duplication or ~1 MB dumps.
> * **Production lifecycle hardening** — fixed a real stealth-2.x launch break;
>   added Chromium crash-recovery (auto-relaunch), an idle-session reaper + hard
>   cap, orphaned-profile sweep on start, and a `doctor --quick` that finally
>   catches a missing Chromium.
> * **Auth persistence + login handoff** — `export_session_state` /
>   `import_session_state` round-trip a logged-in `storage_state` to JSON:
>   *log in once (human does 2FA/CAPTCHA), automate afterwards.* Plus a brand-new
>   **MCP session surface** (`web_create_session`, `web_list_sessions`,
>   `web_close_session`, `web_export_session`, `web_import_session`).
> * **Search resilience + a cheap links-only primitive** — a per-provider
>   circuit breaker, an "all providers blocked vs zero hits" distinction
>   (`SearchResponse.search_blocked`), and `Agent.search(...)` /
>   `web_search_links` that return links **without** N browser fetches.
> * **Proxy support + fingerprint coherence** — new `ProxyConfig`
>   (`WEB_AGENT_PROXY__*`, http/https/socks5, off by default) threaded into every
>   launch + httpx side-path; `coherent_fingerprint` pins the rotated UA's OS
>   family to the configured locale. Honest scope: operator controls for
>   compliant access, **not** a stealth-bypass promise.
> * **Prompt-injection containment for fetched content** (new
>   `web_agent/injection.py`) — hidden-from-humans text is **stripped
>   deterministically** (invisible / bidi-override Unicode + `display:none` /
>   `aria-hidden` / off-screen DOM, before extraction) and visible injection is
>   **flagged, not blocked** via an advisory `ExtractionResult.injection`
>   (`InjectionReport`). New `SafetyConfig.sanitize_fetched_content` /
>   `detect_prompt_injection` / `injection_action`. Defense-in-depth against the
>   "lethal trifecta" — explicitly **not** a claim to solve injection.
> * **Infinite-scroll + pagination collection** —
>   `Agent.scroll_to_bottom` loads lazy / infinite-scroll content so the next
>   observe / fetch sees the full DOM; `Agent.collect_across_pages(url,
>   strategy='next_link' | 'page_param' | 'scroll')` walks a multi-page listing
>   and assembles content across pages, every page re-gated through `fetch`
>   (robots / rate-limit / bot-wall / SSRF / injection-sanitize), bounded by
>   `max_pages` + budget with a `stopped_reason`. New `CollectedPage` /
>   `CollectionResult`; MCP `web_scroll_to_bottom` / `web_collect_pages`.
> * **Richer PDF / table extraction** — the PDF path now prefers `pdfplumber`
>   (per-page text + tables) → `pypdf` → install-hint. Tables render as GFM
>   markdown, interleaved in `content` and exposed on `ExtractionResult.tables`;
>   page markers + `page_count`; scanned / image-only PDFs return an actionable
>   "OCR required" error instead of a bare empty success. `pdfplumber` added to the
>   `[binary]` extra.
> * **Production observability + metrics** (new `web_agent/metrics.py`) — an
>   in-process `MetricsRegistry` (counters + cheap count/sum/min/max
>   distributions, a per-metric label-cardinality cap so a hostile label can't
>   grow the registry unbounded, `disabled` = near-zero-cost no-op) instrumented
>   at outcome points across fetch / search / browser-lifecycle hot paths. New
>   `Agent.metrics() -> MetricsSnapshot`, MCP `web_metrics`, `MetricsConfig`
>   (`WEB_AGENT_METRICS__`). Plus log hygiene: the MCP server now honours
>   `config.log_level` and both MCP + CLI log formats surface the correlation id
>   (`{extra[cid]}`).
> * **Non-root Docker image + MCP container** — `docker/Dockerfile` on the
>   official Playwright Python base (Chromium + deps + fonts baked in), running as
>   the non-root `pwuser`, with a `HEALTHCHECK` (`web-agent doctor --quick`) and
>   `ENTRYPOINT web-agent-mcp`; plus `docker/docker-compose.yml`,
>   `docker/README.md`, and a `.dockerignore`. Two new `doctor` checks
>   (`not_running_as_root`, `container_sandbox`). The image builds + runs verified.
> * **Set-of-marks observe → act targeting** — `observe()` now returns a bounded,
>   numbered list of **interactive** elements on `ObserveResult.elements` (each
>   `InteractiveElement` carries a `ref`, role, accessible name, tag, enabled /
>   visible, `bbox`, and a selector), and an action can target one by `ref` via a
>   new `LocatorSpec(ref=...)` mode — so the model picks "element #N from what I
>   just observed" instead of guessing a CSS selector. Reuses every existing
>   locator + safety gate; the ref pattern is injection-proof. New
>   `AutomationConfig.observe_max_elements` / `observe_tag_refs`. The set-of-marks
>   loop now works **end-to-end over MCP**: `web_interact` / `Agent.interact`
>   accept an **omitted `url`** and act on the session's current tab *in place*
>   (no navigation), so the `data-webtool-ref` stamps from `web_observe` survive.
>   Reliable loop: `web_create_session` → `web_observe(session_id)` (reads refs) →
>   `web_interact(session_id, actions=[{"action":"click","selector":{"ref":"e3"}}])`
>   with `url` omitted. (Previously `web_interact` always navigated and wiped the
>   refs.)
> * **Isolation + CDP on by default** — `BrowserConfig.isolation_mode` and
>   `cdp_enabled` now default **`True`**: every `Agent` launches an isolated,
>   ephemeral-profile browser with a loopback CDP debug port out of the box (both
>   still toggleable off; the validator reconciles `isolation_mode=False` /
>   `backend="remote_cdp"` automatically). Security note: the loopback CDP port is
>   **unauthenticated** — set `browser.cdp_enabled=False` on shared / multi-tenant
>   hosts or with sensitive authenticated sessions (see [SECURITY.md](SECURITY.md)).
> * **Gap-analysis fixes** — `web_interact` acts in place over MCP (above);
>   removed 3 duplicate session tools (`create_browser_session` /
>   `close_browser_session` / `list_browser_sessions`) that shadowed the canonical
>   `web_*` set (server now **44** tools); the downloader threads the configured
>   proxy on its httpx path (+ `download_total` / `download_outcome` /
>   `download_bytes` metrics); `collect_across_pages` surfaces per-page
>   `CollectedPage.injection` + a `max_injection_risk` / `pages_with_injection`
>   rollup and skips a `block`-flagged page mid-walk; injection precision tightened
>   (HTML `<s>` no longer escalates to HIGH) with a new exported `redact_injection`
>   that makes `injection_action=redact` actually mask the override; and the idle
>   reaper no longer races a long scroll.
>
> Adds new MCP tools and removes 3 duplicate session tools — the server now
> exposes **44** — and **17 new public exports** (`ChallengeInfo`,
> `StorageStateResult`, `ProxyConfig`, `SearchEngine`, `SearchOutcome`,
> `InjectionReport`, `CollectedPage`, `CollectionResult`, the injection helpers
> incl. the new `redact_injection`, plus `MetricsRegistry`, `MetricsSnapshot`,
> `MetricsConfig`, `InteractiveElement` → **130** total), and grows the suite to
> **1548 passing** (28 `integration` deselected, opt-in). Adversarial close-out
> reviews (a 4-dimension review of the ~7,400-line diff, a Wave 3 injection +
> collection/PDF pass, and a Wave 4 observability / Docker / a11y-targeting pass)
> found **no critical/high issues**. **Behaviour changes:** bot-challenge pages
> return `BLOCKED` (was SUCCESS); MCP content is single-representation + capped; a
> bare `pytest` run now excludes integration tests (`-m integration` to opt in).
> See [CHANGELOG.md](CHANGELOG.md#170---2026-06-13).
>
> **What's new in 1.6.16** — *Review-hardening — 32 findings from a
> full-codebase brutal review, adversarially verified.* Closes SSRF holes in
> the secondary egress paths (`fetch_binary`, `classify_url`, the downloader
> fall-through, `fill_form_and_extract`); a config fail-open where a **bare**
> unprefixed env var (e.g. `BLOCK_PRIVATE_IPS=false`) could silently disable a
> security fence — every sub-config now carries a `WEB_AGENT_<SECTION>__`
> prefix; an arbitrary-file-write via `web_print_page_as_pdf`; a no-op query
> sanitizer in the GitHub skill; plus validation bounds, redaction/replay
> fixes, and unbounded-growth guards. See `CHANGELOG.md`. **Behaviour change:**
> bare unprefixed env vars are no longer read — use `WEB_AGENT_<SECTION>__<FIELD>`.
>
> **What's new in 1.6.15** — *Log hygiene.* `SearchEngine` no longer
> logs `Skipping unavailable provider: searxng` on every search when
> SearXNG sits in the default chain without a `search.searxng_base_url`
> — it's now reported once at construction (with a hint) and skipped
> silently thereafter. No behaviour change; SearXNG still auto-enables
> when `search.searxng_base_url` is set.
>
> **What's new in 1.6.14** — *Hardening slice — 8 Critical fixes
> from a brutal full-codebase audit, plus a deeper review-hardening
> follow-up (44 more findings: 1 Critical / 10 High / 21 Medium / 12
> Low — all fixed in-place, no version bump).* No new features; pure
> correctness, security, and DoS hardening. The follow-up adds a
> connect-time DNS-rebinding guard (C-1), obfuscated-IP SSRF
> normalisation, a session-namespaced fetch cache, handler-level
> JS-eval gating, and ~20 more — see CHANGELOG. The original 8-fix
> bundle spans 10 source files + 3 new test files (+22 tests); the
> follow-up adds +26 (`tests/test_v1614_review_hardening.py`).
> Headlines (original bundle):
>
> * **Security**: `WaitInput(target=FUNCTION)` now honours
>   `safety.allow_js_evaluation` (closes a cookie exfiltration
>   vector via prompt injection — pre-v1.6.14 the JS-evaluation
>   gate only covered `EvaluateInput`, not `wait_for_function`);
>   `Agent.replay_trace` + `TraceRecorder.load_entries` now contain
>   `trace_file` paths to `trace_dir` (closes an LFI via the MCP
>   `web_replay_trace` tool); `web_interact` MCP docstring now
>   lists all **19 action types**, not the stale "12" — re-enables
>   7 v1.6.6/v1.6.7 actions that were invisible to MCP callers.
> * **Throughput / DoS**: `RateLimiter.notify_429` caps
>   `Retry-After` at 300 s — a server's `Retry-After: 99999999`
>   used to put the limiter into a ~3-year sleep;
>   `WebFetcher.fetch_many` with `session_id` is now bounded by
>   `asyncio.Semaphore(BrowserConfig.max_pages_per_session_fetch)`
>   (default 5) — pre-v1.6.14 reproducibly crashed Chromium at
>   20+ parallel pages on a session;
>   `NetworkCollector.wait_for_pending_bodies` now correctly
>   cancels orphaned tasks on timeout instead of letting them
>   continue running against possibly-closed Pages.
> * **Pipeline**: `Recipes.fill_form_and_extract` returns
>   `extraction_method="none"` (instead of misleading SUCCESS
>   with empty html) when the navigation race kills capture;
>   `TabManager.close_tab` now holds `_lock` across `page.close()`
>   to prevent concurrent `switch_tab` from observing inconsistent
>   intermediate state.
>
> **No breaking changes** to documented v1.6.13 public APIs. New
> `BrowserConfig.max_pages_per_session_fetch=5` default is the
> only behavioural cap; raise it if you have a beefy renderer.
> Implementation was delegated to 3 specialised agents working in
> parallel on disjoint file sets — total wall-clock ~12 minutes.
>
> See [CHANGELOG.md](CHANGELOG.md#1614---2026-05-28) for the full
> entry including the threat model for each fix.
>
> **v1.6.13** — *Page-content capture resilience.*
> Single-slice patch addressing one specific production failure mode:
> `page.content()` raising `"Unable to retrieve content because the
> page is navigating and changing the content"` mid-fetch. The race
> is transient (page is fine, snapshot moment was wrong) but
> pre-v1.6.13 it triggered a full re-navigation via the
> `async_retry` decorator (2-5s wasted) and on aggressively
> redirecting pages (Cloudflare interstitials, hydrating SPAs,
> meta-refresh) could exhaust retries entirely. Headlines:
>
> * **`safe_page_content(page, *, retries=3, settle_ms=250,
>   use_cdp_fallback=True)`** is a new public helper in `utils.py`
>   that walks three tiers: (1) `page.content()` with bounded retry
>   on the specific race; (2) `page.evaluate('...outerHTML')`
>   (runs page-side, tolerates some races the remote protocol
>   rejects); (3) CDP `DOM.getOuterHTML` (reads the browser's
>   internal DOM tree, bypasses JS-side checks). Returns
>   `(html, source)` where source is `"content" | "evaluate" | "cdp"
>   | "navigating"`. Never raises on the race path; non-race errors
>   re-raise so the outer `async_retry` decorator owns generic
>   failure handling.
> * **`FetchResult.html_capture_source`** surfaces which tier won.
>   `None` for binary fetches; the four literals for HTML.
>   `"navigating"` means all tiers failed and `html=""` -- treat as
>   degraded.
> * **All 4 `page.content()` call sites refactored**:
>   [`web_fetcher.py`](web_agent/web_fetcher.py) (main fetch, sets
>   `html_capture_source`), [`downloader.py`](web_agent/downloader.py)
>   (save-page, returns `HTTP_ERROR` when all tiers abandon -- no
>   zero-byte files), [`recipes.py`](web_agent/recipes.py)
>   (`fill_form_and_extract`, where form-submit redirects make the
>   race especially common), [`debug.py`](web_agent/debug.py)
>   (failure-time snapshots, which fire mid-redirect by design).
> * **No new dependencies. No breaking changes.** All v1.6.12
>   public APIs unchanged. `html_capture_source` defaults to `None`
>   so existing `FetchResult(...)` callers see no schema break.
>
> See [CHANGELOG.md](CHANGELOG.md#1613---2026-05-28) for the
> full entry including the alternatives-considered notes.
>
> **v1.6.12** — *Throttle + telemetry + structured-data patch.*
> HTTP 429 handling, granular telemetry, and structured-data
> extraction (JSON-LD always-on plus opt-in XHR/fetch JSON body
> capture and a `prefer_api=True` extractor mode). Headlines:
>
> * **HTTP 429 is no longer silently "successful".** Pre-v1.6.12,
>   `WebFetcher` returned a `FetchResult(status=SUCCESS, status_code=429)`
>   on a "Too Many Requests" response -- a false positive that
>   polluted downstream extraction. v1.6.12 parses the `Retry-After`
>   header, signals the per-host `RateLimiter` via the new
>   `notify_429(host, retry_after)` so the next `acquire(host)` waits
>   long enough, then raises a retryable `Exception` so `async_retry`
>   retries. Decorator jitter stacks on top of the rate-limiter wait.
> * **`parse_retry_after(header_value: str | None) -> float | None`**
>   handles both RFC 9110 §10.2.3 forms (integer delta-seconds + HTTP
>   date). Exported from `web_agent` for callers writing custom
>   backoff logic.
> * **Telemetry depth**: `NetworkEvent` gains `ttfb_ms` (from
>   Playwright's `request.timing['responseStart']`) and `body_size`
>   (from `Content-Length`). `FetchResult` gains `ttfb_ms` (first
>   `document`-typed response), `dom_parse_ms` (computed via
>   `performance.getEntriesByType('navigation')`), and
>   `total_bytes_downloaded` (page weight = sum across all response
>   events; use `len(html)` for the navigation's response body
>   size). All five fields are `Optional[...]` with `None` default
>   -- existing `FetchResult(...)` callers see no signature break.
> * **JSON-LD enrichment (always-on)**.
>   `ExtractionResult.structured_data: list[dict]` is populated from
>   `<script type="application/ld+json">` blocks on every HTML
>   extraction. Schema.org Product / Article / Recipe / Event /
>   BreadcrumbList objects ship straight through. `@graph`
>   containers are unwrapped. Malformed JSON-LD is swallowed.
> * **XHR/fetch body capture (opt-in)**. New
>   `DiagnosticsConfig.capture_response_bodies` (default `False`).
>   When enabled, JSON-typed XHR/fetch response bodies are captured
>   onto `NetworkEvent.body_text` (capped by
>   `max_response_body_bytes`, default 256 KiB; `body_truncated`
>   signals when truncation happened). Capture is async-scheduled;
>   the fetch automatically drains pending captures before
>   snapshotting.
> * **`ContentExtractor.extract(prefer_api=True)`**. Routes
>   extraction through the largest captured JSON body when one is
>   available -- emits `extraction_method="api_json"`. Cleaner than
>   the rendered DOM on SPAs that ship a `/api/page-data` payload.
>   Falls back transparently to trafilatura/bs4/raw when no usable
>   body was captured.
>
> See [CHANGELOG.md](CHANGELOG.md#1612---2026-05-21) for the full
> entry including the 429 behaviour-change migration note.
>
> **v1.6.11** — *Follow-up polish patch.* No new features; seven
> items addressing one behavioural issue, one correctness gap, one
> stale-migration-wording bug, plus four polish items. Headlines:
>
> * **`web_research(extract_files=True)` skips non-extractable kinds
>   before fetching.** Pre-v1.6.11, `.mp4` / `.exe` / `.iso` / `.zip`
>   results were fetched as binary then caught post-fetch by the
>   v1.6.10 I-1 guard (wasted bandwidth). v1.6.11 filters against the
>   new `EXTRACTABLE_BINARY_KINDS = {"pdf", "xlsx", "docx", "csv"}`
>   set BEFORE `fetch_smart` runs; rejected URLs land in
>   `download_candidates` with `block_reason="not_extractable_kind"`.
> * **`find_and_download_file` no longer returns the wrong file
>   type.** Pre-v1.6.11, a caller asking for `file_types=["pdf"]`
>   over a result set containing only `.xlsx` URLs silently got an
>   `.xlsx`. v1.6.11 removes the "any download URL" Fallback 1
>   entirely; Tier 1 (extension match) and the HEAD-probe fallback
>   are now sufficient. **Migration**: widen `file_types` (e.g.
>   `["pdf", "xlsx", "docx", "csv"]`) to opt into multi-kind search.
> * **`EXTRACTABLE_BINARY_KINDS` + `is_extractable_binary_kind()`**
>   are public-stable helpers exported from `web_agent` for callers
>   who want to filter URLs identically before calling `fetch_smart`
>   themselves.
> * **Docs cleanup**: CHANGELOG migration sentence (`_is_binary_kind`
>   -> `is_binary_kind`), `fetch_smart` / `_inspect_element_at_point`
>   docstrings refreshed to v1.6.10 semantics, README MCP-tool count
>   replaced with category wording (no more "37 tools" drift),
>   SECURITY.md `cdp_host` wording aligned with `remote_cdp_url`
>   (`127.0.0.0/8 / ::1 / localhost`).
>
> See [CHANGELOG.md](CHANGELOG.md#1611---2026-05-18) for the full
> list including the two behaviour-change migration notes.
>
> **v1.6.10** — *Follow-up hardening patch.* No new features; eight
> items addressing one functional bug plus seven consistency / UX
> gaps. Headlines:
>
> * **`web_research` no longer drops binary results** -- the v1.6.9
>   gate (`not fr.html`) silently logged successful binary
>   `FetchResult`s from `fetch_smart` as `fetch_failed`; v1.6.10
>   gates on `not (fr.html or fr.binary)`.
> * **`web_research(extract_files=False)`** mirrors
>   `search_and_extract(extract_files=…)`. When True, file URLs are
>   extracted inline via `fetch_smart`.
> * **Richer file classification.** `WebFetcher.classify_url` returns
>   one of `'pdf' | 'xlsx' | 'docx' | 'csv' | 'zip' | 'binary_other'
>   | 'html' | 'unknown'` instead of collapsing every binary to
>   `'binary'`. New `is_binary_kind(c)` helper
>   (`from web_agent import is_binary_kind`) is the migration target.
>   **Breaking** for direct callers comparing to `"binary"`.
> * **`Agent.get_owned_cdp_connection_info()`** returns a structured
>   `CdpConnectionInfo` (cdp_url, profile_dir, ownership_token) or
>   `None`. New MCP tool `web_get_owned_cdp_connection_info`.
> * **`SafetyConfig.coordinate_click_unknown_policy`** (`"allow"` |
>   `"block"`, default `"allow"`, forced `"block"` in `safe_mode`).
>   When `"block"`, `click_xy` rejects clicks where `elementFromPoint`
>   returns no element. Independent of `allow_form_submit`.
> * **`BrowserConfig.cdp_host` uses `_is_loopback_host`** — accepts
>   `127.0.0.0/8` + `::1` + `localhost`.
> * **Named profiles share one `BrowserContext`** (Playwright
>   limitation, surfaced in README + AGENTS).
>
> **v1.6.9** — *Hardening patch.* No new features; ten safety +
> consistency fixes. Headlines:
>
> * **`click_xy` no longer bypasses safety.** New
>   `SafetyConfig.allow_coordinate_clicks` (default True, forced False
>   in `safe_mode`) plus a `document.elementFromPoint(x, y)` inspector
>   that blocks submit / login / delete / pay controls when
>   `allow_form_submit=False`.
> * **`remote_cdp` ownership tokens.** v1.6.8 attached to any loopback
>   `ws://` URL — including a user's personal Chrome. v1.6.9 requires
>   `BrowserConfig.remote_cdp_ownership_token` matching a file webTool
>   writes into the launcher's profile dir (`OwnershipToken`).
> * **Named profiles now use `chromium.launch_persistent_context`** so
>   cookies / localStorage actually survive across `Agent` lifetimes.
> * **`--no-sandbox` auto-detected** — opt-in or CI/container only;
>   local dev keeps the sandbox enabled.
> * **Shared smart-binary routing** via new `WebFetcher.fetch_smart`
>   used by every recipe (no more extensionless PDFs slipping into the
>   HTML extractor).
> * **`mcp` is now an optional extra** — `pip install
>   "web-agent-toolkit[mcp]"` to run the MCP server.
> * Configurable `BrowserConfig.locale` / `timezone_id` /
>   `user_agent_mode` / `user_agent`.
> * `SkillsConfig.enabled` → `project_skills_enabled` (deprecated alias
>   retained for one release).
>
> See [CHANGELOG.md](CHANGELOG.md#169) for the full list and the
> backward-compatibility notes (one intentional break for
> `remote_cdp` configs without a token).
>
> **v1.6.8** — *Diagnostics and Advanced Browser Intelligence.*
> Six features, all off by default, make webTool explainable and
> debuggable: **network event capture** (`page.on(request|response|requestfailed)`
> hooks via `WeakKeyDictionary`-backed `NetworkCollector`); **API endpoint
> candidate discovery** derived from captured events; **download event
> diagnostics** (separate `page.on('download')` notification listener that
> auto-deletes the Chromium tmpfile); **post-action screenshot
> verification** (`verify-<cid>-<index>.png` after each successful action);
> **session replay / audit traces** with new `Agent.replay_trace(file)` +
> CLI `web-agent replay <trace_file>`; **remote CDP backend** —
> `backend="remote_cdp"` + `remote_cdp_url` dispatches to
> `chromium.connect_over_cdp()` (v1.6.9 adds ownership tokens, see above).
> See [CHANGELOG.md](CHANGELOG.md#168) for the full list.
>
> **v1.6.7** added: *Skills and Playbooks.* Domain skill registry
> (`Agent.list_domain_skills / get_domain_skills(url) / apply_domain_skill`),
> markdown skills with YAML frontmatter at three priority tiers (project >
> workspace > builtin), 3 bundled runnable skills (sec.gov, github.com,
> ec.europa.eu), agent-editable workspace with 4 safety modes
> (`read_only` / `markdown_skills_only` (default) / `reviewed_python_helpers`
> / `unsafe_python_helpers`), and 8 new interaction-library methods
> (`handle_dialog`, `select_dropdown`, `upload_file`, `drag_and_drop`,
> `scroll_until_text`, `click_inside_iframe`, `click_shadow_dom`,
> `print_page_as_pdf`).
>
> **v1.6.6** added: *Browser Control Foundation.* Isolation-profile
> launcher (`BrowserConfig.isolation_mode` + ephemeral/named profiles),
> Chrome DevTools Protocol attach to webTool-launched browsers
> (`BrowserConfig.cdp_enabled` + `Agent.get_cdp_endpoint()`), per-session
> tab management (`agent.list_tabs / new_tab / switch_tab / close_tab`),
> coordinate-level click fallbacks (`click_xy / type_text / press_key`),
> observe mode (`Agent.observe()` returns screenshot + viewport + DPR +
> ARIA snapshot), and `Agent.doctor()` self-diagnostic with 19 capability
> probes (CLI: `web-agent doctor`).
>
> **Earlier versions** (1.6.5 cookie isolation / SSRF hardening; 1.6.4
> cross-platform paths; 1.6.3 smart routing; 1.6.2 binary fetch +
> ranking profiles; 1.6.1 failure surface) live in
> [CHANGELOG.md](CHANGELOG.md).

## Features

### Core web pipeline
- **Web Search** — Free-first provider chain: SearXNG → DDGS → Playwright fallback. URL-as-query short-circuits to direct fetch. Per-provider circuit breaker + a links-only `Agent.search()` primitive (v1.7.0).
- **Bot-Wall Detection** (v1.7.0) — Structural Cloudflare / DataDome / Akamai / PerimeterX / reCAPTCHA / hCaptcha detection; a challenge on HTTP 200 returns `FetchStatus.BLOCKED` with an actionable message instead of garbage.
- **Page Fetching** — Renders JavaScript, retries with exponential backoff, detects download URLs. Optional disk cache (TTL-based).
- **Content Extraction** — Three-tier fallback: trafilatura (F1 ≈ 0.958) → BeautifulSoup4 → raw text. CSV / PDF / XLSX / DOCX extraction via the `[binary]` extra. PDF now prefers `pdfplumber` for per-page text + GFM-markdown tables on `ExtractionResult.tables` (v1.7.0).
- **Untrusted-Content Containment** (v1.7.0) — Fetched content is treated as untrusted: invisible / bidi-override Unicode and hidden DOM (`display:none` / `aria-hidden` / off-screen) are stripped **before** extraction; visible prompt-injection is flagged advisorily on `ExtractionResult.injection`, never blocked by default.
- **Pagination + Infinite-Scroll Collection** (v1.7.0) — `scroll_to_bottom` exhausts lazy/infinite-scroll content; `collect_across_pages` walks multi-page listings (`next_link` / `page_param` / `scroll`), every page re-gated through `fetch` and bounded by `max_pages` + budget.
- **File Download** — Three strategies: httpx streaming → Playwright page save → Playwright JS download. Per-strategy size caps enforced.
- **High-Level Recipes** — `search_and_open_best_result`, `find_and_download_file`, `web_research`, `fill_form_and_extract`, `collect_across_pages` (v1.7.0).

### Browser automation (v1.6.5 – v1.6.7)
- **19 Action Types** — composable into scripted sequences. Includes `click_xy / type_text / press_key` (coordinate fallbacks for canvas/shadow/iframe), `upload_file`, `drag_and_drop`, `iframe_click`, `shadow_dom_click`.
- **Semantic Locators** — Find elements by ARIA role, label, text, or test_id (not just CSS).
- **Browser Sessions** — Persistent named contexts retain cookies/login across multi-call workflows.
- **Tab Management** (v1.6.6) — `agent.list_tabs / new_tab / switch_tab / close_tab`. Popups auto-register without stealing focus.
- **Observe Mode** (v1.6.6) — `Agent.observe()` returns screenshot path + viewport + page size + DPR + optional ARIA snapshot. Powers observe → act → verify loops.
- **Set-of-Marks Targeting** (v1.7.0) — `observe()` also returns a bounded, numbered list of interactive elements on `ObserveResult.elements` (`ref` / role / name / tag / enabled / visible / `bbox` / selector); an action targets one by `ref` via `LocatorSpec(ref="e3")` instead of guessing a CSS selector. Reuses every locator + safety gate; the ref pattern is injection-proof.
- **8 Top-Level Interaction Methods** (v1.6.7) — `handle_dialog`, `select_dropdown`, `upload_file`, `drag_and_drop`, `scroll_until_text`, `click_inside_iframe`, `click_shadow_dom`, `print_page_as_pdf`.

### Browser-control backends (v1.6.6 / v1.6.8)
- **Isolation Profile Launcher** — webTool-owned `--user-data-dir` (ephemeral tempdir or named persistent profile). Isolates cookies / localStorage / cache / downloads from the user's real Chrome.
- **CDP Attach** — `backend="cdp_owned"` opens a `--remote-debugging-port` on the webTool-launched browser; external tools attach via `Agent.get_cdp_endpoint()`. **Never attaches to the user's existing Chrome** (rejected at config validation).
- **Remote CDP Backend** (v1.6.8) — `backend="remote_cdp"` + `remote_cdp_url` dispatches to `chromium.connect_over_cdp(url)`. Loopback-only validator rejects non-`127.0.0.0/8` URLs.

### Domain skills + workspace (v1.6.7)
- **Domain Skills Registry** — Markdown skills with YAML frontmatter at three priority tiers (project > workspace > builtin). `agent.list_domain_skills() / get_domain_skills(url) / apply_domain_skill(url, name, inputs)`.
- **3 Bundled Runnable Skills** — `sec.gov/filing_search`, `github.com/release_download`, `ec.europa.eu/document_search`.
- **Agent-Editable Workspace** — Four safety modes: `read_only` / `markdown_skills_only` (default) / `reviewed_python_helpers` / `unsafe_python_helpers`. Workspace skills auto-load into the registry.

### Diagnostics (v1.6.8)
- **Network Event Capture** — `page.on(request|response|requestfailed)` hooks attached to every Page. Surfaced as `FetchResult.network_events` / `ActionSequenceResult.network_events`.
- **API Endpoint Candidate Discovery** — XHR/fetch JSON responses, de-duplicated, on `FetchResult.api_candidates`.
- **Download Event Diagnostics** — separate `page.on('download')` notification (auto-deletes Chromium tmpfile) surfaces as `download_candidates_runtime`.
- **Post-Action Screenshot Verification** — best-effort `verify-<cid>-<index>.png` per successful action.
- **Session Replay** — per-session JSONL trace at `<base_dir>/.webtool-audit/traces/<sid>.jsonl`. Replay via `Agent.replay_trace(file)` or CLI `web-agent replay <file>`.
- **Doctor** (v1.6.6) — `Agent.doctor()` runs 19 capability probes; CLI `web-agent doctor [--json]` exits 2 on `unusable` so CI can gate on it.

### Safety + observability
- **Safety Controls** — Domain allow/deny lists, granular `allow_*` flags, SSRF protection (RFC1918 + loopback + link-local), per-call budgets.
- **Per-Host Cookie Isolation** (v1.6.5) — Domain-aware `httpx.Cookies` jar; cookies for `bank.com` never leak to `attacker.com` when both share a `session_id`.
- **Politeness Layer** — Per-host rate limiter + robots.txt obedience (both opt-out).
- **Audit Log** — Append-only JSONL of every Agent operation (off by default).
- **Disk Cache** — TTL cache for fetch + search results, LRU-by-mtime eviction (off by default).
- **Retry Profiles** — Declarative `fast` / `balanced` / `paranoid` policies.
- **Debug Mode** — Auto-capture HTML / screenshot / error JSON on failures.
- **Correlation IDs** — Single-request tracing across all subsystems via auto-injected log fields (now surfaced in the MCP + CLI log formats via `{extra[cid]}`, v1.7.0).
- **In-Process Metrics** (v1.7.0) — `MetricsRegistry` counters + count/sum/min/max distributions instrumented across fetch / search / browser-lifecycle hot paths, with a per-metric label-cardinality cap. `Agent.metrics() -> MetricsSnapshot`; MCP `web_metrics`. Enabled by default; `disabled` is a near-zero-cost no-op.
- **Anti-Detection** — playwright-stealth, user-agent rotation, resource blocking.
- **Structured Output** — All results are Pydantic v2 models serializable to JSON.

### Integration
- **MCP Server** — search, fetch, download, browser automation, sessions, tabs, CDP, diagnostics, skills, and recipes exposed to Claude Desktop, Claude Code, Cursor, OpenAI Codex, OpenClaw, and any other MCP-compatible AI client.
- **CLI** — `web-agent search / fetch / download / interact / screenshot / observe / skills / doctor / replay`.
- **Docker Container** (v1.7.0) — `docker/Dockerfile` on the official Playwright Python base (Chromium + deps + fonts baked in), runs as the non-root `pwuser`, with a `HEALTHCHECK` and an `ENTRYPOINT` that launches the MCP server. See [`docker/README.md`](docker/README.md).

## Installation

> **Source-install only.** `web-agent-toolkit` is not on PyPI yet. Install from source:

```bash
# From a local clone (recommended for development):
git clone https://github.com/agilamlabs/webTool.git
cd webTool
pip install -e ".[dev]"
playwright install chromium

# Or directly from the GitHub URL:
pip install "web-agent-toolkit @ git+https://github.com/agilamlabs/webTool.git"
playwright install chromium
```

On Linux, use `--with-deps` to auto-install Chromium's system dependencies:

```bash
playwright install --with-deps chromium
```

**Optional binary-document extractors** (PDF via `pdfplumber` + `pypdf` / XLSX / DOCX; CSV is stdlib):

```bash
pip install -e ".[dev,binary]"
```

**Optional MCP server** (v1.6.9+). The Python API works without
`mcp[cli]`; install the `[mcp]` extra to run the MCP server:

```bash
pip install -e ".[mcp]"            # MCP server only
pip install -e ".[dev,mcp,binary]" # everything
```

**Requirements:** Python 3.10+ (3.10, 3.11, 3.12, 3.13 tested in CI)

## Quick Start

### Python API (recommended for AI agents)

```python
import asyncio
from web_agent import Agent

async def main():
    async with Agent() as agent:
        # Search the web and extract content from top results
        result = await agent.search_and_extract("Python web scraping", max_results=5)
        for page in result.pages:
            print(f"{page.title}: {page.content_length} chars")

        # Fetch and extract a single page
        page = await agent.fetch_and_extract("https://example.com")
        print(page.content[:200])

        # Download a file
        dl = await agent.download("https://example.com/report.pdf")
        print(f"Saved to {dl.filepath} ({dl.size_bytes} bytes)")

        # Take a screenshot
        ss = await agent.screenshot("https://example.com", full_page=True)
        print(f"Screenshot: {ss.path}")

asyncio.run(main())
```

### CLI

```bash
# Search and extract
python -m web_agent search "latest AI research papers" --max-results 5

# Fetch a single page
python -m web_agent fetch "https://example.com"

# Download a file
python -m web_agent download "https://www.sec.gov/Archives/edgar/data/1652044/000165204425000014/goog-20241231.htm"

# Take a screenshot
python -m web_agent screenshot "https://example.com" --full-page

# Run a browser automation sequence
python -m web_agent interact "https://example.com" --actions actions.json
```

## Configuration

### Programmatic (recommended)

No config file needed. All settings have sensible defaults:

```python
from web_agent import Agent, AppConfig

# All defaults:
async with Agent() as agent:
    ...

# Custom config:
config = AppConfig(
    browser={"headless": False, "max_contexts": 5},
    search={"max_results": 20, "language": "en"},
    fetch={"max_retries": 5},
    log_level="DEBUG",
    output_dir="/tmp/results",
)
async with Agent(config) as agent:
    ...
```

### Environment Variables

All settings support env vars with the `WEB_AGENT_` prefix:

```bash
export WEB_AGENT_LOG_LEVEL=DEBUG
export WEB_AGENT_BROWSER__HEADLESS=false
export WEB_AGENT_SEARCH__MAX_RESULTS=20
export WEB_AGENT_FETCH__MAX_RETRIES=5
```

### YAML File

```bash
python -m web_agent --config config.yaml search "query"
```

See [config.example.yaml](config.example.yaml) for all available options.

### Configuration Reference

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `browser` | `headless` | `true` | Run browser without visible window |
| `browser` | `slow_mo` | `0` | Delay (ms) between Playwright operations -- debugging aid |
| `browser` | `max_contexts` | `3` | Max concurrent browser contexts |
| `browser` | `default_timeout` | `30000` | Default action timeout (ms) |
| `browser` | `navigation_timeout` | `45000` | Page navigation timeout (ms) |
| `browser` | `block_resources` | `["image","font","stylesheet","media"]` | Resource types to block for speed |
| `browser` | `viewport_width` | `1920` | Browser viewport width |
| `browser` | `viewport_height` | `1080` | Browser viewport height |
| `search` | `max_results` | `10` | Number of search results to return |
| `search` | `language` | `"en"` | Search language |
| `search` | `region` | `"us"` | Search region |
| `fetch` | `wait_until` | `"domcontentloaded"` | Wait condition (`domcontentloaded`, `load`, `networkidle`); changed in 1.6.2 from `networkidle` for robustness against pages that poll/analytics |
| `fetch` | `retry_policy` | `"balanced"` | Profile: `fast` / `balanced` / `paranoid` |
| `fetch` | `max_retries` | `3` | Retry count (overrides policy when set) |
| `fetch` | `retry_base_delay` | `1.0` | Base delay (seconds) for exponential backoff |
| `download` | `download_dir` | `"./downloads"` | Directory for downloaded files |
| `download` | `max_file_size_mb` | `100` | Maximum file size limit |
| `extraction` | `favor_recall` | `true` | Prefer extracting more content vs precision |
| `extraction` | `include_tables` | `true` | Include table data in extraction |
| `extraction` | `min_content_length` | `50` | Minimum characters to accept extraction |
| `automation` | `default_action_timeout` | `10000` | Timeout per browser action (ms) |
| `automation` | `screenshot_dir` | `"./screenshots"` | Directory for screenshots |
| `automation` | `stop_on_error` | `true` | Halt action sequence on first failure |
| `safety` | `allowed_domains` | `[]` | Suffix-match allow-list (empty = allow all) |
| `safety` | `denied_domains` | `[]` | Suffix-match deny-list (takes precedence) |
| `safety` | `safe_mode` | `false` | Block downloads, JS eval, submit clicks |
| `safety` | `max_pages_per_call` | `50` | Pages-per-call budget |
| `safety` | `max_chars_per_call` | `1000000` | Chars-per-call extraction budget |
| `safety` | `max_time_per_call_seconds` | `300` | Wall-clock budget per Agent call |
| `debug` | `enabled` | `false` | Auto-capture HTML/screenshot/JSON on failures |
| `debug` | `debug_dir` | `"./debug"` | Directory for failure artifacts |
| `debug` | `capture_html` | `true` | Save HTML snapshot on failure |
| `debug` | `capture_screenshot` | `true` | Save PNG snapshot on failure |

**Isolation + CDP** (v1.6.6):

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `browser` | `isolation_mode` | `true` | Launch with `--user-data-dir` so cookies/cache/downloads are isolated from real Chrome. Default-on since v1.7.0; set `false` for a plain non-isolated browser (also auto-disables `cdp_enabled`) |
| `browser` | `profile_mode` | `"ephemeral"` | `"ephemeral"` (auto tempdir) or `"named"` (persistent at `profile_dir`) |
| `browser` | `profile_dir` | `None` | Required when `profile_mode="named"`. Resolved against `base_dir` if relative |
| `browser` | `cleanup_on_exit` | `true` | Remove the ephemeral profile dir on Agent exit |
| `browser` | `backend` | `"playwright"` | `"playwright"` / `"cdp_owned"` / `"remote_cdp"` (v1.6.8) |
| `browser` | `cdp_enabled` | `true` | Launch with `--remote-debugging-port` so external tools can attach via CDP. Requires `isolation_mode=true`. Default-on since v1.7.0. **Security:** the port is loopback-bound but the CDP endpoint is **unauthenticated** — any local process can drive the browser; set `false` on shared/multi-tenant hosts or with sensitive sessions (see [SECURITY.md](SECURITY.md)) |
| `browser` | `cdp_host` | `"127.0.0.1"` | Loopback only -- non-loopback rejected at validation |
| `browser` | `cdp_port` | `0` | `0` = OS-assigned, discovered via `DevToolsActivePort` |
| `browser` | `remote_cdp_url` | `None` | Required when `backend="remote_cdp"`. Must be a loopback `ws://` / `wss://` URL (v1.6.8) |

> **Warning — Named profiles are not session-isolated (v1.6.9 / v1.6.10).**
> `profile_mode="named"` uses Playwright's `chromium.launch_persistent_context`,
> which exposes a **single shared `BrowserContext`** for every session on
> that profile (Playwright limitation: one persistent context per
> user-data-dir). All `session_id`s on a named-profile Agent share
> cookies, localStorage, IndexedDB, and cache. Use `profile_mode="ephemeral"`
> when per-session isolation matters.

**Skills + workspace** (v1.6.7):

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `skills` | `enabled` | `false` | Master switch for the project-tier skill load (workspace + builtin tiers govern themselves) |
| `skills` | `skill_dirs` | `["./.webtool-skills"]` | Project skill directories (highest priority). First match on `(domain, name)` wins |
| `skills` | `builtin_skills_enabled` | `true` | Include bundled `sec.gov` / `github.com` / `ec.europa.eu` skills |
| `workspace` | `enabled` | `false` | Master switch -- workspace is invisible to Agent when false |
| `workspace` | `workspace_dir` | `"./.webtool-workspace"` | Workspace root (resolved against `base_dir` if relative) |
| `workspace` | `mode` | `"markdown_skills_only"` | `read_only` / `markdown_skills_only` / `reviewed_python_helpers` / `unsafe_python_helpers` |
| `workspace` | `execute_helpers` | `false` | Second opt-in: import and expose `helpers.py` to skills |

**Diagnostics** (v1.6.8 — all default-off):

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `diagnostics` | `capture_network` | `false` | Hook `page.on(request/response/requestfailed)` on every Page |
| `diagnostics` | `max_network_events` | `500` | Hard cap on retained events per Page (deque maxlen) |
| `diagnostics` | `network_resource_types` | `["xhr","fetch","document"]` | Playwright `resource_type` values to record |
| `diagnostics` | `include_request_headers` | `false` | Capture request headers (off by default — Authorization / Cookie are sensitive) |
| `diagnostics` | `include_response_headers` | `false` | Capture response headers |
| `diagnostics` | `capture_download_intents` | `false` | Attach `page.on('download')` notification + auto-delete tmpfile |
| `diagnostics` | `screenshot_after_action` | `false` | Best-effort `verify-<cid>-<index>.png` per successful action |
| `diagnostics` | `trace_enabled` | `false` | Write per-session JSONL action log to `trace_dir` |
| `diagnostics` | `trace_dir` | `"./.webtool-audit/traces"` | Trace JSONL directory (resolved against `base_dir` if relative) |

**Real-world hardening** (v1.7.0):

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `fetch` | `challenge_detection_enabled` | `true` | Detect bot-walls / CAPTCHAs and return `FetchStatus.BLOCKED` instead of garbage |
| `fetch` | `challenge_settle_ms` | `3500` | Settle delay before re-capturing a managed-JS challenge the browser may auto-pass |
| `fetch` | `challenge_max_rechecks` | `2` | Max settle-and-recheck attempts before giving up as `BLOCKED` |
| `extraction` | `default_max_chars` | `40000` | Per-response content cap **at the MCP boundary only** (Python API stays unlimited) |
| `extraction` | `pdf_extract_tables` | `true` | Extract PDF tables (via `pdfplumber`) as GFM markdown into `content` + `ExtractionResult.tables` |
| `extraction` | `pdf_page_markers` | `true` | Emit `===== Page N =====` markers + populate `page_count` so callers can cite pages |
| `extraction` | `pdf_max_tables` | `50` | Cap on tables extracted per PDF (truncation flagged) |
| `extraction` | `pdf_max_table_cells` | `2000` | Cap on total table cells per PDF (truncation flagged) |
| `safety` | `sanitize_fetched_content` | `true` | Strip hidden-from-humans DOM + invisible/bidi Unicode before extraction |
| `safety` | `detect_prompt_injection` | `true` | Run advisory `detect_injection` → `ExtractionResult.injection` (`InjectionReport`) |
| `safety` | `injection_action` | `"flag"` | `"flag"` (default) / `"redact"` / `"block"`; never blocks legitimate content by default |
| `automation` | `pagination_max_pages` | `10` | Max pages `collect_across_pages` walks before stopping (`stopped_reason="max_pages"`) |
| `automation` | `scroll_stable_rounds` | `2` | Rounds `scrollHeight` must stay stable before `scroll_to_bottom` declares exhaustion |
| `automation` | `scroll_settle_ms` | `1000` | Settle delay (ms) between scroll rounds |
| `automation` | `pagination_next_texts` | (vocabulary) | Anchor-text vocabulary the `next_link` strategy matches (next / older / more / load-more / chevron) |
| `automation` | `observe_max_elements` | `100` | Cap on interactive elements returned in `ObserveResult.elements` (`elements_truncated` flags overflow) |
| `automation` | `observe_tag_refs` | `true` | Stamp a session-scoped `data-webtool-ref` per observed element so `LocatorSpec(ref=...)` can target it |
| `browser` | `stealth_enabled` | `true` | Apply playwright-stealth per context |
| `browser` | `auto_relaunch` | `true` | Transparently relaunch Chromium on disconnect/crash |
| `browser` | `relaunch_max_attempts` | `3` | Bounded relaunch attempts |
| `browser` | `relaunch_backoff_base_s` | `1.0` | Base backoff (seconds) between relaunch attempts |
| `browser` | `session_max_count` | `32` | Hard cap on concurrent persistent sessions |
| `browser` | `session_idle_ttl_s` | `1800` | Idle-session reaper TTL (seconds) |
| `browser` | `profile_sweep_max_age_h` | `24` | Age (hours) above which orphaned ephemeral profile dirs are swept on start |
| `browser` | `coherent_fingerprint` | `true` | Pin the rotated UA's OS family to the configured locale; coherent UA on httpx side-paths |
| `search` | `circuit_cooldown_s` | `60.0` | Per-provider circuit-breaker cooldown after a block/error |
| `proxy` | `server` | `None` | Proxy URL (`http` / `https` / `socks5`); inactive by default. Env `WEB_AGENT_PROXY__SERVER` |
| `proxy` | `username` | `None` | Proxy auth username. Env `WEB_AGENT_PROXY__USERNAME` |
| `proxy` | `password` | `None` | Proxy auth password. Env `WEB_AGENT_PROXY__PASSWORD` |
| `proxy` | `bypass` | `None` | Comma-separated no-proxy hosts. Env `WEB_AGENT_PROXY__BYPASS` |
| `metrics` | `enabled` | `true` | In-process `MetricsRegistry` on/off; disabled is a near-zero-cost no-op. Env `WEB_AGENT_METRICS__ENABLED` |
| `metrics` | `max_label_cardinality` | `200` | Per-metric distinct-label cap; overflow folds into an `_other` bucket. Env `WEB_AGENT_METRICS__MAX_LABEL_CARDINALITY` |

**Safety** (additional v1.6.5+ knobs):

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `safety` | `allow_js_evaluation` | `false` | Allow `EvaluateInput` actions (LLM-supplied JS can exfiltrate cookies) |
| `safety` | `allow_downloads` | `true` | Permit download actions |
| `safety` | `allow_form_submit` | `true` | Permit clicks on submit-typed buttons |
| `safety` | `block_private_ips` | `true` | SSRF protection: blocks RFC1918, loopback, link-local |
| `safety` | `allow_upload_outside_download_dir` | `false` | Widen `upload_file` paths beyond `download.download_dir` (v1.6.7) |
| `safety` | `probe_binary_urls` | `true` | HEAD-probe extensionless URLs to detect PDF / XLSX (v1.6.2) |
| `safety` | `rate_limit_per_host_rps` | `2.0` | Per-host rate cap (req/sec); `0` to disable |
| `safety` | `respect_robots_txt` | `true` | Fetch + obey robots.txt before each request |
| `safety` | `robots_user_agent` | `"web-agent-toolkit"` | UA for robots.txt fetches + rule matching |
| `audit` | `enabled` | `false` | Append-only JSONL log of every public Agent call |
| `audit` | `audit_log_path` | `"./audit.jsonl"` | Audit log path (resolved against `base_dir` if relative) |
| `cache` | `enabled` | `false` | Disk-backed TTL cache for fetch + search |
| `cache` | `cache_dir` | `"./cache"` | Cache directory |
| `cache` | `ttl_seconds` | `3600` | Entry TTL |
| `cache` | `max_cache_mb` | `100` | Cap (LRU-by-mtime eviction) |

## API Reference

### Agent Methods

The public surface grew across versions; every method below is on the `Agent` class. Each `async def` wraps a correlation scope + audit log entry. Pass `session_id=...` to reuse a persistent browser session for cookie / login continuity.

```python
class Agent:
    # --- Core pipeline ---
    async def search_and_extract(query, max_results=None, *, session_id=None, extract_files=False) -> AgentResult
    async def fetch_and_extract(url, *, session_id=None) -> ExtractionResult
    async def download(url, filename=None, *, session_id=None) -> DownloadResult
    async def screenshot(url, path=None, full_page=False, *, session_id=None) -> ScreenshotResult
    async def interact(url, actions, stop_on_error=None, *, session_id=None) -> ActionSequenceResult

    # --- Recipes ---
    async def search_and_open_best_result(query, *, prefer_domains=None, domain_profile=None) -> ExtractionResult
    async def find_and_download_file(query, *, file_types=None) -> DownloadResult
    async def web_research(query, max_pages=5, depth=1, *, prefer_domains=None) -> ResearchResult
    async def fill_form_and_extract(url, spec: FormFilterSpec) -> AgentResult

    # --- Sessions + tabs ---  (v1.6.6)
    async def create_session(name=None) -> str
    async def close_session(session_id) -> None
    def list_sessions() -> list[SessionInfo]
    async def list_tabs(session_id) -> list[TabInfo]
    async def current_tab(session_id) -> TabInfo | None
    async def new_tab(session_id, url=None) -> str
    async def switch_tab(session_id, tab_id) -> None
    async def close_tab(session_id, tab_id) -> None

    # --- Observe + low-level fallbacks ---  (v1.6.6)
    async def observe(url, *, session_id, tab_id=None, include_text=True, include_aria=False) -> ObserveResult
    async def click_xy(x, y, *, session_id, tab_id=None, button="left", modifiers=None) -> ActionResult
    async def type_text(text, *, session_id, tab_id=None, delay=0) -> ActionResult
    async def press_key(key, *, session_id, tab_id=None, repeat=1) -> ActionResult

    # --- Domain skills + workspace ---  (v1.6.7)
    def list_domain_skills() -> list[DomainSkill]
    def get_domain_skills(url) -> list[DomainSkill]
    async def get_domain_skill(url, name) -> DomainSkill | None
    async def apply_domain_skill(url, name, inputs=None, *, session_id=None) -> SkillApplicationResult

    # --- Interaction-skill library ---  (v1.6.7)
    async def handle_dialog(action="accept", prompt_text=None, *, session_id) -> ActionResult
    async def select_dropdown(selector, *, session_id, value=None, label=None, index=None) -> ActionResult
    async def upload_file(selector, paths, *, session_id) -> ActionResult
    async def drag_and_drop(source, target, *, session_id) -> ActionResult
    async def scroll_until_text(text, *, session_id, max_scrolls=10, scroll_step=800) -> ActionResult
    async def click_inside_iframe(iframe_selector, inner_selector, *, session_id) -> ActionResult
    async def click_shadow_dom(host_selector, inner_selector, *, session_id) -> ActionResult
    async def print_page_as_pdf(url=None, output_path=None, *, session_id=None) -> ScreenshotResult

    # --- Diagnostics + replay ---  (v1.6.6 + v1.6.8)
    def get_cdp_endpoint() -> str | None
    def get_remote_cdp_url() -> str | None
    async def doctor(quick=False) -> DoctorReport
    def list_traces() -> list[str]
    async def replay_trace(trace_file) -> ActionSequenceResult

    # --- Real-world hardening ---  (v1.7.0)
    async def search(query, max_results=None, *, strict=False) -> SearchResponse        # links-only; sets search_blocked
    async def export_session_state(session_id, filename) -> StorageStateResult          # save logged-in storage_state
    async def import_session_state(filename, *, name=None) -> StorageStateResult         # rehydrate in a later process
    async def scroll_to_bottom(*, session_id, tab_id=None) -> ActionResult              # exhaust lazy/infinite-scroll content
    async def collect_across_pages(url, *, strategy="next_link", max_pages=None, session_id=None) -> CollectionResult  # walk paginated listings
    def metrics() -> MetricsSnapshot                                                     # in-process counters + distributions snapshot

    # --- Output ---
    async def save_results(result, output_path=None) -> Path
```

For exhaustive parameter docs, read the docstrings on each method (`help(agent.fetch_and_extract)`). Every signature is also exercised in `tests/`.

### Result Models

**AgentResult** (from `search_and_extract`):

```python
result.query                # str - original search query
result.search               # SearchResponse - search results with titles, URLs, snippets
result.pages                # list[ExtractionResult] - extracted page content
result.errors               # list[str] - FATAL issues (e.g. "all fetches failed")
result.warnings             # list[str] - NON-FATAL (blocked domains, partial fetches)
result.download_candidates  # list[SearchResultItem] - skipped PDF/XLSX URLs
result.diagnostics          # list[FetchDiagnostic] - per-URL outcomes
result.total_time_ms        # float - pipeline execution time
```

**ExtractionResult** (from `fetch_and_extract`):

```python
result.url               # str
result.title             # str | None
result.description       # str | None
result.author            # str | None
result.date              # str | None
result.sitename          # str | None
result.content           # str | None - main text content
result.language          # str | None
result.extraction_method # "trafilatura" | "bs4" | "raw" | "none" | "pdfplumber" | "pdf" | "api_json"
result.content_length    # int
result.injection         # InjectionReport | None - advisory prompt-injection finding (v1.7.0)
result.content_sanitized # bool - hidden-DOM / invisible-Unicode stripping ran (v1.7.0)
result.page_count        # int | None - PDF page count (v1.7.0)
result.tables            # list - extracted tables as GFM markdown, e.g. from PDF (v1.7.0)
```

**DownloadResult** (from `download`):

```python
result.url          # str
result.filepath     # str - local file path
result.filename     # str
result.size_bytes   # int
result.content_type # str | None
result.status       # "success" | "timeout" | "http_error" | "network_error"
```

**ScreenshotResult** (from `screenshot`):

```python
result.url        # str
result.path       # str - file path
result.format     # "png" | "jpeg"
result.size_bytes # int
result.status     # "success" | "failed"
```

### Browser Automation Actions

**19 action types** composable into sequences (selector-based + coordinate-based + frame / shadow / file-upload):

| Action | Description | Key Fields |
|--------|-------------|------------|
| `ClickInput` | Click an element | `selector`, `button`, `double_click`, `modifiers` |
| `TypeInput` | Type text keystroke-by-keystroke | `selector`, `text`, `delay`, `clear_first` |
| `FillInput` | Set input value instantly | `selector`, `value` |
| `ScrollInput` | Scroll page or element | `direction`, `amount`, `infinite_scroll` |
| `ScreenshotInput` | Capture screenshot | `full_page`, `format`, `quality`, `path` |
| `NavigateInput` | Navigate to URL / back / forward | `url`, `navigate_action`, `wait_until` |
| `DialogInput` | Handle browser dialogs | `dialog_action` (accept/dismiss), `prompt_text` |
| `HoverInput` | Hover over element | `selector` |
| `SelectInput` | Select dropdown option | `selector`, `value` / `label` / `index` |
| `KeyboardInput` | Press keys or combos | `key` (e.g. `"Enter"`, `"Control+A"`), `repeat` |
| `WaitInput` | Wait for condition | `target` (selector/text/url/network_idle), `value` |
| `EvaluateInput` | Run JavaScript (gated by `safety.allow_js_evaluation`) | `expression` |
| `ClickXYInput` *(v1.6.6)* | Click at CSS-pixel coordinates | `x`, `y`, `button`, `modifiers` |
| `TypeTextInput` *(v1.6.6)* | Type at current focus | `text`, `delay` |
| `PressKeyInput` *(v1.6.6)* | Press a single key combo | `key`, `repeat` |
| `UploadFileInput` *(v1.6.7)* | Upload one or more files | `selector`, `paths` |
| `IframeClickInput` *(v1.6.7)* | Click inside a frame | `iframe_selector`, `inner_selector` |
| `ShadowDomClickInput` *(v1.6.7)* | Pierce shadow DOM | `host_selector`, `inner_selector` |
| `DragAndDropInput` *(v1.6.7)* | Drag between selectors | `source`, `target` |

Every action accepts an optional `tab_id` (v1.6.6) to target a specific tab within a session.

#### Example: Form Automation

```python
from web_agent import Agent
from web_agent.models import (
    ClickInput, FillInput, WaitInput, ScreenshotInput, WaitTarget
)

async with Agent() as agent:
    result = await agent.interact("https://httpbin.org/forms/post", [
        WaitInput(target=WaitTarget.SELECTOR, value="input[name='custname']"),
        FillInput(selector="input[name='custname']", value="John Doe"),
        FillInput(selector="input[name='custemail']", value="john@example.com"),
        ClickInput(selector="button[type='submit']"),
        ScreenshotInput(full_page=True),
    ])
    print(f"Actions: {result.actions_succeeded}/{result.actions_total} succeeded")
```

#### Example: Action Sequence from JSON

Create `actions.json`:

```json
[
  {"action": "wait", "target": "selector", "value": "h1"},
  {"action": "evaluate", "expression": "document.title"},
  {"action": "screenshot", "full_page": true},
  {"action": "scroll", "direction": "down", "amount": 5},
  {"action": "evaluate", "expression": "window.scrollY"}
]
```

Run it:

```bash
python -m web_agent interact "https://example.com" --actions actions.json
```

## Architecture

```
Agent (orchestrator)
  |
  |-- SearchEngine          Chain: SearXNG -> DDGS -> Playwright
  |     |-- SearXNGProvider     httpx + self-hosted SearXNG JSON API
  |     |-- DDGSProvider        ddgs package (no browser needed)
  |     `-- PlaywrightProvider  Browser-driven Google + DDG HTML
  |
  |-- WebFetcher            Playwright page rendering + retry
  |-- ContentExtractor      trafilatura -> BS4 -> raw text
  |-- Downloader            httpx -> Playwright page save -> Playwright download
  |-- BrowserActions        12 action handlers with dispatch table
  |
  `-- BrowserManager        Chromium lifecycle, stealth, semaphore pool
```

**Smart routing:**

- **URL-as-query**: `agent.search_and_extract("https://example.com")` skips search entirely and fetches the URL directly
- **Search chain**: SearXNG (skipped silently if `searxng_base_url` not set) -> DDGS (skipped silently if `ddgs` not installed) -> Playwright (always available, slow fallback)
- File URLs (`.pdf`, `.xlsx`, `.zip`) are detected upfront and routed to the downloader
- Web page URLs (`.html`, `.htm`) use Playwright page save instead of download events
- `networkidle` timeouts automatically fall back to `load` wait state
- HTTP 4xx errors fail immediately; 5xx errors retry with exponential backoff

### Configuring the search chain

```python
from web_agent import Agent, AppConfig

# Use a self-hosted SearXNG as the primary source
config = AppConfig(search={
    "providers": ["searxng", "ddgs", "playwright"],
    "searxng_base_url": "http://localhost:8888",
    "searxng_timeout": 10.0,
})

# Or skip browser-based search entirely (faster, but no fallback if APIs fail)
config = AppConfig(search={"providers": ["searxng", "ddgs"]})

# Or only use the legacy Playwright path (1.3.0 behavior)
config = AppConfig(search={"providers": ["playwright"]})

async with Agent(config) as agent:
    result = await agent.search_and_extract("python web scraping")
```

The chain falls through on each provider's empty result or transient error. Pass `strict=True` to raise `SearchError` when the entire chain exhausts:

```python
result = await agent.search_and_extract("query", strict=True)
# raises SearchError if SearXNG, DDGS, and Playwright all fail
```

### Self-hosting SearXNG (recommended for the primary tier)

[SearXNG](https://github.com/searxng/searxng) is a privacy-respecting metasearch engine that aggregates ~80 search backends (Google, Bing, DuckDuckGo, Wikipedia, GitHub, arXiv, etc.) without tracking. Self-hosting gives you the fastest tier of the chain (no browser launch, no third-party rate limit) at the cost of running one container.

The repo ships a tuned config under [`docker/searxng/`](docker/searxng/):

- [`docker-compose.yml`](docker/searxng/docker-compose.yml) — pinned to the official `searxng/searxng:latest` image, bound to `127.0.0.1:8888` only.
- [`settings.yml`](docker/searxng/settings.yml) — JSON output enabled (required by web_agent), default engines, internal limiter disabled (web_agent rate-limits on its own).

Three steps from a fresh clone:

```bash
# 1. Generate a per-deployment secret key:
python -c "import secrets; print(secrets.token_hex(32))"
# ... paste the output into docker/searxng/settings.yml in place of
# the REPLACE_WITH_RANDOM_STRING placeholder.

# 2. Start the container:
docker compose -f docker/searxng/docker-compose.yml up -d

# 3. Smoke-test the JSON endpoint web_agent uses:
curl 'http://localhost:8888/search?q=python&format=json' | head -c 500
```

Then point web_agent at it:

```python
from web_agent import Agent, AppConfig

config = AppConfig(search={
    "providers": ["searxng", "ddgs", "playwright"],
    "searxng_base_url": "http://localhost:8888",
})
async with Agent(config) as agent:
    result = await agent.search_and_extract("python web scraping")
    # First provider in the chain (SearXNG) handles it -- no browser
    # launch, no DDGS network, sub-second latency.
```

To stop and clean up:

```bash
docker compose -f docker/searxng/docker-compose.yml down
```

**Production / shared use**: re-enable the SearXNG-level rate limiter (`server.limiter: true` in `settings.yml`), put it behind a reverse proxy with auth + TLS, and consider a Redis backend (see the [SearXNG admin docs](https://docs.searxng.org/admin/settings/settings_server.html)).

## Browser Sessions

Persistent browser sessions retain cookies, localStorage, and origin tokens across multiple Agent calls -- ideal for login flows, multi-step workflows, or any task that needs continuity.

```python
async with Agent() as agent:
    # Step 1: log in once
    sid = await agent.create_session(name="my-app")
    await agent.interact("https://app.example.com/login", [
        FillInput(selector="#user", value="me"),
        FillInput(selector="#pass", value="secret"),
        ClickInput(selector="button[type=submit]"),
    ], session_id=sid)

    # Step 2: subsequent calls reuse cookies
    dashboard = await agent.fetch_and_extract(
        "https://app.example.com/dashboard", session_id=sid
    )
    report = await agent.download(
        "https://app.example.com/reports/q4.pdf", session_id=sid
    )

    await agent.close_session(sid)
```

In MCP, the equivalent flow is `web_create_session` -> `web_interact(...session_id=sid)` -> `web_fetch(...session_id=sid)` -> `web_close_session` (all v1.7.0).

### Auth persistence — log in once, automate later (v1.7.0)

When login requires a human (password + 2FA + CAPTCHA), do it once interactively, then export the session's `storage_state` (cookies + origins) to a JSON file and rehydrate it in a later, fully-automated process:

```python
# Process 1 (human-assisted login):
sid = await agent.create_session(name="my-app")
# ... human logs in (headed handoff, or a one-time interactive run) ...
await agent.export_session_state(sid, "my-app.state.json")   # confined to download_dir

# Process 2 (unattended, days later):
sid = await agent.import_session_state("my-app.state.json", name="my-app")
dashboard = await agent.fetch_and_extract("https://app.example.com/dashboard", session_id=sid)
```

The state file is confined to `download.download_dir` via `safe_join_path` (traversal / absolute / UNC paths rejected) and import refuses files over 8 MB. Cookies rehydrate fully; per-origin `localStorage` is best-effort. The MCP equivalents are `web_export_session` / `web_import_session`.

## Safety Controls

`SafetyConfig` provides multiple defense layers: domain allow/deny lists, granular feature flags, SSRF protection, and per-call budgets. Most defaults are secure-out-of-the-box (`block_private_ips=True`, `allow_js_evaluation=False`).

```python
from web_agent import Agent, AppConfig

config = AppConfig(safety={
    "allowed_domains": ["wikipedia.org", "arxiv.org"],
    "denied_domains": ["malicious.example.com"],
    "safe_mode": False,                   # master kill-switch
    "allow_js_evaluation": False,         # gates EvaluateInput (default False)
    "allow_downloads": True,              # gates file downloads
    "allow_form_submit": True,            # gates submit-button clicks
    "block_private_ips": True,            # SSRF protection (RFC1918 + IMDS)
    "max_pages_per_call": 10,
    "max_chars_per_call": 500_000,
    "max_time_per_call_seconds": 60.0,
})
async with Agent(config) as agent:
    ...
```

| Field | Default | Effect |
|---|---|---|
| `allowed_domains` | `[]` (allow all) | Suffix-match patterns; empty allows everything |
| `denied_domains` | `[]` | Always blocked, takes precedence over allow-list |
| `safe_mode` | `false` | Master kill-switch: forces all 3 `allow_*` to False |
| `allow_js_evaluation` | **`false`** | Gates `EvaluateInput` (LLM-supplied JS). Opt in explicitly. |
| `allow_downloads` | `true` | Gates file downloads via `agent.download()` |
| `allow_form_submit` | `true` | Gates clicks on submit-typed buttons (heuristic match) |
| `block_private_ips` | `true` | Blocks RFC1918, loopback, link-local (incl. AWS IMDS at 169.254.169.254) |
| `max_pages_per_call` | `50` | Stops fetching after N pages |
| `max_chars_per_call` | `1_000_000` | Stops extracting after total chars exceeded |
| `max_time_per_call_seconds` | `300` | Wall-clock cutoff per Agent call |
| `rate_limit_per_host_rps` | `2.0` | Per-host requests/second cap. Set to `0` to disable. |
| `respect_robots_txt` | `true` | Fetch and obey each host's robots.txt before requesting pages |
| `robots_user_agent` | `"web-agent-toolkit"` | UA token sent to robots.txt and matched against rule groups |

**Path traversal protection**: `Downloader.download(filename=...)` and `ScreenshotInput.path` reject `..` traversal and absolute paths. Filenames must resolve inside the configured `download_dir` / `screenshot_dir`.

**SSRF protection**: When `block_private_ips=True` (default), the toolkit blocks fetches/downloads to RFC1918 ranges, loopback, and link-local addresses. The `Downloader` re-validates every HTTP redirect target so a whitelisted host cannot bounce you to AWS IMDS or an internal-only URL.

**Politeness layer**: `RateLimiter` enforces a per-host minimum interval between requests so parallel fetches against a single host don't trip server-side throttles. `RobotsChecker` fetches each host's `robots.txt` once, caches it for an hour, and short-circuits any URL the rules disallow for our user-agent. Disallowed URLs return `FetchStatus.BLOCKED` with `error_message="robots.txt for {host} disallows ..."`.

Blocked URLs return `FetchStatus.BLOCKED` with a clear `error_message`. Budget exhaustion raises `BudgetExceededError` (caught and added to `errors[]` in `AgentResult`).

### Audit Log

`AuditConfig` enables an append-only JSONL log of every public Agent operation. Distinct from regular logging: structured (one JSON object per line), records only public method calls (start + end), and survives restarts.

```python
from web_agent import AppConfig, Agent

config = AppConfig(audit={"enabled": True, "audit_log_path": "./audit.jsonl"})
async with Agent(config) as agent:
    await agent.fetch_and_extract("https://example.com")
# audit.jsonl now contains one line:
# {"timestamp": "...", "correlation_id": "...", "method": "fetch_and_extract",
#  "args": {"url": "https://example.com"}, "status": "success", "elapsed_ms": 432.1}
```

Failures are logged with `"status": "error"` and `"error": "<repr(exc)>"`. The `correlation_id` field cross-references the entry with regular loguru logs.

### Cache (NEW in 1.5.0)

`CacheConfig` enables a disk-backed TTL cache for fetch results and search responses. Disabled by default. When enabled, every successful `agent.fetch_and_extract(url)` and `agent.search_and_extract(query)` writes its result to disk; subsequent calls within `ttl_seconds` short-circuit and return the cached payload (`from_cache=True`) without hitting the network.

```python
from web_agent import Agent, AppConfig

config = AppConfig(cache={
    "enabled": True,
    "cache_dir": "./cache",
    "ttl_seconds": 3600,    # 1 hour
    "max_cache_mb": 100,    # LRU-by-mtime eviction past this
})
async with Agent(config) as agent:
    page1 = await agent.fetch_and_extract("https://example.com")  # network
    page2 = await agent.fetch_and_extract("https://example.com")  # cache hit
    # page2.url == page1.url and the underlying FetchResult.from_cache == True
```

Cache keys: URL for fetches, `"search:<query>:<max_results>"` for searches. Only successful fetches and non-empty search responses are cached -- caching errors/empty would lock in transient failures across the TTL window.

Ordering: `robots.txt` check runs **before** the cache lookup so a host changing its `robots.txt` to disallow a path takes effect immediately, even for URLs we cached under more permissive rules. The robots check itself is cached per-host (1h TTL inside `RobotsChecker`), so the practical overhead on cache hits is near-zero. Cache hit -> skip rate-limit + network.

To clear the cache mid-run: `await agent._cache.clear()` (returns count removed). To extend the cache backend (e.g. Redis), implement the `Cache` ABC in `web_agent/cache.py` and pass it directly to `WebFetcher` / `SearchEngine`.

### Markdown Extraction (NEW in 1.5.0)

`ExtractionResult.markdown` is a markdown rendering of the page, populated automatically whenever `trafilatura` is the winning extractor (the most common path). Markdown preserves headings, lists, links, and emphasis, which most LLMs prefer to consume over plain text.

```python
async with Agent() as agent:
    page = await agent.fetch_and_extract("https://example.com")
    print(page.content)    # plain text, like before
    print(page.markdown)   # markdown rendering -- preserves structure
```

`markdown` stays `None` when the bs4 or raw-text fallback layers win (those layers don't have a markdown equivalent). The double-pass through trafilatura is cheap -- HTML is parsed twice, no extra network.

### Strict Mode

By default, all `Agent` methods return result models even on failure. Pass `strict=True` to convert failures into typed exceptions:

```python
from web_agent.exceptions import NavigationError, SearchError, DownloadError

async with Agent() as agent:
    try:
        page = await agent.fetch_and_extract(url, strict=True)
    except NavigationError as e:
        print(f"Failed: {e} (status={e.status_code})")

    try:
        result = await agent.search_and_extract(query, strict=True)
    except SearchError as e:
        print(f"Search provider chain exhausted: {e}")
```

## High-Level Recipes

Four composite workflows AI agents can call directly without orchestrating primitives:

```python
# Recipe 1: search + rank + fetch top hit
result = await agent.search_and_open_best_result(
    "FastAPI tutorial 2024",
    prefer_domains=["fastapi.tiangolo.com"],   # NEW in 1.6.1
)
print(result.title, result.content[:500])

# Recipe 2: search + locate file + download
dl = await agent.find_and_download_file(
    "Tesla 10-K annual report 2024", file_types=["pdf"]
)
print(f"Saved: {dl.filepath} ({dl.size_bytes} bytes)")

# Recipe 3: multi-page research with citations
research = await agent.web_research(
    "vector databases comparison",
    max_pages=5,
    prefer_domains=["arxiv.org", "github.com"],   # NEW in 1.6.1
)
for c in research.citations:
    print(f"[{c.relevance_score:.2f}] {c.title} -- {c.url}")

# Recipe 4 (NEW in 1.6.1): fill a search/filter form, then extract
from web_agent import FormFilterSpec, LocatorSpec

result = await agent.fill_form_and_extract(
    "https://www.esma.europa.eu/document-search",
    FormFilterSpec(
        query_selector="input[name=keywords]",
        query_value="MiFID II",
        filters=[
            ("select#year", "2024"),
            (LocatorSpec(role="combobox", role_name="Document Type"), "Q&A"),
        ],
        submit_selector=LocatorSpec(role="button", role_name="Search"),
        wait_for=".search-results",
    ),
)
print(result.content[:500])
```

`search_and_open_best_result` ranking schemes:
- `default` -- query overlap + HTTPS bonus + well-known domain bonus + `prefer_domains` bonus + position tiebreaker
- `overlap` -- pure token overlap
- `position` -- inverse search engine rank

## Failure-Surface Diagnostics

When `search_and_extract` or `web_research` runs against the open
web, individual URLs can fail for many reasons (domain blocked,
robots disallow, timeout, HTTP 4xx, file URL skipped). v1.6.1 makes
these failures programmatically inspectable.

```python
result = await agent.search_and_extract("Tesla 10-K 2024")

# Fatal vs non-fatal split
if result.errors:
    raise RuntimeError(f"Search call failed: {result.errors}")
for w in result.warnings:
    log.info("Non-fatal: %s", w)

# Structured download candidates (PDFs, XLSX, etc. that were skipped)
for cand in result.download_candidates:
    log.info("File URL skipped: %s (%s)", cand.url, cand.title)
    # Programmatically retry via download:
    # dl = await agent.download(cand.url)

# Per-URL diagnostics
for d in result.diagnostics:
    log.info(
        "%s -> %s [%s] block=%s len=%d cache=%s",
        d.url, d.status, d.provider, d.block_reason,
        d.content_length, d.from_cache,
    )
```

Want PDF/XLSX text inline in `pages` instead of just in
`download_candidates`?

```bash
pip install "web-agent-toolkit[binary]"   # adds pypdf + openpyxl
```

```python
result = await agent.search_and_extract("Tesla 10-K 2024", extract_files=True)
# PDFs now appear in result.pages with extraction_method="pdf"
```

If the `[binary]` extra is missing, the call still succeeds but PDFs
land as `extraction_method="none"` with a clear log warning telling
you which package to install.

## Semantic Locators

Beyond CSS selectors, browser automation actions accept `LocatorSpec` for AI-friendly element targeting:

```python
from web_agent.models import ClickInput, FillInput, LocatorSpec

# All three are equivalent ways to target the same element:
ClickInput(selector="button.submit-btn")                          # CSS
ClickInput(selector=LocatorSpec(role="button", role_name="Submit"))  # ARIA role
ClickInput(selector=LocatorSpec(text="Submit"))                   # visible text

# More options:
FillInput(selector=LocatorSpec(label="Email"), value="me@example.com")
FillInput(selector=LocatorSpec(placeholder="Search..."), value="query")
ClickInput(selector=LocatorSpec(test_id="login-button"))
```

In JSON (and MCP `web_interact`), pass either a string or a `LocatorSpec` object:

```json
{"action": "click", "selector": {"role": "button", "role_name": "Submit"}}
```

Resolution priority: `role` > `test_id` > `label` > `placeholder` > `text` > `selector`.

## Retry Policies

Declarative retry profiles via `FetchConfig.retry_policy`:

| Policy | Retries | Base | Max | Use case |
|---|---|---|---|---|
| `fast` | 1 | 0.5s | 5s | Latency-sensitive flows; quick failure preferred |
| `balanced` (default) | 3 | 1s | 30s | General-purpose |
| `paranoid` | 5 | 2s | 60s | Flaky targets where eventual success matters |

```python
config = AppConfig(fetch={"retry_policy": "paranoid"})
```

Numeric overrides win over the policy: `AppConfig(fetch={"retry_policy": "fast", "max_retries": 7})` keeps `fast`'s base/max delays but uses 7 retries.

## Debug Mode

When enabled, every fetch/action/download failure auto-captures HTML, a screenshot, and an error JSON to `debug_dir/{correlation_id}/{timestamp}-{label}.{ext}`:

```python
config = AppConfig(debug={"enabled": True, "debug_dir": "/tmp/web_agent_debug"})
async with Agent(config) as agent:
    result = await agent.fetch_and_extract("https://flaky-site.example.com")
    if result.extraction_method == "none":
        print("Failed; debug artifacts:", result.debug_artifacts)
```

Result models gain a `debug_artifacts: list[str]` field with the saved file paths.

## Correlation IDs

Every public Agent method generates a UUID4 correlation id. The id is:

- Echoed back on every result model (`result.correlation_id`)
- Auto-injected into every `loguru` log record's `extra["cid"]` field
- Carried through retries, fetches, extractions, and recipe sub-calls

To use it in your own log format:

```python
from loguru import logger
import sys
logger.remove()
logger.add(sys.stderr, format="{time} | {extra[cid]} | {message}")
```

## MCP Integration

Run `web_agent` as an MCP (Model Context Protocol) server so Claude Desktop, Claude Code, Cursor, OpenAI Codex, OpenClaw, and any other MCP-compatible AI client can use it directly as a tool. The browser stays warm across tool calls within a session (skips ~5-10s startup per call after the first).

### Starting the server

```bash
# Via module:
python -m web_agent.mcp_server

# Via installed script:
web-agent-mcp

# Via CLI subcommand:
python -m web_agent serve-mcp
```

The server uses stdio transport -- it's invoked by the MCP client, not run standalone.

### Exposed Tools (44 total)

**Single-shot pipeline tools** — one URL or query, one result:

| Tool | Description |
|------|-------------|
| `web_search` | Search the web and extract content from top results |
| `web_search_links` | Links-only search (no fetch/extract); reports `search_blocked` (v1.7.0) |
| `web_fetch` | Fetch a single URL and extract main content |
| `web_download` | Download a file or save a web page |
| `web_screenshot` | Take a screenshot of a page |
| `web_interact` | Execute a browser action sequence |
| `web_fill_form_and_extract` | Fill a form (`FormFilterSpec`) and extract the result page |

Content-returning tools (`web_fetch`, `web_search`, `web_search_best`, `web_research`, `web_fill_form_and_extract`) accept per-call `max_chars` / `offset` / `format` and return **one** representation (markdown default) capped at `extraction.default_max_chars` (v1.7.0).

**High-level recipes** — composite workflows:

| Tool | Description |
|------|-------------|
| `web_search_best` | Search, rank, return extracted top hit |
| `web_find_and_download` | Search + download first matching file |
| `web_research` | Multi-page research with citations |
| `web_collect_pages` | Walk a paginated/infinite-scroll listing and assemble content across pages (v1.7.0) |

**Browser session management** (cookies / login continuity; create/export/import new in v1.7.0):

| Tool | Description |
|------|-------------|
| `web_create_session` | Create a persistent session (v1.7.0) |
| `web_list_sessions` | List all live sessions (v1.7.0) |
| `web_close_session` | Close a session, free its context (v1.7.0) |
| `web_export_session` | Export a logged-in `storage_state` to JSON (v1.7.0) |
| `web_import_session` | Rehydrate a session from an exported `storage_state` (v1.7.0) |

**Tab management** (v1.6.6):

| Tool | Description |
|------|-------------|
| `web_list_tabs` | List tabs in a session |
| `web_current_tab` | Get the session's current tab |
| `web_new_tab` | Open a new tab in the session |
| `web_switch_tab` | Switch the current tab |
| `web_close_tab` | Close a tab |

**Coordinate-level fallbacks** (v1.6.6, for canvas / shadow / iframe):

| Tool | Description |
|------|-------------|
| `web_click_xy` | Click at CSS-pixel coordinates |
| `web_type_text` | Type via keyboard at the current focus |
| `web_press_key` | Press a single key combo (e.g. `Shift+Enter`) |
| `web_scroll_to_bottom` | Scroll a session tab to exhaustion to load lazy/infinite-scroll content (v1.7.0) |

**Observe + diagnostics** (v1.6.6 + v1.6.8):

| Tool | Description |
|------|-------------|
| `web_observe` | Screenshot + viewport + page-size + DPR + optional ARIA snapshot; also returns a numbered list of interactive `elements` for the act-by-ref loop (`{"ref":"e3"}` as a selector, v1.7.0) |
| `web_doctor` | Run 19 capability probes; returns `DoctorReport` |
| `web_metrics` | Snapshot in-process counters + distributions (`MetricsSnapshot`, v1.7.0) |
| `web_get_cdp_endpoint` | Return CDP ws:// URL when `cdp_enabled=True` |
| `web_get_owned_cdp_connection_info` | Full attach bundle (`cdp_url` + `profile_dir` + `ownership_token`) for a sibling `remote_cdp` Agent |
| `web_get_remote_cdp_url` | Return ws:// URL when `backend="remote_cdp"` |
| `web_list_traces` | Session-ids of replay traces under `diagnostics.trace_dir` |
| `web_replay_trace` | Re-execute the action list in a trace JSONL |

**Domain skills** (v1.6.7):

| Tool | Description |
|------|-------------|
| `list_domain_skills` | All registered skills (builtin + workspace + project) |
| `get_domain_skill` | Single skill by `(url, name)` |
| `apply_domain_skill` | Run a runnable skill end-to-end |

**Interaction-skill library** (v1.6.7):

| Tool | Description |
|------|-------------|
| `web_handle_dialog` | Accept / dismiss browser dialogs |
| `web_select_dropdown` | Select `<option>` by value / label / index |
| `web_upload_file` | Upload one or more files |
| `web_drag_and_drop` | Drag from source selector to target selector |
| `web_scroll_until_text` | Scroll until visible text appears or attempts exhaust |
| `web_click_inside_iframe` | Click inside a frame located by selector |
| `web_click_shadow_dom` | Pierce shadow DOM via `host >> inner` |
| `web_print_page_as_pdf` | Render the current page as PDF |

All tools return structured Pydantic models that auto-serialize to JSON for the client. Every tool accepts an optional `session_id` to reuse a persistent browser context for cookie / login continuity.

### Claude Desktop Setup

Edit `claude_desktop_config.json`:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%AppData%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

Add:

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "python",
      "args": ["-m", "web_agent.mcp_server"]
    }
  }
}
```

Restart Claude Desktop. All 44 tools should appear in the tool picker.

### Claude Code Setup

```bash
claude mcp add web_agent -- python -m web_agent.mcp_server
```

Or manually edit `~/.claude.json` (or your project's `.mcp.json`):

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "python",
      "args": ["-m", "web_agent.mcp_server"]
    }
  }
}
```

### Cursor Setup

Edit `~/.cursor/mcp.json` (or project-local `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "python",
      "args": ["-m", "web_agent.mcp_server"]
    }
  }
}
```

### Codex Setup

OpenAI's [Codex CLI](https://developers.openai.com/codex/cli) reads MCP servers from `~/.codex/config.toml`. Add:

```toml
[mcp_servers.web_agent]
command = "python"
args = ["-m", "web_agent.mcp_server"]
```

Or scope to a single project via `.codex/config.toml` (only honored in trusted project dirs). For environment-variable overrides:

```toml
[mcp_servers.web_agent]
command = "python"
args = ["-m", "web_agent.mcp_server"]

[mcp_servers.web_agent.env]
WEB_AGENT_LOG_LEVEL = "INFO"
WEB_AGENT_SEARCH__SEARXNG_BASE_URL = "http://localhost:8888"
WEB_AGENT_CACHE__ENABLED = "true"
```

You can manage entries from the CLI with `codex mcp add` / `codex mcp list`. The IDE extension (VS Code / JetBrains Codex plugin) shares this config -- no duplicate setup needed.

### Custom Config via Environment Variables

All `AppConfig` fields are overridable via env vars in the MCP config:

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "python",
      "args": ["-m", "web_agent.mcp_server"],
      "env": {
        "WEB_AGENT_LOG_LEVEL": "INFO",
        "WEB_AGENT_BROWSER__HEADLESS": "true",
        "WEB_AGENT_SEARCH__MAX_RESULTS": "5",
        "WEB_AGENT_DOWNLOAD__DOWNLOAD_DIR": "/tmp/web_agent_downloads"
      }
    }
  }
}
```

See [sample_data/mcp_config_example.json](sample_data/mcp_config_example.json) for a complete example.

### Testing the Server

Use the MCP inspector (bundled with the `mcp` CLI):

```bash
mcp dev web_agent/mcp_server.py
```

This launches a web UI where you can invoke each tool with test inputs and see the results.

## Using web_agent as a Backend for Local Agents

`web_agent` is designed to slot under autonomous agents that run on user hardware (no API keys, no cloud calls). Two integration shapes work for almost any framework:

### OpenClaw Integration

[OpenClaw](https://github.com/openclaw/openclaw) is a local autonomous AI agent (the runtime, not a framework). It needs a web backend to actually browse / search / scrape, and `web_agent` is a clean fit: free-first search chain (SearXNG -> DDGS -> Playwright), built-in safety policy (rate limit + robots.txt + SSRF protection + domain allow/deny), and structured Pydantic output that matches OpenClaw's tool-result schema.

**Path A: As an MCP server** (recommended -- browser stays warm across calls, zero glue code):

```toml
# In OpenClaw's MCP server registry (consult your OpenClaw deployment
# for the exact path -- typically ~/.openclaw/config.toml or similar)
[[mcp_servers]]
name = "web_agent"
command = "python"
args = ["-m", "web_agent.mcp_server"]

[mcp_servers.env]
# Tighter policy for an always-on autonomous agent:
WEB_AGENT_SAFETY__RATE_LIMIT_PER_HOST_RPS = "1.0"
WEB_AGENT_SAFETY__RESPECT_ROBOTS_TXT = "true"
WEB_AGENT_AUDIT__ENABLED = "true"
WEB_AGENT_AUDIT__AUDIT_LOG_PATH = "/var/log/openclaw/web_agent.audit.jsonl"
WEB_AGENT_CACHE__ENABLED = "true"
WEB_AGENT_CACHE__CACHE_DIR = "/var/cache/openclaw/web_agent"
```

OpenClaw will auto-discover all 44 tools (`web_search` / `web_search_links`, `web_fetch`, `web_download`, `web_screenshot`, `web_interact`, the recipes including `web_fill_form_and_extract` and `web_collect_pages`, the 5 session-management tools, `web_metrics`, plus the tab / coordinate / scroll / observe / domain-skill / interaction-library surfaces).

**Path B: As a Python library** (for OpenClaw skills / custom hooks where you need fine control):

```python
from web_agent import Agent, AppConfig

# Production-ready config for an unattended agent:
config = AppConfig(
    search={
        "providers": ["searxng", "ddgs", "playwright"],
        "searxng_base_url": "http://localhost:8888",  # see docker/searxng/
    },
    safety={
        "respect_robots_txt": True,
        "rate_limit_per_host_rps": 1.0,    # be a good citizen 24/7
        "block_private_ips": True,         # SSRF defense
        "denied_domains": ["facebook.com", "tiktok.com"],
    },
    cache={"enabled": True, "ttl_seconds": 3600},
    audit={"enabled": True, "audit_log_path": "./openclaw_web.jsonl"},
)

async with Agent(config) as agent:
    # Tool intent: "research"
    research = await agent.web_research("vector databases comparison", max_pages=5)

    # Tool intent: "open this URL"
    page = await agent.fetch_and_extract(url)
    print(page.markdown)  # LLM-friendly markdown rendering

    # Tool intent: "fetch this report"
    dl = await agent.download(url, filename="q4-report.pdf")
```

Pair `web_agent.safety` with OpenClaw's policy layer -- the granular flags (`allow_js_evaluation`, `allow_downloads`, `allow_form_submit`) and the audit log give OpenClaw a tamper-evident record of every web operation the agent took, which is invaluable when running unattended.

### Generic LangGraph / LangChain Integration

The Python-library path above also works inside any LangGraph node or LangChain tool:

```python
from langchain_core.tools import tool
from web_agent import Agent, AppConfig

# Agent is created once per graph; reuse across nodes via dependency injection
_agent: Agent | None = None

async def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = await Agent(AppConfig(cache={"enabled": True})).__aenter__()
    return _agent

@tool
async def web_search_and_extract(query: str, max_results: int = 5) -> dict:
    """Search the web and extract content from the top results."""
    agent = await get_agent()
    result = await agent.search_and_extract(query, max_results=max_results)
    return result.model_dump()

@tool
async def web_fetch(url: str) -> dict:
    """Fetch and extract a single URL."""
    agent = await get_agent()
    result = await agent.fetch_and_extract(url)
    return result.model_dump()
```

Pydantic v2 result models serialize cleanly to dicts, so they round-trip through LangGraph's state without custom encoders.

## Error Handling

All exceptions inherit from `WebAgentError`:

```python
from web_agent.exceptions import NavigationError, DownloadError, WebAgentError

async with Agent() as agent:
    try:
        result = await agent.fetch_and_extract(url)
    except NavigationError as e:
        print(f"Page failed: {e} (status={e.status_code})")
    except WebAgentError as e:
        print(f"Agent error: {e}")
```

Exception hierarchy:

```
WebAgentError
  |-- BrowserError              Browser launch/context failures
  |-- NavigationError           Page load, timeout, blocked
  |-- ExtractionError           Content extraction failures
  |-- SearchError               Search engine failures
  |-- DownloadError             File download failures
  |-- ActionError               Browser action failures
  |     |-- ActionTimeoutError
  |     `-- SelectorNotFoundError
  |-- DomainNotAllowedError     URL host not in allow-list / matches deny-list
  |-- BudgetExceededError       Per-call budget (pages/chars/time) hit
  |-- SafeModeBlockedError      Operation forbidden by safe_mode
  `-- ConfigError               Configuration validation failures
```

## Docker

A production container ships under [`docker/`](docker/) — `docker/Dockerfile` is built on the official Playwright Python base image (Chromium + system deps + fonts baked in), runs as the **non-root `pwuser`**, and has a `HEALTHCHECK` (`web-agent doctor --quick`) plus an `ENTRYPOINT` that launches the MCP server. The full guide (compose, config mounts, the sandbox trade-off) is in [`docker/README.md`](docker/README.md); the self-hosted SearXNG quickstart under [`docker/searxng/`](docker/searxng/) is separate and unaffected.

Build the image:

```bash
docker build -f docker/Dockerfile -t web-agent-toolkit:latest .
```

Sanity-check it, run the CLI, or start the MCP server (stdio):

```bash
# Health check (the same probe the HEALTHCHECK uses)
docker run --rm web-agent-toolkit:latest doctor --quick

# One-shot CLI search
docker run --rm web-agent-toolkit:latest search "example query"

# MCP server over stdio, with a persistent workspace volume
docker run -i -v web-agent-workspace:/workspace web-agent-toolkit:latest
```

Wire it into an MCP client (`claude_desktop_config.json` etc.):

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-v", "web-agent-workspace:/workspace", "web-agent-toolkit:latest"]
    }
  }
}
```

**Non-root + the Chromium-sandbox trade-off.** Because the container runs as `pwuser` (not root), the Chromium sandbox is auto-disabled in-container (`--no-sandbox`). To keep the sandbox on, run with `--security-opt seccomp=unconfined` / `--cap-add=SYS_ADMIN` and set `WEB_AGENT_BROWSER__DISABLE_CHROMIUM_SANDBOX=false`. `WEB_AGENT_BASE_DIR=/workspace` (a `VOLUME`) holds downloads / screenshots / traces; the `HEALTHCHECK` keeps an orchestrator honest. See [`docker/README.md`](docker/README.md) for the honest stdio-vs-service note.

## Testing

```bash
# Full suite (~583 tests on Windows + 5 platform-conditional skips on Linux)
python -m pytest -v

# Unit tests only (no network) -- the CI invocation
python -m pytest \
  --ignore=tests/test_agent.py --ignore=tests/test_browser_actions.py -v

# Integration tests (requires network + Chromium) under the `integration` marker
python -m pytest -v -m integration
```

CI runs `ruff check`, `ruff format --check`, `mypy`, and the unit-test job on Python 3.10 / 3.12 / 3.13. The integration job runs Playwright + network tests on push-to-main and on a nightly schedule (it's `continue-on-error: true` so transient CAPTCHAs on free search providers don't block legitimate merges).

## Project Structure

```
web_agent/                       # 33 modules, mypy strict-clean
  __init__.py                    # v1.7.0 -- 130 public exports
  py.typed                       # PEP 561 marker
  exceptions.py                  # WebAgentError hierarchy
  config.py                      # AppConfig + 15 sub-configs incl. ProxyConfig + MetricsConfig (programmatic / env / YAML)
  models.py                      # 40+ Pydantic v2 models (single source of wire shape)
  utils.py                       # async_retry, safe_join_path, is_private_address, BudgetTracker
  correlation.py                 # ContextVar correlation IDs + loguru patcher (surfaced as {extra[cid]}, v1.7.0)
  metrics.py                     # In-process MetricsRegistry: counters + distributions, label-cardinality cap (v1.7.0)
  debug.py                       # DebugCapture: HTML + screenshot + JSON on failure
  audit.py                       # Append-only JSONL audit log of every public Agent call (opt-in)
  cache.py                       # Disk-backed TTL cache for fetch + search (opt-in)
  rate_limiter.py                # Per-host async token-bucket gate
  robots.py                      # robots.txt fetcher + TTL cache
  agent.py                       # Public Agent orchestrator (entry point)
  browser_manager.py             # Chromium lifecycle + per-context stealth + crash auto-relaunch + 3 backends (playwright | cdp_owned | remote_cdp)
  challenge.py                   # Bot-wall / CAPTCHA detection -> ChallengeInfo (v1.7.0)
  injection.py                   # Untrusted-content containment: hidden-DOM/Unicode strip + InjectionReport (v1.7.0)
  browser_actions.py             # 19 action handlers + per-action verify screenshot + trace recording + scroll_to_bottom (v1.7.0)
  session_manager.py             # Persistent named BrowserContext sessions + storage_state export/import + idle reaper (v1.7.0)
  tab_manager.py                 # Per-session tab lifecycle + popup auto-register (v1.6.6)
  doctor.py                      # 19 capability probes + DoctorReport (v1.6.6)
  domain_skills.py               # Skill registry + dispatcher (v1.6.7)
  workspace.py                   # Agent-editable workspace with 4 safety modes (v1.6.7)
  builtin_skills/                # 3 bundled skills: sec.gov / github.com / ec.europa.eu (v1.6.7)
  network_collector.py           # Per-Page request/response/download event collector (v1.6.8)
  trace_recorder.py              # Per-session JSONL action traces for replay (v1.6.8)
  search_engine.py               # Multi-provider search chain + per-provider circuit breaker (v1.7.0)
  search_providers.py            # SearchProvider ABC + SearXNG / DDGS / Playwright impls
  web_fetcher.py                 # Page + binary fetch with retry, safety, debug, sessions, cache
  content_extractor.py           # trafilatura -> BS4 -> raw; PDF (pdfplumber -> pypdf) + tables / XLSX / DOCX / CSV via [binary] (v1.7.0)
  downloader.py                  # Three-strategy file/page download with safety + sessions
  recipes.py                     # search_and_open_best, find_and_download, web_research, fill_form_and_extract, collect_across_pages (v1.7.0)
  mcp_server.py                  # FastMCP server -- 44 tools (honours log_level + correlation id, v1.7.0)
  main.py                        # CLI: search / fetch / download / interact / screenshot / observe / skills / doctor / replay
docker/                          # Non-root Playwright-based image: Dockerfile + docker-compose.yml + README.md + .dockerignore (v1.7.0)
docker/searxng/                  # Self-hosted SearXNG quickstart (compose + tuned settings)
tests/                           # 59 test files; mirrors the package layout (28 integration-marked, opt-in)
config.example.yaml              # Reference configuration (annotated)
sample_data/                     # Test fixtures and example action sequences
```

## License

Apache-2.0 license
