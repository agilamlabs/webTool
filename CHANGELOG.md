# Changelog

## [1.6.10] - 2026-05-18

### Review pass (3 fixes: 2 Critical + 1 Important)

- **C-1**: `SafetyConfig.coordinate_click_unknown_policy="block"` had
  no effect when `allow_form_submit=True` (the default). The
  unknown-policy gate was nested inside the destructive-check block,
  so callers keeping the default form-submit policy and explicitly
  opting into block-on-unknown saw their setting silently ignored.
  Fix: hoist `_inspect_element_at_point` so the destructive check
  fires whenever `allow_form_submit=False` AND the unknown-policy
  check fires whenever `coordinate_click_unknown_policy="block"`
  (independently). Added a regression test
  (`TestV1610Integration::test_click_xy_unknown_policy_block_fires_when_form_submit_allowed`).
- **C-2**: The CHANGELOG migration note for `classify_url` referenced
  `_is_binary_kind` (leading underscore) but the helper was not
  exported from the `web_agent.*` public namespace, breaking the
  migration story. Fix: renamed to `is_binary_kind` (no underscore)
  and exported from `web_agent`. Now callers can migrate via
  `from web_agent import is_binary_kind`. The function docstring
  says "Public-stable as of v1.6.10"; the symbol name now matches.
- **I-1**: `web_research(extract_files=True)` silently appended a
  contentless `Citation` and zero-cost `summary_pages` entry when
  `fetch_smart` returned a successful binary `FetchResult` of an
  unrecognized kind (PPTX, ZIP, octet-stream). Fix: add a
  `binary_not_extracted` warning + diagnostic and skip the result
  when `fr.binary is not None` AND
  `extraction_method == "none" AND content_length == 0`.

### Follow-up Hardening (8 items, no new features)

v1.6.10 is another **discipline slice** -- the v1.6.9 release surfaced
one real functional bug plus seven consistency/UX gaps in a follow-up
review. All eight items are correctness, consistency, or UX hardening;
no new features. Test count grows ~734 -> ~739 (5 new integration tests
in `tests/test_agent.py::TestV1610Integration` plus 2 unit-level
additions in `tests/test_v169_smart_binary_routing.py`).

### Breaking change (direct callers of `WebFetcher.classify_url`)

`WebFetcher.classify_url` and the underlying `_url_ext_classification`
helper no longer return the string `"binary"`. They now return one of:

- `"pdf" | "xlsx" | "docx" | "csv" | "zip" | "binary_other" | "html" | "unknown"`

**Migration:** replace `classification == "binary"` with the new
`_is_binary_kind(classification)` helper exported from
`web_agent.web_fetcher`. Callers that only consume `fetch_smart` or
public `Agent` methods are unaffected -- the routing decision is
already centralized inside `fetch_smart` and uses the helper.

### Must-fix (functional correctness)

1. **`web_research` no longer drops successful binary results.** The
   v1.6.9 `web_research` recipe routed search-result URLs through
   `fetch_smart` (good) but then gated on `not fr.html` (bad) -- so a
   successful binary `FetchResult` from `fetch_smart` (extensionless
   PDF, regulator dashboard, etc.) was logged as `fetch_failed` and
   silently dropped. v1.6.10 fixes the gate to
   `not (fr.html or fr.binary)`, aligning with the equivalent gate in
   `Agent.search_and_extract`. The `ContentExtractor.extract` callsite
   already dispatches on `fr.binary` vs `fr.html`, so no other change
   was needed in the loop.

2. **`web_research(extract_files=False)` parameter.** Mirrors the
   existing `search_and_extract(extract_files=False)` knob. Default
   `False` preserves the v1.6.9 read-pages-only behaviour (file URLs
   land in `download_candidates`). When `True`, file URLs are routed
   through `fetch_smart` + the binary extractor inline so PDF/XLSX/DOCX/
   CSV results join the `summary_pages` list with citations. Propagated
   through `Agent.web_research` and the `web_research` MCP tool.

3. **Richer file classification.** `_url_ext_classification` and
   `WebFetcher.classify_url` now distinguish PDF / XLSX / DOCX / CSV /
   ZIP / `binary_other` instead of collapsing every binary to
   `"binary"`. `find_and_download_file(file_types=["pdf"])` now
   rejects an extensionless XLSX or ZIP matched via HEAD probe -- the
   v1.6.9 behaviour accepted any binary content type regardless of the
   requested kinds. New module-level helper `_is_binary_kind(s)` is the
   canonical migration target for any code that compared to the old
   `"binary"` string.

### Should-fix (consistency / UX)

4. **`SafetyConfig.coordinate_click_unknown_policy: Literal["allow", "block"] = "allow"`.**
   When `"block"`, `click_xy` rejects clicks where
   `document.elementFromPoint(x, y)` returns no element (point outside
   any element, or the JS evaluation raised). Only fires when
   `allow_coordinate_clicks=True` AND `allow_form_submit=False` --
   `safe_mode` already blocks all coordinate clicks at the
   `allow_coordinate_clicks=False` short-circuit. `_apply_safe_mode`
   still forces this knob to `"block"` defensively, so callers
   toggling `allow_coordinate_clicks` back on at runtime don't silently
   revert to `"allow"`. Default `"allow"` preserves v1.6.9 behaviour.

5. **`BrowserConfig.cdp_host` uses `_is_loopback_host`.** The v1.6.8
   review (C-3) widened the `remote_cdp_url` loopback check to accept
   the full 127.0.0.0/8 block plus `::1`; the parallel `cdp_host`
   validator at `config.py:344` still used the literal set
   `{"127.0.0.1", "localhost"}`. v1.6.10 unifies the two checks so
   `cdp_host` accepts the same hosts as `remote_cdp_url`. The error
   message was updated to mention the loopback block.

6. **`Agent.get_owned_cdp_connection_info()`.** Returns a structured
   `CdpConnectionInfo` (`cdp_url`, `profile_dir`, `ownership_token`)
   or `None`. Bundles the three values a co-resident `remote_cdp`
   Agent needs to attach to a webTool-launched browser, so callers
   don't have to discover `BrowserManager.get_cdp_endpoint`,
   `get_effective_profile_dir`, and `get_ownership_token` separately.
   New MCP tool `web_get_owned_cdp_connection_info` is the MCP
   counterpart. `CdpConnectionInfo` is exported from
   `web_agent.__init__`.

7. **README + AGENTS named-profile caveat.** Both files now prominently
   note that `profile_mode="named"` exposes a **single shared
   `BrowserContext`** for all sessions on that profile (Playwright's
   `launch_persistent_context` limitation -- one persistent context per
   user-data-dir). Sessions on a named profile share cookies,
   localStorage, IndexedDB, and cache. Use `profile_mode="ephemeral"`
   for per-session isolation.

8. **Five new integration tests** in
   `tests/test_agent.py::TestV1610Integration` covering:
   - `get_owned_cdp_connection_info` returns the full bundle after an
     isolated `cdp_owned` launch
   - `document.cookie` persistence across two `Agent` lifetimes on a
     named profile (companion to the v1.6.9 localStorage test)
   - `click_xy` with `coordinate_click_unknown_policy="block"` rejects
     clicks on an empty page
   - `fetch_smart` routes a `"pdf"` classification (the extensionless-
     PDF case post-v1.6.10 enum change) through `fetch_binary`
   - Two `session_id`s on a named profile observably share the
     persistent `BrowserContext` (regression test for the
     documented limitation)

### Behaviour preserved (no migration needed)

- `web_research` callers that don't pass `extract_files` see the
  v1.6.9 behaviour exactly (file URLs continue to land in
  `download_candidates`); only the gate fix in Item 1 changes the
  outcome, and only for callers who were already getting
  silently-dropped binaries (a bug fix, not a behaviour change).
- `coordinate_click_unknown_policy` defaults to `"allow"`, matching
  the v1.6.9 permissive default.
- `cdp_host="127.0.0.1"` and `cdp_host="localhost"` -- the only two
  values previously accepted -- continue to validate. v1.6.10 only
  widens what is accepted.
- The MCP server adds one new tool and one new optional parameter;
  existing tool schemas and signatures are unchanged.

### Files changed

- `web_agent/web_fetcher.py` -- `_BINARY_KINDS`, `_is_binary_kind`,
  `_EXT_TO_KIND`, `_CT_TO_KIND`, rewritten `_url_ext_classification`
  and the `classify_url` HEAD-result mapping, `fetch_smart` routing
  check.
- `web_agent/agent.py` -- imports, `_is_binary_kind` callsites at
  lines 542 and 566, `web_research` `extract_files` propagation, new
  `Agent.get_owned_cdp_connection_info`.
- `web_agent/recipes.py` -- imports, `web_research` `extract_files`
  param + filter-loop branch, gate fix at the binary-result check,
  `find_and_download_file` HEAD-probe classification refinement.
- `web_agent/config.py` -- `SafetyConfig.coordinate_click_unknown_policy`
  field + `_apply_safe_mode` forcing, `cdp_host` validator using
  `_is_loopback_host`.
- `web_agent/browser_actions.py` -- `_do_click_xy` unknown-policy
  block path.
- `web_agent/models.py` -- new `CdpConnectionInfo` Pydantic model.
- `web_agent/mcp_server.py` -- `web_research` `extract_files` param,
  new `web_get_owned_cdp_connection_info` tool.
- `web_agent/__init__.py` -- version bump to `1.6.10`, export
  `CdpConnectionInfo`.
- `tests/test_agent.py` -- new `TestV1610Integration` class.
- `tests/test_v169_smart_binary_routing.py` -- stub return values
  updated to the new granular kinds (`"pdf"`, `"binary_other"`, ...)
  plus 2 new tests verifying the new routing branches.
- `README.md`, `AGENTS.md`, `SECURITY.md` -- documentation updates.

## [1.6.9] - 2026-05-18

### Hardening Patch (10 items, no new features)

v1.6.9 is a **discipline slice**: no new big features, just safety
hardening and consistency cleanup based on the post-v1.6.8 review. The
test count grows from ~574 to ~734 (160 new tests across 8 new
`tests/test_v169_*.py` files; pre-existing v1.6.6/v1.6.7/v1.6.8 tests
that asserted now-changed defaults were also updated).

Two **P0 ship-blockers** are fixed:

1. **`click_xy` no longer bypasses form-submit safety.** Prior versions
   logged a warning under `safe_mode` and clicked anyway; coordinate
   clicks could hit submit/login/delete/pay buttons without any
   heuristic check. v1.6.9 adds `safety.allow_coordinate_clicks`
   (default `True`, forced `False` in `safe_mode`) and, when
   `allow_form_submit=False`, runs `document.elementFromPoint(x, y)` to
   inspect the target element + 5 ancestors and block submit/destructive
   controls.
2. **`remote_cdp` now requires an ownership token.** v1.6.8 accepted any
   loopback `ws://` URL via `chromium.connect_over_cdp`, which violated
   the "webTool only controls browsers it launched" design rule -- a
   user's personal Chrome on `127.0.0.1:9222` would attach fine. v1.6.9
   adds filesystem-anchored ownership tokens
   (`web_agent/ownership.py:OwnershipToken`): webTool writes a random
   64-char hex token into `<profile_dir>/.webtool-ownership` on every
   isolated launch, and `remote_cdp` callers must present a matching
   token via `BrowserConfig.remote_cdp_ownership_token` +
   `remote_cdp_profile_dir`.

Plus **8 should-fix items**: launch_persistent_context refactor for
named profiles, `--no-sandbox` auto-detect, shared smart-binary routing,
MCP as optional dep, configurable locale/timezone/user-agent,
`SkillsConfig.enabled` rename with deprecation alias, MCP docstring
polish, and integration test expansion.

### click_xy safety inspection (P0)

* `web_agent/browser_actions.py:_do_click_xy` rewritten:
  * Honors `safety.allow_coordinate_clicks` (default `True`; forced
    `False` by `safe_mode=True` -- master kill-switch behavior
    matching the v1.6.5 `allow_form_submit` / `allow_js_evaluation`
    pattern).
  * When `allow_form_submit=False`, runs `_inspect_element_at_point`
    (returns up to 5 ancestors via `document.elementFromPoint`) and
    feeds the result through new `_looks_like_destructive_at_point`.
  * Blocks the click when the inspection identifies submit/login/
    delete/pay controls; allows on empty/failed inspection (cannot
    tell -> default permissive, matching selector-path behavior).
* New `_DESTRUCTIVE_TEXT_PATTERN` regex covers submit/send/save/
  login/register/continue/delete/remove/pay/buy/purchase/checkout/
  order/accept/agree/consent/allow/enable.
* New `SafetyConfig.allow_coordinate_clicks: bool = True`.
* 37 new tests in `tests/test_v169_click_xy_safety.py`.

### remote_cdp ownership token (P0)

* New `web_agent/ownership.py:OwnershipToken` with three classmethods:
  * `issue(profile_dir)` -- generates a 64-char hex token, writes it
    to `<profile_dir>/.webtool-ownership`, chmods 0o600 best-effort.
  * `read(profile_dir)` -- returns the token string or `None`.
  * `verify(profile_dir, candidate)` -- constant-time compare via
    `secrets.compare_digest`.
* New `BrowserConfig.remote_cdp_ownership_token: Optional[str]` and
  `BrowserConfig.remote_cdp_profile_dir: Optional[str]`. Both **required**
  when `backend='remote_cdp'`.
* `BrowserManager.start()` now:
  * Writes a fresh token under the active profile dir after every
    successful isolated launch (ephemeral + named).
  * Verifies the configured token against the on-disk file BEFORE
    opening a CDP connection; mismatched tokens raise `BrowserError`.
* New public exports: `OwnershipToken`, `BrowserManager.get_ownership_token()`,
  `BrowserManager.get_effective_profile_dir()`.
* 16 new tests in `tests/test_v169_remote_cdp_ownership.py`.

**Breaking** (intentional): v1.6.8 configs that set
`backend='remote_cdp'` without a token will fail at config-validation
time. Documented as a deliberate fix; v1.6.8's behavior was unsafe.

### Named profile -> launch_persistent_context

* `BrowserManager.start()` dispatches `isolation_mode=True` +
  `profile_mode='named'` through `chromium.launch_persistent_context`
  (returns a `BrowserContext`, not a `Browser`). Prior versions used
  `chromium.launch(--user-data-dir=...)` + `browser.new_context(...)`,
  which created incognito-flavoured contexts that did **not** share
  state with the persistent profile -- cookies / localStorage often
  did not survive across `Agent` lifetimes as users expected.
* New `_NoCloseContextProxy` forwards every attribute to the underlying
  persistent context but turns `close()` into a no-op; all callers
  (sessions, ephemeral fetches) share the single persistent context
  without accidentally closing it.
* Persistent context closed once, from `BrowserManager.stop()`.
* Integration test in `tests/test_agent.py::TestV169NamedProfilePersistence`
  performs the cookie/localStorage round-trip across two `Agent`
  lifetimes.
* 6 new unit tests in `tests/test_v169_persistent_profile.py`.

### --no-sandbox auto-detect

* New `BrowserConfig.disable_chromium_sandbox: Optional[bool] = None`:
  * `True` -> always pass `--no-sandbox`.
  * `False` -> never pass it.
  * `None` (default) -> auto-detect: enabled in CI (`CI=true`,
    `GITHUB_ACTIONS=true`) or container (`/.dockerenv` exists).
* New `_should_disable_chromium_sandbox` helper in `browser_manager.py`.
* 16 new tests in `tests/test_v169_no_sandbox_autodetect.py`.

**Behavior change**: local dev no longer passes `--no-sandbox` by
default. This is a deliberate hardening since the sandbox provides
per-tab isolation against renderer exploits. Operators relying on
`--no-sandbox` locally set `disable_chromium_sandbox=True`. CI keeps
working (auto-detect).

### Shared smart-binary routing

* New `WebFetcher.fetch_smart(url, *, session_id, binary_probe=True)`
  consolidates the binary-vs-HTML routing rules previously duplicated
  across `Agent.fetch_and_extract`, `Agent.search_and_extract`,
  `Recipes.search_and_open_best_result`, and `Recipes.web_research`.
* Recipes now route their top-result fetches through `fetch_smart` so
  extensionless binary URLs (regulator dashboards etc.) get
  `fetch_binary`'d instead of dumped into the HTML extractor.
* 6 new tests in `tests/test_v169_smart_binary_routing.py`.

### MCP as optional dependency

* `mcp[cli]>=1.0.0` moved from `dependencies` to
  `[project.optional-dependencies] mcp`.
* New import-time guard in `web_agent/mcp_server.py` raises an
  `ImportError` with the install hint
  `pip install "web-agent-toolkit[mcp]"` when the package is missing.
* 3 new tests in `tests/test_v169_mcp_optional.py`.

**One-time install action**: existing users who installed via
`pip install web-agent-toolkit` AND use the MCP server need to reinstall
with `pip install "web-agent-toolkit[mcp]"`.

### Configurable locale / timezone / user-agent

Previously hardcoded in `BrowserManager._build_context`:

* `BrowserConfig.locale: str = "en-US"`
* `BrowserConfig.timezone_id: str = "America/New_York"`
* `BrowserConfig.user_agent_mode: Literal["random", "explicit", "playwright_default"] = "random"`
* `BrowserConfig.user_agent: Optional[str] = None` (required when mode is `explicit`)
* Defaults preserve v1.6.8 behavior.
* 11 new tests in `tests/test_v169_browser_locale_ua.py`.

### SkillsConfig.enabled rename

* New canonical field: `SkillsConfig.project_skills_enabled` (the old
  name `enabled` only ever governed the project-tier load -- not
  workspace or builtin -- which was confusing).
* Old name `enabled` kept as a `validation_alias` for one release;
  using it emits a `DeprecationWarning`. Will be removed in v1.7.0.
* 5 new tests in `tests/test_v169_skills_alias.py`.

### MCP server docstring

Replaced the hardcoded "Exposes 12 tools" introduction with a category
list (search, fetch, download, browser automation, sessions, tabs,
network/trace diagnostics, domain skills, recipes). The decorated-tool
count grows with each release; categories don't.

### Files added

| Path | Purpose |
|------|---------|
| `web_agent/ownership.py` | `OwnershipToken` writer/reader/verifier |
| `tests/test_v169_click_xy_safety.py` | 37 tests for elementFromPoint inspection |
| `tests/test_v169_remote_cdp_ownership.py` | 16 tests for token round-trip + verification |
| `tests/test_v169_persistent_profile.py` | 6 unit tests for launch_persistent_context dispatch |
| `tests/test_v169_no_sandbox_autodetect.py` | 16 tests for CI/container detection |
| `tests/test_v169_smart_binary_routing.py` | 6 tests for `fetch_smart` |
| `tests/test_v169_mcp_optional.py` | 3 tests for MCP import-guard |
| `tests/test_v169_browser_locale_ua.py` | 11 tests for locale/tz/UA config |
| `tests/test_v169_skills_alias.py` | 5 tests for `SkillsConfig.enabled` -> `project_skills_enabled` |

### Files modified

| Path | Change |
|------|--------|
| `web_agent/__init__.py` | Bump `__version__` to `1.6.9`; export `OwnershipToken` |
| `web_agent/config.py` | `SafetyConfig.allow_coordinate_clicks` + `_apply_safe_mode` override; `BrowserConfig.{remote_cdp_ownership_token, remote_cdp_profile_dir, disable_chromium_sandbox, locale, timezone_id, user_agent_mode, user_agent}`; `_validate_user_agent_mode`; remote_cdp validator extended with token + profile_dir requirements; `SkillsConfig.project_skills_enabled` (alias `enabled`) + deprecation warning |
| `web_agent/browser_actions.py` | Rewrite `_do_click_xy` with safety gate + elementFromPoint inspection; new `_inspect_element_at_point` + `_looks_like_destructive_at_point` helpers; new `_DESTRUCTIVE_TEXT_PATTERN` |
| `web_agent/browser_manager.py` | Three-way dispatch in `start()` (playwright launch / `launch_persistent_context` / `connect_over_cdp` with token verify); `_NoCloseContextProxy` for shared persistent context; `_resolve_user_agent` + `_should_disable_chromium_sandbox` helpers; locale/tz/UA from config |
| `web_agent/web_fetcher.py` | New `fetch_smart` consolidating binary-vs-HTML routing |
| `web_agent/agent.py` | `fetch_and_extract` and `search_and_extract` (URL branch) call `fetch_smart` instead of duplicating routing |
| `web_agent/recipes.py` | `search_and_open_best_result` and `web_research` call `fetch_smart` |
| `web_agent/mcp_server.py` | Import-time guard on `mcp` package; docstring categories instead of hardcoded count |
| `web_agent/domain_skills.py` | Reads `config.skills.project_skills_enabled` (was `.enabled`) |
| `pyproject.toml` | `mcp[cli]` moved to `[project.optional-dependencies] mcp`; kept in `dev` for tests |
| `tests/test_v166_coord_click.py`, `tests/test_v166_isolation.py`, `tests/test_v167_domain_skills.py`, `tests/test_v168_remote_cdp.py`, `tests/test_v168_diagnostics_config.py` | Updated to match v1.6.9 behavior (token + profile_dir on positive remote_cdp paths; persistent_context dispatch for named profiles; `--no-sandbox` no longer asserted unconditionally; `project_skills_enabled` instead of `enabled`) |
| `tests/test_agent.py` | New integration test class `TestV169NamedProfilePersistence` for cookie/localStorage round-trip |
| `CHANGELOG.md`, `README.md`, `AGENTS.md`, `SECURITY.md` | v1.6.9 documentation refresh |

### Backward-compatibility summary

* **Behavior-preserving defaults**: locale / timezone_id / user_agent
  defaults match v1.6.8 exactly. `allow_coordinate_clicks=True`
  preserves existing `click_xy` callers UNLESS they also have
  `allow_form_submit=False` (in which case they get the new inspection
  check, which is the intent).
* **Deprecated**: `SkillsConfig.enabled` still works via alias but
  emits `DeprecationWarning`. Will be removed in v1.7.0.
* **Intentional break**: v1.6.8 `remote_cdp` configs without a token
  will now fail validation -- the v1.6.8 behavior was unsafe.
* **Behavior change**: `--no-sandbox` is no longer passed locally by
  default. CI keeps working (auto-detect). Local users wanting it set
  `disable_chromium_sandbox=True`.
* **One-time install action**: MCP server users must reinstall with
  `pip install "web-agent-toolkit[mcp]"`.

### Foot-gun callouts

* **Named-profile shared context**: under v1.6.9, all sessions on a
  named profile share the *single* persistent `BrowserContext`. This
  is a Playwright limitation: `launch_persistent_context` returns one
  context that owns the profile, and additional `new_context()` calls
  produce incognito contexts that do not see the profile. The new
  `_NoCloseContextProxy` makes this transparent at the API surface
  but means popups / dialogs from one session are observable by all
  sessions sharing the profile. Use ephemeral profiles for
  isolation-per-session.
* **`--no-sandbox` flip**: containers without `/.dockerenv` (e.g. some
  Kubernetes runtimes) and CI providers other than GHA need to set
  `disable_chromium_sandbox=True` explicitly. Otherwise Chromium may
  fail to start with a `Failed to move to new namespace` error.
* **Ownership token + named profiles**: each `Agent.start()` overwrites
  the token under a named profile. Spinning up two sibling
  `remote_cdp` agents against the same named profile requires reading
  the token AFTER the launcher starts (not before).

## [1.6.8] - 2026-05-17

### Diagnostics and Advanced Browser Intelligence (6 features)

Turns webTool from a *successful* browser tool into an **explainable
and debuggable** one. Per the upgrade doc, this is the slice that
"makes webTool explainable and debuggable for complex dynamic
websites." Every diagnostic surface is **off by default** -- existing
callers see no behavior change.

Test count 510 -> 574 (64 new tests across 6 new files
`tests/test_v168_*.py`).

#### Feature 1 -- Network Event Capture (Rank 7, P1)

New ``NetworkCollector`` (``web_agent/network_collector.py``) attaches
``page.on("request" | "response" | "requestfailed")`` to every Page the
Agent creates. Storage uses ``WeakKeyDictionary[Page, deque]`` so closed
Pages auto-evict. The deque ``maxlen`` enforces ``max_network_events``
with O(1) eviction.

* New ``NetworkEvent`` Pydantic model -- ``event_type``, ``url``,
  ``method``, ``resource_type``, ``status_code``, ``content_type``,
  ``request_headers``, ``response_headers``, ``timing_ms``,
  ``failure_text``, ``occurred_at``, ``correlation_id``.
* New ``FetchResult.network_events`` field + ``ActionSequenceResult.network_events``
  field. Both default to ``[]``; populated only when capture is on.
* Attachment sites: ``BrowserManager.new_page``, ``TabManager`` (initial
  page + ``new_tab`` + popup hook), ``SessionManager``, ``WebFetcher``,
  ``BrowserActions`` fallback page, ``Downloader``.
* Foot-gun: request/response headers may contain Authorization / Cookie
  values. ``include_request_headers`` / ``include_response_headers``
  default False; opt in only.

#### Feature 2 -- API Endpoint Candidate Discovery

Derived from Feature 1. ``NetworkCollector.api_candidates_for(page)``
filters captured events for ``resource_type ∈ {xhr,fetch}`` + JSON
content-type, de-duplicated order-preserving. Surfaced as
``FetchResult.api_candidates`` and ``ActionSequenceResult.api_candidates``.
No new config -- piggybacks on ``capture_network``.

#### Feature 3 -- Download Event Diagnostics

Adds ``page.on("download")`` notification listener (separate from
``downloader.py``'s explicit ``expect_download`` consumer). Captured
URLs surface as ``FetchResult.download_candidates_runtime`` and
``ActionSequenceResult.download_candidates``.

* Foot-gun: ``page.on("download")`` triggers Chromium tmpfile creation.
  When no ``expect_download`` consumer is active, the file would pile up
  -- the listener calls ``download.delete()`` as a side-effect so long
  sessions don't leak temp files.

#### Feature 4 -- Post-Action Screenshot Verification

New ``BrowserActions._capture_verification_screenshot`` writes
``verify-<correlation_id>-<index>.png`` under
``automation.screenshot_dir`` after each successful action when
``DiagnosticsConfig.screenshot_after_action=True``. Best-effort: failure
logs at DEBUG and never fails the sequence. Paths go through
``safe_join_path`` (v1.6.4).

* New ``ActionSequenceResult.verification_screenshots: list[str]``.

#### Feature 5 -- Session Replay / Audit Traces

New ``SessionTraceRecorder`` (``web_agent/trace_recorder.py``) writes
one JSONL file per session under ``diagnostics.trace_dir``. Each line
is ``{ts, ordinal, session_id, correlation_id, method, args, status,
elapsed_ms, url}``. Distinct from ``AuditLogger`` (which is
Agent-call-grained); recorders coexist.

* 3 new ``Agent`` methods: ``replay_trace(file)``, ``list_traces()``,
  ``get_remote_cdp_url()``.
* New CLI subcommand: ``web-agent replay <trace_file>``.
* Foot-gun: session_ids are validated against
  ``^[A-Za-z0-9._-]+$`` before being used as filenames (path-traversal
  defense in depth).

#### Feature 6 -- Remote CDP Backend Abstraction (Rank 10, P2)

Adds third ``BrowserConfig.backend`` literal ``"remote_cdp"`` +
``remote_cdp_url`` field. ``BrowserManager.start()`` dispatches to
``chromium.connect_over_cdp(remote_cdp_url)`` instead of ``launch()``.
``stop()`` disconnects without killing the remote process (per
Playwright's documented ``Browser.close()`` semantics under CDP).

* Config validator enforces loopback-only URLs (same posture as v1.6.6
  ``cdp_host``), ``ws://`` / ``wss://`` scheme, and rejects combinations
  with ``isolation_mode=True`` / ``cdp_enabled=True``.
* New ``Agent.get_remote_cdp_url()`` mirror of ``get_cdp_endpoint()``.
* 3 new MCP tools: ``web_get_remote_cdp_url``, ``web_list_traces``,
  ``web_replay_trace``.

### New configuration

```python
class DiagnosticsConfig(BaseSettings):
    capture_network: bool = False
    max_network_events: int = 500  # bounded [1, 10000]
    network_resource_types: list[str] = ["xhr", "fetch", "document"]
    include_request_headers: bool = False
    include_response_headers: bool = False
    capture_download_intents: bool = False
    screenshot_after_action: bool = False
    trace_enabled: bool = False
    trace_dir: str = "./.webtool-audit/traces"
```

Nested env vars via the existing ``WEB_AGENT_`` prefix +
``env_nested_delimiter="__"``:
``WEB_AGENT_DIAGNOSTICS__CAPTURE_NETWORK=true``.

### Backward-compat notes

* All new behavior is **off by default**. Existing configs boot
  unchanged.
* ``FetchResult`` / ``ActionSequenceResult`` field additions are purely
  additive with ``default_factory=list``.
* ``BrowserConfig.backend`` Literal widens from
  ``Literal["playwright", "cdp_owned"]`` to
  ``Literal["playwright", "cdp_owned", "remote_cdp"]``. Old values keep
  working; the new value is opt-in.
* No new core dependencies.
* MCP tool count grows from ~46 to ~49.

## [1.6.7] - 2026-05-17

### Skills and Playbooks (5 features, browser-harness-inspired)

Turns webTool from a stateless browser backend into one that
*accumulates reusable knowledge about websites*. Per the upgrade doc,
this is the strongest idea borrowed from `browser-harness`: an agent
should reuse known instructions for a site instead of rediscovering
quirks every run.

Test count 454 -> ~506 (51 new tests across 3 new files
`tests/test_v167_*.py`). All five features are opt-in or read-only by
default; the only behavior change is that v1.6.6 callers will now see
the bundled SEC / GitHub / EC skills in ``Agent.list_domain_skills()``
unless they explicitly disable ``skills.builtin_skills_enabled``.

#### Feature 1+2+3 -- Domain Skills Registry + Discovery + Markdown Format (Rank 3, P0)

A "skill" is a markdown file with YAML frontmatter at
``<skill_dir>/<name>.md`` describing how to handle a domain: inputs,
output schema, recommended flow, known selectors, known traps. Three
discovery tiers with priority order:

```
project (highest, default ./.webtool-skills) > workspace > builtin (lowest)
```

* New module: ``web_agent/domain_skills.py`` -- ``SkillRegistry`` +
  parser + dispatcher.
* New module: ``web_agent/builtin_skills/`` -- bundled-skill registry.
* New core dep: ``python-frontmatter>=1.0.0``.
* New ``SkillsConfig`` (master switch ``enabled=False`` for project
  skills; ``builtin_skills_enabled=True`` so the 3 bundled examples
  are visible out-of-box).
* 3 new ``Agent`` methods: ``list_domain_skills``, ``get_domain_skills``,
  ``apply_domain_skill``.
* 3 new MCP tools: ``list_domain_skills``, ``get_domain_skill``,
  ``apply_domain_skill``.
* New CLI subcommand: ``web-agent skills list|show|apply``.
* New exceptions: ``SkillError``, ``SkillNotFoundError``,
  ``SkillNotRunnableError``, ``SkillInputError``.
* New models: ``DomainSkill``, ``SkillInputSpec``,
  ``SkillApplicationResult``.

Bundled skills (always loaded unless ``builtin_skills_enabled=False``):

* ``sec.gov/filing_search`` -- find a company's most recent SEC filing
  of a given form type. Composes the existing
  ``search_and_extract`` recipe with EDGAR-scoped queries.
* ``github.com/release_download`` -- download a release asset via
  ``find_and_download_file``.
* ``ec.europa.eu/document_search`` -- search the EU document
  register across ``ec.europa.eu``, ``eur-lex.europa.eu``,
  ``finance.ec.europa.eu``.

User markdown skills (under ``.webtool-skills/<domain>/<name>.md``)
are *informational only* -- ``apply_domain_skill`` raises
``SkillNotRunnableError`` for them. Bundled skills are dispatchable
because they ship with a Python runner alongside the .md.

#### Feature 4 -- Agent-editable Workspace (Rank 9, P2)

* New module: ``web_agent/workspace.py`` -- ``Workspace`` class with
  mode-gated read/write access to ``./.webtool-workspace/``.
* New ``WorkspaceConfig``. Default ``enabled=False`` (opt-in for
  safety, matching v1.6.6 isolation/CDP defaults). When enabled,
  default ``mode="markdown_skills_only"``.
* 4 safety modes:
  * ``read_only`` -- blocks every write.
  * ``markdown_skills_only`` (default) -- allows ``.md`` writes only
    under ``domain-skills/``; blocks everything else including
    ``helpers.py``.
  * ``reviewed_python_helpers`` -- allows ``.md`` anywhere + a single
    ``helpers.py`` at the workspace root. Execution requires a
    *second* opt-in (``execute_helpers=True``) so a write-permission
    grant alone doesn't enable Python execution.
  * ``unsafe_python_helpers`` -- no restrictions.
* Path traversal blocked unconditionally in every mode via
  ``safe_join_path`` (v1.6.4 helper).
* Workspace skills under ``domain-skills/`` are auto-loaded into the
  ``SkillRegistry`` at startup as the "workspace" priority tier.

#### Feature 5 -- Interaction Skill Library (Rank 12, P2)

8 new top-level ``Agent`` convenience methods for common patterns:

* ``Agent.handle_dialog(action, prompt_text, session_id, tab_id)``
  -- pre-arm the next browser dialog handler.
* ``Agent.select_dropdown(selector, value/label/index, ...)``
  -- wraps existing ``SelectInput``.
* ``Agent.upload_file(selector, paths, ...)``
  -- NEW ``UploadFileInput`` action. Paths default to under
  ``download.download_dir``; widen with
  ``safety.allow_upload_outside_download_dir=True``.
* ``Agent.drag_and_drop(source, target, ...)``
  -- NEW ``DragAndDropInput`` action.
* ``Agent.scroll_until_text(text, max_scrolls, ...)``
  -- scroll until the target text is visible.
* ``Agent.click_inside_iframe(iframe_selector, inner_selector, ...)``
  -- NEW ``IframeClickInput`` action; uses Playwright's frame_locator.
* ``Agent.click_shadow_dom(host_selector, inner_selector, ...)``
  -- NEW ``ShadowDomClickInput`` action; uses ``>>`` pierce combinator.
* ``Agent.print_page_as_pdf(url, output_path, ...)``
  -- Chromium ``page.pdf()``. Reuses ``ScreenshotResult`` shape.

4 new ``ActionType`` enum members (``UPLOAD_FILE``, ``IFRAME_CLICK``,
``SHADOW_DOM_CLICK``, ``DRAG_AND_DROP``) and ``Action`` discriminated
union entries. Every method available as both top-level ``Agent`` API
AND inside ``interact()`` action sequences.

8 new MCP tools: ``web_handle_dialog``, ``web_select_dropdown``,
``web_upload_file``, ``web_drag_and_drop``, ``web_scroll_until_text``,
``web_click_inside_iframe``, ``web_click_shadow_dom``,
``web_print_page_as_pdf``.

#### Safety additions to ``SafetyConfig``

* ``allow_upload_outside_download_dir`` (default ``False``) -- gates
  ``upload_file`` paths to the download directory unless explicitly
  widened. Blocks prompt-injection attempts to exfiltrate arbitrary
  local files.

#### Backward-compat summary

* All new flags default off / disabled.
* New ``Action`` union members are purely additive -- legacy JSON
  callers continue to dispatch correctly.
* New ``python-frontmatter`` dep (~30 KB; pulls in PyYAML which we
  already required).
* ``WorkspaceConfig.workspace_dir`` is named explicitly (not ``path``)
  because pydantic-settings would otherwise read the ``PATH`` environment
  variable as a default on every OS -- documented in the field comment.
* Bundled skills always-visible-by-default IS technically a behavior
  change for ``Agent.list_domain_skills()``. Set
  ``skills.builtin_skills_enabled=False`` to restore the empty registry.

## [1.6.6] - 2026-05-17

### Browser Control Foundation (6 features, inspired by browser-harness)

A major capability expansion adapting `browser-harness` ideas to
`web_agent`'s structured architecture, with a firm safety boundary:
**webTool never attaches to the user's existing personal Chrome.** It
may launch its own isolated browser and attach to that browser over
CDP, but never reuses a user-owned profile or process.

All six features are opt-in via config flags; defaults preserve v1.6.5
behavior. Test count grew from 409 to **450** (41 new tests across 7
new files).

#### Feature 1 — Browser Isolation Profile Launcher (Rank 1)

`BrowserManager.start()` now optionally launches Chromium with
`--user-data-dir=<webTool-owned-path>`, isolating cookies / localStorage
/ sessionStorage / cache / downloads from the user's real Chrome.

* New config: `browser.isolation_mode` (default `False`),
  `browser.profile_mode` (`"ephemeral"` | `"named"`, default
  `"ephemeral"`), `browser.profile_dir`, `browser.cleanup_on_exit`
  (default `True`).
* Ephemeral profiles auto-generate a tempdir under
  `<base_dir>/.webtool/browser-profiles/run-<token>/` and remove it on
  `Agent.__aexit__`. Failed launches do NOT leak tempdirs.
* Named profiles persist across runs (e.g. for logged-in workflows)
  under a user-specified path; `cleanup_on_exit` is a no-op for named.
* New `BrowserConfig` validator: `profile_mode="named"` requires
  `profile_dir`, else `ConfigError`.
* The existing `BrowserConfig.user_data_dir` field is now marked
  deprecated; if both are set, `profile_dir` wins.

#### Feature 2 — CDP Attach to webTool-Launched Browser (Rank 2)

When `browser.cdp_enabled=True`, launch args grow
`--remote-debugging-port=<port>` and `--remote-debugging-address=<host>`.
The actual port (when `cdp_port=0` for OS-assigned) and ws URL are
discovered from `<user-data-dir>/DevToolsActivePort` and exposed via
`Agent.get_cdp_endpoint() -> ws://host:port/devtools/browser/<uuid>`.

* New config: `browser.backend` (`"playwright"` | `"cdp_owned"`),
  `browser.cdp_enabled` (default `False`), `browser.cdp_host` (default
  `"127.0.0.1"`), `browser.cdp_port` (default `0`),
  `browser.launch_owned_cdp_browser`, `browser.attach_existing_browser`.
* `BrowserConfig` validator rejects four foot-guns:
  - `attach_existing_browser=True` -> `ConfigError`.
  - `cdp_enabled=True` without `isolation_mode=True` -> `ConfigError`
    (DevToolsActivePort needs a user-data-dir).
  - `cdp_host` not in `{"127.0.0.1", "localhost"}` -> `ConfigError`
    (no public CDP binding).
  - `backend="cdp_owned"` without `cdp_enabled=True` -> `ConfigError`.
* New MCP tool `web_get_cdp_endpoint`. No `connect_over_cdp` re-attach
  path is provided -- deferred to v1.7 if real demand surfaces.

#### Feature 3 — Tab Management (Rank 6)

Sessions now own a `TabManager` that maps `tab_id -> Page`, with a
sticky `_current_tab_id` pointer and popup auto-registration via
`ctx.on("page", ...)`. New `BaseAction` parent class adds an optional
`tab_id: Optional[str] = None` to every Action input -- transparent to
v1.6.5 JSON callers (Pydantic discriminated union dispatches on the
`action` literal, not class identity).

* New module: `web_agent/tab_manager.py`.
* New models: `TabInfo`, `BaseAction`.
* `SessionManager.create()` instantiates the TabManager BEFORE the
  initial UA-probe page, registering that page as the `"main"` tab.
* New `Agent` methods (all session-scoped): `list_tabs`, `current_tab`,
  `new_tab`, `switch_tab`, `close_tab`.
* New MCP tools: `web_list_tabs`, `web_current_tab`, `web_new_tab`,
  `web_switch_tab`, `web_close_tab`.
* `_PAGE_DIALOG_STATES`-style `WeakKeyDictionary` pattern reused for
  `TabManager._reverse: Page -> tab_id` -- auto-evicts on page close.

**Behavior change:** `execute_sequence` against an existing `session_id`
now reuses the session's current tab instead of opening a fresh page
per call. Cookies/storage were already shared in v1.6.5; now scroll,
viewport, and `Page` identity are shared too -- more intuitive but
different. Escape hatch: set `automation.fresh_tab_per_call=True` to
restore v1.6.5 fresh-page behavior.

#### Feature 4 — Coordinate Click + Low-Level Input (Rank 5)

Three new Action discriminator types for when selectors fail (canvas
apps, shadow DOM, cross-origin iframes, visual-only controls):
`ClickXYInput`, `TypeTextInput`, `PressKeyInput`. Coordinates are CSS
pixels (what `page.mouse.click` expects), not device pixels -- always
honor `ObserveResult.device_pixel_ratio` when mapping from a screenshot.

* New action types: `ActionType.CLICK_XY`, `TYPE_TEXT`, `PRESS_KEY`.
* New `Agent` methods (all require `session_id`): `click_xy`,
  `type_text`, `press_key`.
* New `BrowserActions.execute_single_on_session(action, session_id,
  tab_id)` helper -- shared by all three top-level methods.
* New MCP tools: `web_click_xy`, `web_type_text`, `web_press_key`.
* Safety note: coord click bypasses the `_looks_like_submit` heuristic
  (no selector to inspect). Under `safety.safe_mode=True`, a WARNING
  is logged but the click runs -- safe_mode was a config-level opt-in,
  not a per-coord-click block.

#### Feature 5 — Screenshot-First `observe()` Mode (Rank 4)

`Agent.observe(url, session_id, tab_id, include_text, include_aria)`
returns an `ObserveResult` with screenshot path, viewport / page /
scroll dimensions, `device_pixel_ratio`, optional truncated visible
text, and optional ARIA snapshot. One `page.evaluate(...)` round-trip
for all dimensions.

* New model: `ObserveResult`.
* Screenshot path resolution goes through the v1.6.4 cross-platform
  `safe_join_path`. Files land under `automation.screenshot_dir/
  observe_<correlation_id>_<timestamp>.png`.
* Text capture truncates to `safety.max_chars_per_call` so observed
  pages can't blow the context budget.
* ARIA capture is opt-in (`include_aria=True`) -- snapshots can be
  megabytes on complex pages.
* New MCP tool: `web_observe`.
* New CLI subcommand: `web-agent observe <url>`.

#### Feature 6 — Doctor Command (Rank 8)

`Agent.doctor(quick=False)` runs 14 capability probes and returns a
`DoctorReport` with summary `healthy` | `usable_with_warnings` |
`unusable`. Probes: Python + web_agent version, Playwright import,
Chromium driver path, headless browser launch (skipped with `quick=True`),
DDGS, SearXNG reachability, FastMCP, three binary-extraction extras
(pypdf / openpyxl / python-docx), three writable dirs (downloads /
screenshots / debug), network connectivity to example.com,
robots/rate-limit sanity, and YAML config parse from
`WEB_AGENT_CONFIG`.

* New module: `web_agent/doctor.py`.
* New models: `DoctorCheck`, `DoctorReport`.
* Bypasses `Agent._call_scope` audit logging and `SafetyConfig`
  domain gating by design -- doctor is a capability self-check, not
  a regular agent operation.
* Each probe wrapped in `asyncio.wait_for(..., 5.0)` -- the run never
  crashes; per-check failures become `fail`-status entries.
* New MCP tool: `web_doctor`.
* New CLI subcommand: `web-agent doctor [--quick] [--json]`. CI gate
  via exit code 2 when `summary == "unusable"`.

#### New automation config

* `automation.fresh_tab_per_call: bool = False` -- restore v1.6.5
  fresh-page-per-call behavior for session-owned `interact()` calls.

#### Backward-compat summary

* All new `BrowserConfig` fields default to off / disabled.
* `BaseAction` parent class is purely additive; `tab_id` defaults to
  None and is omitted from JSON via `exclude_none=True`.
* Legacy Action JSON without `tab_id` continues to parse cleanly
  through `TypeAdapter[Action]`.
* The single behavioral change is per-call tab reuse for sessions
  (Feature 3); flip `automation.fresh_tab_per_call=True` to restore.

## [1.6.5] - 2026-05-11

### Self-review pass (16 findings: SSRF + cookie isolation + polish)

A focused fix-up release driven by an internal full-project review of
v1.6.4. Three classes of issue:

1. **SSRF / cookie-isolation gaps** that the v1.6.4 review missed --
   the post-redirect re-check existed in some code paths but not all,
   and httpx-based session paths leaked cookies across hosts.
2. **Documented-but-broken behaviors** -- the README's nested env-var
   pattern silently did not work; the `searched_at` timestamp was
   rewritten on cache hits.
3. **Long-tail polish** -- dead code, extension-list drift, fragile
   attribute-stuffing on Playwright objects, missing MCP config path,
   missing pattern normalization on deny/allow lists.

All 16 issues land with regression tests; the test count grew from
~376 to **409**. Backward-compatible -- the public Agent API is
unchanged; the only "breaking" change is that
`WebFetcher._cookies_for_session` now takes a `target_url` and returns
`httpx.Cookies` instead of `dict[str, str]`. That helper is private
(underscored) and re-exporting was never claimed.

#### Critical (security)

- **Cross-domain cookie leak in `WebFetcher._cookies_for_session`
  closed.** The helper previously returned a flat `{name: value}` dict
  that httpx would send to every host. With a session logged into
  `bank.com`, calling
  `agent.fetch_binary("https://attacker.com/", session_id=sid)`
  leaked bank auth cookies to the attacker host. The helper now
  filters by Playwright cookie domain (exact or subdomain match
  against the target URL's host) and returns an `httpx.Cookies` jar so
  httpx applies its own cookie-domain rules as a second layer of
  defense. Affects `classify_url` and `fetch_binary` (the two
  httpx-based session paths). The Playwright-driven `fetch` path was
  never affected -- BrowserContext applies cookie-domain rules
  natively.
- **`classify_url` pre-gates the URL.** Adds
  `check_domain_allowed(url, ...)` at the entry of the HEAD-probe
  branch. Before: a denied-domain or private-IP URL would still fire
  a HEAD request even though the policy rejects it. After: returns
  `"unknown"` immediately, no network I/O. Mirrors the same defense
  v1.6.4 added on the redirect side.
- **Playwright download paths re-validate post-redirect URLs.**
  `_do_save_page` now checks `page.url` after `page.goto(...)`;
  `_download_with_playwright` checks `download.url` (the actual
  download origin, post-redirect) before any `save_as` call. Closes
  the SSRF gap that v1.6.4's `_download_httpx` had already addressed
  -- now consistent across all three download strategies.

#### High

- **`env_nested_delimiter="__"` added to `AppConfig.model_config`.**
  Without it, pydantic-settings v2 silently ignored
  `WEB_AGENT_BROWSER__HEADLESS=false` and similar nested env vars.
  README's claim that this works is now backed by code.
- **Cached DNS resolution.** `_resolve_host_addresses` is a 2048-entry
  LRU cache around `socket.getaddrinfo`. The default-on private-IP
  gate (introduced in v1.6.x) called `getaddrinfo` synchronously per
  outbound URL; with `fetch_many(urls=10)` that's 10x event-loop
  blocking on cold DNS. Now the cache pays once per host per process.

#### Medium

- **Cache hit no longer rewrites `searched_at`.** v1.6.x previously
  overwrote the cached timestamp with `datetime.now()` on every cache
  hit, which meant callers doing time-diff math on the field saw
  stale data labeled fresh. Reverted: the original timestamp is
  preserved; `from_cache=True` is the only correct staleness signal.
- **`async_retry` validates `max_retries >= 1`.** Setting
  `FetchConfig(max_retries=0)` previously caused `async_retry` to
  raise `TypeError("exceptions must derive from BaseException")` at
  call time (because the for-loop body never ran and
  `last_exception` was None). Now raises `ValueError` at decorator
  construction with a clear message.
- **`find_and_download_file` recovers extensionless binary URLs.**
  When no extension-matched candidate exists, the recipe now
  HEAD-probes extensionless results via `classify_url` and treats
  any with `"binary"` classification as download candidates. Catches
  regulator-archive URLs like `sec.gov/Archives/12345` whose path
  has no extension but whose Content-Type is `application/pdf`.
  Gated by `SafetyConfig.probe_binary_urls` (default True).
- **`_unwrap_search_url` caps unwrapped query at 1024 chars.** A
  hostile SERP URL with a giant `?q=` payload would otherwise
  propagate through the search + cache + diagnostic pipeline.
- **Dead `_classify_message` / `_to_structured` /
  `_MESSAGE_PREFIX_CODES` removed.** v1.6.3 introduced `_MessageBag`
  to record codes at the source; the prefix-string fallback has had
  zero internal callers ever since. ~60 lines of dead code gone.

#### Low / polish

- **`take_screenshot` honors `FetchConfig.wait_until`.** Previously
  hardcoded `networkidle`, which is exactly the wait state most
  likely to hang on analytics-polling pages -- and v1.6.2 already
  switched the fetch default away from it. Screenshots now use the
  same configured wait state, so a user tuning for slow renders
  gets consistent behavior across `fetch` and `screenshot`.
- **Shared `_OFFICE_AND_ARCHIVE_EXTENSIONS`.** `web_fetcher` now
  exports a 14-extension shared set used by both `web_fetcher`'s
  `_DOWNLOAD_EXTENSIONS` and `downloader`'s `_BINARY_EXTENSIONS`.
  Each module's set is a documented superset of the shared core.
  Prevents the kind of drift that left `.iso/.deb/.rpm` in the
  fetcher set but not the downloader set.
- **`WeakKeyDictionary` for per-Page dialog state.** Replaces the
  v1.6.4 hack of attribute-stuffing
  `page._web_agent_dialog_state` onto Playwright Page objects.
  Module-level `_PAGE_DIALOG_STATES` keyed weakly so closing a page
  reclaims the entry automatically; future `__slots__`-using
  Playwright versions won't break.
- **`SafetyConfig` auto-normalizes deny/allow patterns.**
  `field_validator` strips `https://`, lowercases, drops trailing
  paths/queries/fragments, and removes leading dots. So
  `denied_domains=["https://Evil.com/"]` now actually blocks
  `evil.com` (previously: silently never matched anything).
- **MCP server honors `WEB_AGENT_CONFIG` env var.** New
  `_load_mcp_config()` helper resolves an optional YAML path before
  falling back to `AppConfig()` (which now picks up nested
  `WEB_AGENT_*__*` env vars per the H4 fix). Operators deploying
  via Claude Desktop / Cursor can now set `safe_mode=True` or a
  domain allowlist without code changes.
- **Test dedup.** `test_v163_messagebag_profiles.py` had ~30 lines
  of duplicated setup; cleaned up.

#### Tests

- New: `tests/test_v165_security.py` (16 cases for cookie isolation,
  classify_url pre-gate, Playwright redirect re-checks).
- New: `tests/test_v165_high_severity.py` (10 cases for
  env_nested_delimiter + DNS caching).
- New: `tests/test_v165_medium.py` (12 cases for cache freshness,
  retry validation, find_and_download recovery, unwrap-query cap,
  dead-code removal regression guard).
- New: `tests/test_v165_low_polish.py` (14 cases for screenshot
  wait_until, shared extensions, MCP config, weak dialog state,
  domain pattern normalization).
- Updated: `tests/test_v162_routing.py`, `tests/test_v163_routing.py`
  for the `_cookies_for_session` signature change. Removed obsolete
  `_classify_message` / `_to_structured` tests from
  `test_v162_models_and_profiles.py`.

Total test count: ~376 -> **409**.

## [1.6.4] - 2026-05-07

### External-review pass (cross-platform path fix, mypy fix, download cap, redirect SSRF)

Addresses an external code review's seven recommendations. Three were
real bugs (one of which explains the long-deferred CI failure on
Linux); the rest are security hardening + documentation polish.

#### P0 -- CI gates: fixed

- **Cross-platform absolute-path detection in `safe_join_path` (security
  helper).** `pathlib.PurePosixPath("C:\\Windows").is_absolute()` is
  False on Linux, so a Linux container would silently accept Windows
  drive-rooted paths from caller-supplied filenames -- defeating the
  point of the helper. New `_is_cross_platform_absolute(path)` rejects
  POSIX absolute, Windows drive-rooted (any letter, either slash),
  Windows root-only, and UNC paths regardless of the OS the check runs
  on. This explains the test that's been failing on the CI Linux
  runners since v1.6.1 (`test_rejects_windows_drive_absolute_path`).
- **bs4 `.get("content")` mypy errors.** `Tag.get()` is typed as
  `str | AttributeValueList | None` in newer bs4 stubs;
  `ExtractionResult` requires `str | None`. The two errors at
  `content_extractor.py:457-458` are gone now -- both meta-tag reads
  coerce via `str(val) if val is not None else None`.

After these two fixes, CI's lint + test jobs pass on all of
Python 3.10 / 3.12 / 3.13 (the v1.6.1-era failure mystery is resolved).

#### P1 -- Security hardening

- **Playwright download paths now enforce `max_file_size_mb`.** Before:
  only the httpx streaming path enforced the cap; the Playwright
  page-save and expect-download paths wrote the full file before any
  size check. Now:
  - Strategy 2 (page-save) pre-checks the navigation response's
    `Content-Length` header and the in-memory rendered DOM size before
    writing -- aborts before any disk write if either exceeds the cap.
  - Strategy 3 (expect-download) post-saves and stat-checks; if
    oversize, the file is unlinked and an HTTP_ERROR result is
    returned.
- **HEAD probe redirect re-validation.** `WebFetcher.classify_url` was
  the one remaining redirect-following code path that didn't re-validate
  the final URL against `check_domain_allowed` after redirects. A
  whitelisted entry host could redirect HEAD to a denied target and
  the probe would happily report 'binary'/'html' based on the denied
  target's headers -- weakening SSRF defense and leaking that the
  redirect target exists. Now probes that follow a redirect to a
  disallowed host return 'unknown' (so the caller falls back to a
  real fetch, which has its own gate).

#### P2 -- Polish

- **CI badge** added to README, plus Python-version and license badges.
- **Install instructions** clarified: source-install only (not on
  PyPI); both `git clone` + `pip install -e ".[dev]"` and
  `pip install "web-agent-toolkit @ git+https://..."` documented.
- **`SECURITY.md`** added: vulnerability reporting process, full
  threat model (in-scope and out-of-scope), enumeration of all 12
  defense-in-depth layers, hardening recommendations for production.
  Documents the DNS rebinding limitation explicitly with mitigation
  options for callers who need it.

### Deferred to v1.7

The reviewer flagged two further items for a v1.7 pass:

- **DNS rebinding mitigation** (pin DNS at pre-check or use Playwright
  request interception). Currently documented in SECURITY.md.
- **`except Exception` audit** (some intentionally swallow result-based
  errors; a few call sites worth tightening).

### Test additions

- `tests/test_v164_fixes.py`: 18 tests for cross-platform absolute
  detection (every Windows drive letter A-Z, UNC, lookalikes that
  must NOT match), Playwright download cap (Content-Length pre-check,
  rendered DOM pre-check, post-save stat+unlink), HEAD probe redirect
  re-validation, bs4 None-content handling.

Total: 342/342 unit tests passing (v1.6.3 was 319; +23).

### Backward compatibility

- All changes are additive or internal helpers. No public-API breaks.
- Behavior change: callers who previously relied on a Windows
  drive-rooted filename being silently accepted on Linux now get a
  ValueError. This is the intended security tightening.
- Old JSON dumps still parse against the v1.6.4 model.

## [1.6.3] - 2026-05-07

### Follow-up to v1.6.2: 8-issue review pass

Tightens the smart-routing + structured-error work that landed in
v1.6.1/v1.6.2. Backward-compatible -- no breaking changes.

#### Smart routing now covers the direct-URL search path (#1)

`Agent.search_and_extract("https://x.com/report.pdf")` previously
called `WebFetcher.fetch` directly in the URL-as-query branch and
fell through to a NETWORK_ERROR for known download URLs. The branch
now runs the same classification + routing logic as
`fetch_and_extract`: known download extensions and HEAD-probed
extensionless documents go through `fetch_binary`; HTML stays on
the browser path.

#### Search results probed for extensionless binaries (#2)

In normal search mode, results were split into file_items / page_items
using URL-extension only. Extensionless PDFs from search results
(common with regulator dashboards: `/download?id=42`) silently fell
into HTML extraction and produced empty pages.

The split now uses a three-way classifier:
- Known download extension -> `binary` (immediate)
- Known HTML extension (`.html`, `.aspx`, `.php`, `.jsp`, `.asp`,
  `.htm`, `.xhtml`, `.shtml`, `.phtml`, `.cgi`) -> `html` (immediate)
- Otherwise -> `unknown` (HEAD-probed)

Unknown URLs are HEAD-probed in parallel via `asyncio.gather`, so
the latency cost is one round-trip total -- not one per result.
Probe failures default to HTML and the URL falls through to the
existing fetch path.

When `extract_files=True`, newly-detected extensionless binaries are
extracted inline; otherwise they land in `download_candidates`.

#### `classify_url` accepts `session_id` (#3)

`WebFetcher.classify_url(url, *, session_id=None)`: when supplied,
the HEAD probe inherits cookies from the Playwright session via the
existing `_cookies_for_session` path. Authenticated extensionless
document URLs (intranet downloads, regulator dashboards) now pass
the same auth gate as `fetch_binary`.

#### Structured warnings/errors recorded at the source (#4)

The hot path no longer relies on prefix-string classification to
derive structured codes after the fact. New internal `_MessageBag`
class with `.warn(code, message, url)` and `.err(code, message, url)`
methods that maintain both string and `ToolMessage` lists in lockstep,
populating `code` at the call site.

`AgentResult.structured_warnings[*].code` is now always one of:
`domain_blocked`, `fetch_failed`, `fetch_exception`, `download_skipped`,
`binary_extraction_failed`, `budget_exceeded`, `no_search_results`,
`no_allowed_pages`, `all_fetches_failed`. The legacy prefix
classifier (`_classify_message` / `_to_structured`) is retained as a
back-compat helper but is no longer used by the agent itself, so
unrecognized message text no longer leaks `code="unknown"` into
results.

#### User-extensible ranking profiles (#8)

New `AppConfig.ranking_profiles: dict[str, list[str]] = {}`. Combined
with the built-in `RANKING_PROFILES` at `Recipes.__init__` time;
user-defined profiles override built-ins on name collision so callers
can redefine, e.g., `"docs"` for an internal portal.

```python
config = AppConfig(ranking_profiles={
    "internal_kb": ["wiki.acme.io", "kb.acme.io"],
    "docs": ["my-docs.acme.io"],   # overrides built-in 'docs'
})
async with Agent(config) as agent:
    result = await agent.web_research(
        "rate-limiter design",
        domain_profile="internal_kb",
    )
```

`Recipes._resolve_hints` (instance method) consults the merged dict;
the legacy free function `_resolve_domain_hints` stays for back-compat
but only sees built-ins.

#### Stale doc cleanup (#5, #6, #7)

- README config table: `wait_until` default now correctly shows
  `domcontentloaded` (was `networkidle` -- doc lag from v1.6.2).
- README MCP section + `mcp_server.py` module docstring: 11 -> 12
  tools, with `web_fill_form_and_extract` listed.
- `content_extractor.py` top docstring updated: PDF/XLSX -> PDF/XLSX/
  DOCX/CSV.
- `pyproject.toml` `[binary]` extra comment updated to reflect DOCX
  inclusion.

### Test additions

- `tests/test_v163_routing.py`: 13 tests for `_url_ext_classification`,
  `classify_url(session_id=...)`, direct-URL routing, and search-result
  parallel probe behavior (probed / not probed / probe disabled /
  promotion to download_candidate).
- `tests/test_v163_messagebag_profiles.py`: 11 tests for `_MessageBag`
  semantics, no-`unknown`-leakage regression, and user-extensible
  ranking profiles (merging, override, unknown-profile silent ignore).

Total: 319/319 unit tests passing (v1.6.2 was 295; +24).

### Backward compatibility

- All new fields/params default to empty/None.
- `_classify_message` / `_to_structured` retained for external callers.
- `_resolve_domain_hints` (free function) retained, now only sees
  built-in profiles.
- `classify_url(url)` (positional only) still works -- the new
  `session_id` is keyword-only.
- Old JSON dumps still parse against the v1.6.3 model.

## [1.6.2] - 2026-05-07

### Follow-up to v1.6.1: 12-issue review pass

This release lands the full client review of v1.6.1 (12 issues across
routing, extraction, MCP surface, ranking, and observability).
Backward-compatible -- existing callers keep working unchanged.

#### Smart binary routing in `fetch_and_extract` (#1, #2, #10)

`Agent.fetch_and_extract(url)` now routes PDF/XLSX/DOCX/CSV URLs
through the binary extractor automatically:

1. URL extension matches a known download type → ``fetch_binary``.
2. Otherwise, with the new ``binary_probe`` flag (default True), send
   a HEAD request and inspect ``Content-Type`` / ``Content-Disposition``
   to detect *extensionless* document URLs (regulator dashboards
   often serve `/download?id=42` with `Content-Type: application/pdf`).
3. Otherwise → normal HTML fetch through the browser.

New `WebFetcher.classify_url(url) -> 'binary' | 'html' | 'unknown'`
exposed as the underlying primitive. Toggle the probe globally via
`SafetyConfig.probe_binary_urls = False`.

#### Streaming binary fetches with size cap (#3)

`fetch_binary` now uses `httpx.Client.stream("GET", ...)` and accumulates
chunks while enforcing `DownloadConfig.max_file_size_mb`. Aborts and
returns `HTTP_ERROR` with a clear message when the cap is hit, instead
of letting a rogue large response exhaust memory.

#### Browser session cookies reused for `fetch_binary` (#4)

When the caller threads `session_id=...` into `fetch_binary`,
authentication cookies set in the Playwright `BrowserContext`
(typically by an earlier `agent.interact(login_url, ...)` call) are
copied into the httpx request. Authenticated regulator dashboards now
work end-to-end without manual cookie handling.

#### MCP tool surface (#5)

- `web_search` exposes `extract_files`.
- `web_search_best` and `web_research` expose `prefer_domains` and
  `domain_profile`.
- `web_fetch` exposes `binary_probe`.
- New tool: `web_fill_form_and_extract` -- the v1.6.1 form-fill recipe
  is now reachable from MCP clients.

#### Ranking profiles (#8)

New `RANKING_PROFILES` dict (publicly importable) with five curated
profiles:

- `official_sources` -- regulators, central banks, multilaterals
- `docs` -- canonical software documentation hosts
- `research` -- arxiv, PubMed, ACM, IEEE, etc.
- `news` -- wire services + leading mastheads
- `files` -- common canonical-PDF / dataset hosts

New parameter `domain_profile: str | None` on
`Agent.search_and_open_best_result` and `Agent.web_research`. Combines
with caller-supplied `prefer_domains`; unknown profiles are silently
ignored.

#### Structured `ToolError` / `ToolWarning` (#12)

New `ToolMessage` model with `code` (snake_case identifier),
`message`, optional `url`, and `severity` (enum: INFO/WARNING/ERROR/FATAL).
`ToolWarning` and `ToolError` are aliases for `ToolMessage`.

Two new fields on `AgentResult` and `ResearchResult`:

- `structured_warnings: list[ToolMessage]` -- machine-readable
  counterpart to `warnings: list[str]`.
- `structured_errors: list[ToolMessage]` -- counterpart to `errors`.

The legacy string lists are still populated -- callers can adopt
structured forms incrementally. v1.7.0 will deprecate the strings.

#### CSV + DOCX extraction (#11)

`ContentExtractor` now dispatches to two more binary branches:

- CSV/TSV -> stdlib `csv` (no extra dependency). Auto-detects
  delimiter, falls back to UTF-8 BOM / latin-1.
- DOCX -> `python-docx` (added to the `[binary]` extra). Walks
  paragraphs and tables.

XLS (legacy) and PPTX deferred -- xlrd has CVEs, PPTX is a niche use.

#### Defaults + housekeeping (#6, #7, #9)

- CI matrix: added Python 3.13 alongside 3.10 and 3.12.
- `FetchConfig.wait_until` default flipped from `"networkidle"` to
  `"domcontentloaded"` -- robust against pages with analytics /
  long-polling that prevent networkidle from ever firing. The
  existing per-URL fallback to `"load"` remains for callers who
  override the default.
- `SearchResultItem` docstring updated: "A single Google search result"
  → "A single search result from any configured provider."

### Test additions

- `tests/test_v162_routing.py`: 13 tests for header-based binary
  detection, smart routing, streaming size cap, cookie sharing.
- `tests/test_v162_extraction.py`: ~10 tests for CSV (multiple
  delimiters, BOM, latin-1, empty) and DOCX (synthesized fixture +
  missing-library degrade).
- `tests/test_v162_models_and_profiles.py`: 14 tests for ranking
  profiles, structured ToolMessage round-trip, message classification.

Total: ~37 new tests, full unit suite 295/295 passing.

### Backward compatibility

- `errors`/`warnings` stay as `list[str]`. New `structured_errors`/
  `structured_warnings` are additive.
- Old JSON dumps still parse against the v1.6.2 model.
- `[binary]` stays optional -- without it, CSV still works (stdlib);
  DOCX/PDF/XLSX degrade with a clear install hint.
- `wait_until` default change is documented; callers that depended on
  `networkidle` can pin it via `AppConfig(fetch={"wait_until": "networkidle"})`.

## [1.6.1] - 2026-05-07

### Failure-surface hardening (7 client-suggested improvements)

This release sharpens what callers see when fetches partially fail,
expands the extraction pipeline to cover PDF/XLSX, and adds a
declarative form-fill recipe for dynamic calendar / filings pages.
All changes are backward-compatible: existing callers continue to
work; new behavior is opt-in via flags or new fields that default
to empty.

#### 1. Warnings split from fatal errors

`AgentResult` and `ResearchResult` previously mixed informational
issues (blocked domains, skipped file URLs) and fatal ones
(everything failed) in a single `errors: list[str]`. Now:

- `errors`: only fatal issues. If non-empty, treat the call as failed.
- `warnings`: non-fatal informational issues; the call still produced
  usable output.

Callers checking `if not result.errors:` for "did this succeed" will
now correctly succeed in cases where one URL of five was blocked.

#### 2. Structured download candidates

When `search_and_extract` encounters PDF/XLSX/etc. URLs it used to
emit a string error per URL ("File URL skipped..."). Now those URLs
land in `AgentResult.download_candidates: list[SearchResultItem]` —
fully structured (title, snippet, position, provider) so callers can
programmatically retry via `agent.download(url)`.

#### 3. Built-in PDF / XLSX extraction

New optional dependency group `[binary]` (pypdf + openpyxl).

- `Agent.search_and_extract(query, extract_files=True)` now routes
  PDF/XLSX results through a binary fetch path (httpx) and extracts
  text inline into `pages` instead of skipping them.
- `ContentExtractor.extract` dispatches on `FetchResult.binary` and
  `content_type` to the right branch:
  - PDF → `pypdf.PdfReader`, with encrypted-PDF detection.
  - XLSX → `openpyxl`, dumped TSV-style per sheet.
- Library-missing path returns
  `ExtractionResult(extraction_method="none")` with a clear
  `pip install 'web-agent-toolkit[binary]'` log warning — never
  crashes the pipeline.

New `WebFetcher.fetch_binary(url, session_id=None)` wraps the binary
GET with the same domain / robots / rate-limit gates as `fetch`, and
re-validates `final_url` after redirects.

#### 4. Caller-provided domain hints (`prefer_domains`)

`Recipes._rank` and the public `Agent.search_and_open_best_result` /
`Agent.web_research` now accept `prefer_domains: list[str]`. Any
result whose host matches a hint (exact or as a parent suffix) gets a
+0.40 ranking bonus — large enough to dominate the well-known-domain
heuristic. Use it when you know the regulator / vendor / source you
expect (e.g. `["ec.europa.eu", "esma.europa.eu"]`).

#### 5. Search-engine SERP URLs unwrap to queries

Calling `search_and_extract("https://www.google.com/search?q=tesla+10k")`
used to fetch the SERP HTML and try to extract content from it
(useless and triggers anti-bot). Now SERP URLs from Google / Bing /
DuckDuckGo / Brave / SearX / SearXNG are unwrapped to their `?q=`
parameter and the toolkit runs its own search instead. Plain URLs
(`https://example.com/page`) still go through the URL-as-query
fast-path.

#### 6. Per-URL fetch diagnostics

New `FetchDiagnostic` model with:

- `url`, `final_url`, `status`, `status_code`
- `provider`: which search backend surfaced the URL (`searxng` /
  `ddgs` / `playwright` / `direct` / `unknown`)
- `block_reason`: `domain_blocked` | `robots_disallowed` |
  `timeout` | `http_error` | `network_error` | `download_skipped` | None
- `content_length`, `response_time_ms`, `from_cache`

`AgentResult.diagnostics` and `ResearchResult.diagnostics` carry one
entry per URL the pipeline considered, in order. Lets callers
programmatically inspect *why* each URL succeeded or failed without
parsing free-form error strings.

`SearchResultItem.provider` (new field) is populated by every search
provider and threaded into diagnostics.

#### 7. Form-fill recipe for dynamic calendar / filings pages

New `FormFilterSpec` model — declarative spec for a search/filter
form (query selector + value, ordered filters, submit, wait_for).

New `Agent.fill_form_and_extract(url, spec, *, session_id=None)`:

1. Open `url`.
2. Fill `spec.query_selector` with `spec.query_value`.
3. Apply each `(locator, value)` in `spec.filters` (auto-detecting
   `<select>` vs input).
4. Click `spec.submit_selector` (or press Enter on the query input).
5. Wait for `spec.wait_for` (or `networkidle`).
6. Run `ContentExtractor.extract` on the post-submit HTML.

Result-based: returns `ExtractionResult(extraction_method="none")`
on timeout / locator-not-found rather than raising.

### Other

- `FetchResult` gained `binary: bytes | None` and `content_type: str | None`
  fields for the binary fetch path; `html` and `binary` are mutually
  exclusive on a given result.
- `AGENTS.md` (new): project-level guide for AI coding agents
  (Codex, Claude Code, Cursor, OpenClaw). Documents setup, lay-of-the-
  land, conventions, and the "how to add a feature" loop.

### Test additions

- `tests/test_v161_models.py`: warnings/download_candidates/diagnostics defaults.
- `tests/test_search_url_unwrap.py`: SERP URL detection + unwrapping.
- `tests/test_prefer_domains.py`: ranking bonus.
- `tests/test_binary_extraction.py`: PDF + XLSX paths, missing-library degrade.
- `tests/test_form_recipe_spec.py`: `FormFilterSpec` validation.

### Backward compatibility

- `errors` field shape unchanged; only its semantics tightened (now
  contains *only* fatal issues). Code checking `if not result.errors:`
  may now succeed where it previously returned True with non-fatal
  noise — this is the intended improvement.
- All new model fields default to empty/None; old JSON dumps still
  parse.
- `[binary]` extra is genuinely optional. Without it, callers that
  don't pass `extract_files=True` see no behavior change at all.

## [1.6.0] - 2026-04-30

### Production-safety hardening (the last two gaps before "ready for use")

#### BrowserActions URL safety (4 new gates)

Browser-automation calls were the only network-touching subsystem
that didn't fully validate URL state. WebFetcher and Downloader had
been hardened in v1.2 (pre-check + post-redirect re-check); this
release closes the same loop on BrowserActions.

1. **`_do_navigate` GOTO pre-check**: ``NavigateInput.url`` is now
   validated against the safety policy *before* ``page.goto`` is
   called. Without this, an LLM-supplied automation script could
   navigate the headless browser to AWS IMDS / RFC1918 / a denied
   host, bypassing the policy that gates fetch and download.
2. **`_do_navigate` post-redirect re-check**: every nav direction
   (GOTO / BACK / FORWARD / RELOAD) re-validates ``page.url``
   afterwards. A whitelisted host can no longer redirect us to a
   private IP via 30x.
3. **`execute_sequence` initial-goto re-check**: the entry-URL goto
   inside ``execute_sequence`` now re-checks ``page.url`` after
   landing. Catches redirects on the very first navigation.
4. **`execute_sequence` per-action drift detection**: after every
   action in the sequence, ``page.url`` is checked. If a click,
   form-submit, or JS-driven nav lands on a disallowed domain or
   private IP, the offending action is downgraded to FAILED, all
   remaining actions are SKIPPED, and the sequence aborts
   regardless of ``stop_on_error``.
5. **`take_screenshot` post-redirect re-check**: ``page.goto`` inside
   the screenshot capture now re-validates ``page.url`` before the
   image is written. Prevents leaking a screenshot of a private-
   network page reached via redirect.

#### Model addition

- ``ScreenshotResult.error_message: str | None`` -- new field. Used
  by the post-redirect-blocked path to surface the failure reason
  (previously a blocked screenshot returned ``status=FAILED`` with
  no explanation).

#### Integration CI lane

The existing lint + unit-test workflow gates every PR. A new
**integration** job now runs the full Playwright + live-network
suite (``tests/test_browser_actions.py`` + ``tests/test_agent.py``,
21 tests, ~2 minutes) on:

- Push to ``main`` (post-merge sanity check)
- Nightly schedule at 07:00 UTC (catches drift in upstream search
  engines, CAPTCHA behavior, Playwright browser releases)
- Manual ``workflow_dispatch``

The integration job is **not** wired to PRs because search engines
can serve CAPTCHAs from CI's IP ranges, and that flakiness shouldn't
block legitimate merges. ``continue-on-error: true`` keeps a
flaky-network run from failing the overall workflow -- the job's
value is signal, not gating. On failure, ``debug/`` and
``screenshots/`` are uploaded as a 3-day artifact for diagnosis.

The job uses ``playwright install --with-deps chromium`` so the
runner picks up Chromium + system libs in one step.

### Tests

- ``tests/test_browser_url_safety.py`` (new): 9 mock-based unit tests
  covering the 5 new gates above. No browser launch -- they mock
  ``Page.url`` and ``Page.goto`` to exercise pre-check, post-
  redirect re-check, and per-action drift paths in isolation.

Test count: 193 -> 202 unit (+9). 214 -> 223 total (192 unit +
21 integration before; 202 unit + 21 integration now).

## [1.5.2] - 2026-04-30

### Hygiene cleanup (10 findings from full-project review)

Bug fix:

- ``SearchEngine.search`` cache hits now reset ``searched_at`` to
  ``datetime.now(timezone.utc)`` instead of returning the original
  search timestamp. Previously, a 1-hour-old cache hit would surface
  a stale ``searched_at`` that misled callers into thinking the data
  was fresh; now ``from_cache=True`` is the source of truth and
  ``searched_at`` reflects when the cached payload was returned.
  ``WebFetcher`` already did the right thing.

Dead code:

- Removed unused ``RetryableHTTPError`` class from
  ``web_agent/utils.py`` -- never raised anywhere; the 5xx path in
  ``web_fetcher.py`` raises a plain ``Exception`` and lets
  ``async_retry`` handle it.
- Removed two unused fixtures (``minimal_html``, ``empty_html``)
  from ``tests/conftest.py``.

Documentation drift:

- ``SearchError`` docstring no longer says "Google and DuckDuckGo
  both failed" -- now correctly describes the v1.4 multi-provider
  chain. Same fix in ``Agent.search_and_extract`` docstring (3 spots),
  ``mcp_server.py:web_search`` docstring, and the README's strict-mode
  example.
- README "Project Structure" section was missing 5 modules added in
  v1.3-1.5: ``rate_limiter.py``, ``robots.py``, ``audit.py``,
  ``cache.py``, ``search_providers.py``. Added with version-stamp
  one-liners.
- README test count refreshed: 111 -> 213, 90 unit -> 192 unit. The
  unit-test command was rewritten to use ``--ignore`` instead of an
  explicit (and incomplete) module list.
- README export count refreshed: 49 -> 60 names; version stamp
  ``v1.1.0`` -> ``v1.5.2``.
- README configuration table + ``config.example.yaml`` now document
  ``browser.slow_mo`` (was implemented but undocumented).

Repo hygiene:

- ``.gitignore`` now also ignores ``cache/`` and ``audit.jsonl``
  (the default runtime output paths for v1.3 audit + v1.5 cache).
- ``search_providers.py`` mixed ``Optional[X]`` and ``X | None`` in
  the same file -- normalized to ``X | None`` (consistent with
  Python 3.10+ idiom and the rest of the file's style).

Comment fix:

- ``WebFetcher.fetch`` cache-lookup comment was already corrected in
  v1.5.1; no change here.

Tests:

- ``test_cache.py`` extended with ``test_fetch_caches_result_and_serves_from_cache``
  -- covers the ``FetchResult.from_cache`` field that previously had
  no direct test (only the ``SearchResponse.from_cache`` counterpart
  was tested).

Test count: 192 -> 193 unit (+1). 213 -> 214 total.

## [1.5.0] - 2026-04-30

### New: disk-backed TTL cache + markdown extraction

Two of the deferred items from 1.4 land here. Both are opt-in (cache
is disabled by default) or no-cost (markdown is populated for free
on every successful trafilatura extraction).

#### Cache layer

- ``web_agent/cache.py``: ``Cache`` ABC + ``DiskCache`` concrete
  implementation. JSON-on-disk, one file per entry keyed by SHA256
  of the input. Per-entry TTL with stale-on-read deletion. Soft
  ``max_cache_mb`` budget enforced via LRU-by-mtime eviction on writes.
- ``CacheConfig``: ``enabled`` (False), ``cache_dir`` (./cache),
  ``ttl_seconds`` (3600), ``max_cache_mb`` (100). Path resolved
  against ``base_dir`` like other directory configs.
- Wired into ``WebFetcher.fetch`` (cache hit skips rate-limit +
  network; robots.txt is still checked first so a host's robots.txt
  changes immediately take effect even on cached URLs) and
  ``SearchEngine.search`` (cache hit short-circuits the full provider
  chain). Both subsystems take a ``cache: Cache | None`` kwarg.
- Only **successful** fetches and **non-empty** search responses are
  cached. Errors / empty results would lock in transient failures
  across the TTL window.

#### Markdown extraction

- ``ExtractionResult.markdown: str | None`` -- new field. Populated
  whenever ``trafilatura`` is the winning extractor.
- ``ContentExtractor._extract_trafilatura`` calls
  ``trafilatura.extract(html, output_format="markdown")`` as a second
  pass after the metadata-rich ``bare_extraction``. Cheap (HTML re-
  parsed once) and the result is what most LLMs prefer to consume --
  preserves headings, lists, links, emphasis without HTML noise.
- ``markdown`` stays ``None`` when bs4 or raw-text fallbacks win
  (those layers have no markdown rendering equivalent).

### Models

- ``FetchResult.from_cache: bool`` -- True when served from cache.
- ``SearchResponse.from_cache: bool`` -- same.
- ``ExtractionResult.markdown: str | None`` -- markdown rendering.

### Tests

- ``tests/test_cache.py`` (new): 18 tests covering DiskCache
  (roundtrip, TTL, expiry-on-access, eviction, corrupt-file, lazy-
  dir-creation) + 4 Agent-integration tests (cache wiring, end-to-end
  search caching).
- ``tests/test_content_extractor.py`` extended with 2 markdown tests.

Test count: 173 -> 192 unit (+19). 192 -> 213 total. All green.

## [1.4.0] - 2026-04-30

### Search pipeline rebuilt: SearXNG -> DDGS -> Playwright

The hardcoded "Google then DuckDuckGo via Playwright" path is replaced
with a configurable provider chain. The old behavior is still available
as the third tier; the new default makes search **3-5x faster** by
skipping browser launch when an API-based provider can answer.

**New flow** for ``Agent.search_and_extract(query)``:

1. **URL short-circuit** -- if ``query`` is itself a single ``http(s)://``
   URL, skip search entirely and fetch + extract the URL directly.
2. **SearXNG** -- query a self-hosted SearXNG JSON API. Privacy-
   respecting metasearch aggregator. Skipped silently when
   ``searxng_base_url`` is not set.
3. **DDGS** -- DuckDuckGo via the ``ddgs`` Python package (no browser).
   Skipped silently if the optional dep is missing.
4. **Playwright** -- browser-driven Google + DDG-HTML scraping (the
   pre-1.4 behavior, still here as the safety-net fallback).
5. Extract page content (existing trafilatura -> bs4 -> raw chain).

### New modules

- ``web_agent/search_providers.py``:
  - ``SearchProvider`` (ABC)
  - ``SearXNGProvider`` -- httpx + SearXNG JSON API
  - ``DDGSProvider`` -- ``ddgs`` package (lazy-imported, optional)
  - ``PlaywrightProvider`` -- absorbed the old Google + DDG-HTML
    parsing logic from ``search_engine.py``

### Refactor

- ``SearchEngine`` is now a ~140-line chain orchestrator over
  ``SearchProvider`` instances. Builds the catalog from
  ``config.search.providers`` (default
  ``["searxng", "ddgs", "playwright"]``) and walks the chain until
  one returns results. ``strict=True`` raises ``SearchError`` if all
  providers exhaust.
- The 200+ lines of Google SERP parsing + DDG-HTML parsing moved out
  of ``search_engine.py`` and into ``PlaywrightProvider``. No public
  API change.

### Config additions

- ``SearchConfig.providers: list[str] = ["searxng", "ddgs", "playwright"]``
- ``SearchConfig.searxng_base_url: str | None = None``
- ``SearchConfig.searxng_timeout: float = 10.0``

### Dependencies

- New runtime dep: ``ddgs >= 9.0.0`` (formerly ``duckduckgo-search``).

### Tests

- ``tests/test_search_providers.py`` (new): 23 tests covering each
  provider in isolation (mocked HTTP / mocked ``ddgs``), the chain
  orchestrator (mocked providers via ``_RecordingProvider``), and
  URL-as-query detection.
- Test count: 171 -> 196 unit tests (192 -> 217 total with
  integration). All green.

### Performance

- Live integration suite (``tests/test_agent.py``) runs in **~36s**
  vs. ~106s pre-1.4 because DDGS resolves search results in
  sub-second without a browser launch.

## [1.3.0] - 2026-04-30

### New: politeness layer + audit log

- **`RateLimiter`** (`web_agent.rate_limiter`): per-host async rate gate
  using minimum-interval scheduling. Different hosts proceed
  concurrently; same-host requests are serialized to
  `safety.rate_limit_per_host_rps` (default **2.0** rps). Set to `0`
  to disable.
- **`RobotsChecker`** (`web_agent.robots`): fetches and obeys each
  host's `robots.txt` before requesting pages. Uses stdlib
  `urllib.robotparser` and an httpx fetcher with a 5-second timeout.
  Per-host TTL cache (default 1 hour). Missing or unreachable
  `robots.txt` is treated as allow-all. Gated by
  `safety.respect_robots_txt` (default **True**).
- **`AuditLogger`** (`web_agent.audit`): append-only JSONL log of every
  public Agent operation. Each entry: `timestamp`, `correlation_id`,
  `method`, `args`, `status` (`success` / `error`), `elapsed_ms`,
  optional `error` repr. Disabled by default; enable via
  `audit.enabled = True` and `audit.audit_log_path = "..."`.

### Wiring

- `WebFetcher.__init__` and `Downloader.__init__` accept new optional
  `rate_limiter` and `robots` kwargs. When set, both gates run before
  any network I/O. `SearchEngine.__init__` accepts `rate_limiter`
  (per-host limit applies to `www.google.com` and `html.duckduckgo.com`).
- `Agent` instantiates a single `RateLimiter`, `RobotsChecker`, and
  `AuditLogger` from `SafetyConfig` / `AuditConfig` and passes them
  into the subsystems. Each public Agent method now runs inside
  `_call_scope(...)` which composes `correlation_scope` + audit log
  into one async-context-manager.

### Config additions

- `SafetyConfig` gains: `rate_limit_per_host_rps: float = 2.0`,
  `respect_robots_txt: bool = True`, `robots_user_agent: str = "web-agent-toolkit"`.
- New `AuditConfig` (top-level `audit:` field on `AppConfig`):
  `enabled: bool = False`, `audit_log_path: str = "./audit.jsonl"`.
- `AppConfig._resolve_paths` now resolves `audit.audit_log_path`
  against `base_dir`.

### Tests

Test count: **150 → 171** unit tests (21 new across `test_politeness.py`
and `test_audit.py`). Integration test count unchanged at 21. Total
**192** passing.

### Deferred to a follow-up

- **Cache layer** (HTTP fetch / search result cache): scoped out of
  this release. Would need: cache key generation, FetchResult
  serialization, TTL + LRU, `from_cache: bool` on result models,
  integration in 3 places.
- **Pluggable search providers**: scoped out. Current `SearchEngine`
  is hardcoded for Google + DuckDuckGo. Refactoring to a
  `SearchProvider` ABC + registry would be a separate architectural
  change.

## [1.2.0] - 2026-04-30

### Security (Critical fixes from full-project code review)

- **Path traversal protection**: `Downloader.download(filename=...)` and
  `ScreenshotInput.path` now reject `..` traversal and absolute paths via
  the new `safe_join_path` helper.
- **SSRF protection**:
  - Added `SafetyConfig.block_private_ips` (default **True**) which blocks
    RFC1918, loopback, link-local (incl. AWS IMDS at 169.254.169.254), and
    unspecified IPs.
  - `Downloader._download_httpx` now installs an httpx event hook that
    re-validates every redirect target against the safety config -- a
    whitelisted host can no longer redirect to a private IP / denied domain.
  - `WebFetcher` re-checks `page.url` after Playwright navigation for the
    same defense-in-depth purpose.

### Breaking Changes

- **`SafetyConfig.allow_js_evaluation` defaults to `False`** (was implicitly
  True). MCP/CLI callers that rely on `EvaluateInput` actions must opt in
  by setting `safety.allow_js_evaluation: true` (or run with `safe_mode=False`
  AND no longer rely on the old implicit allow).
- The single `safe_mode` flag has been split into 4 granular flags:
  - `allow_js_evaluation` (default False) -- gates `EvaluateInput`
  - `allow_downloads` (default True) -- gates file downloads
  - `allow_form_submit` (default True) -- gates submit-button clicks
  - `block_private_ips` (default True) -- SSRF protection
- `safe_mode` remains as a master kill-switch: when True it forces all
  three `allow_*` flags to False (regardless of explicit settings).

### Correctness Bugs Fixed

- **UnboundLocalError in `BrowserActions.execute_sequence`**: when
  `ctx.new_page()` failed on the session path, the original exception was
  shadowed by an `UnboundLocalError`. Fixed by initializing cleanup state
  before the if/else branch.
- **`debug_artifacts` lost across retries**: `WebFetcher._do_fetch` declared
  the artifacts list inside the retry-decorated function, so files written
  to disk on a transient failure became unreachable from the eventual
  `FetchResult`. The list is now hoisted to the outer scope and accumulates
  across all retry attempts.
- **`BrowserManager.start()` race condition**: two concurrent calls could
  both launch Chromium and leak the first browser. Fixed with an
  `asyncio.Lock` covering both `start()` and `stop()`.

### Exception Hierarchy

The 10 typed exception classes that were exported but never raised are now
wired up at the obvious internal sites:

- `BrowserError` -- raised by `BrowserManager.start()` on Playwright launch
  failure.
- `SearchError` -- raised by `SearchEngine.search(strict=True)` when both
  Google and DuckDuckGo return zero results.
- `DomainNotAllowedError` -- raised by `check_domain_allowed(strict=True)`.
- `ConfigError` -- raised by `AppConfig.from_yaml()` on YAML parse or
  validation failure.
- `SelectorNotFoundError` -- raised by `_resolve_locator` when the
  `LocatorSpec` is empty.
- `ActionError` -- raised by `_do_select` when no value/label/index is set.
- `NavigationError` -- raised by `Downloader._download_httpx` on disallowed
  redirect targets, and by `WebFetcher` when post-redirect URL is blocked.
- `ExtractionError` -- raised by `ContentExtractor.extract(strict=True)`
  when all three extraction layers fail.

### New `strict=False` parameter

All public `Agent` methods now accept `strict: bool = False`:
- `Agent.search_and_extract(..., strict=True)` -- raises `SearchError`
- `Agent.fetch_and_extract(..., strict=True)` -- raises `NavigationError`
- `Agent.download(..., strict=True)` -- raises `DownloadError`

Default `strict=False` preserves the existing result-based API.

### Reliability

- `_download_httpx` cleans up partial files (try/finally + unlink) so a
  size-limit-exceeded download doesn't leave corrupted bytes that the
  Playwright fallback overwrites with HTML.
- `WebFetcher` networkidle-to-load fallback now persists across retry
  attempts. Previously, every retry would burn 45s on networkidle before
  falling through; now once the first attempt fails, subsequent retries
  use `load` directly.
- `SearchEngine` rejects DDG result URLs with non-http(s) schemes
  (`javascript:`, `data:`).
- `_looks_like_submit` heuristic now checks `text` / `label` / `placeholder`
  fields of `LocatorSpec`, not just `role_name` and `selector`. Closes the
  safe_mode bypass via `LocatorSpec(text="Sign in")`.

### Polish

- `Recipes.web_research` caches `_rank` scores in a dict before sorting,
  avoiding redundant tokenization per item.
- `SessionManager.create()` now holds the lock across the entire creation
  (context build + UA probe + dict registration) for correctness.
- `SessionManager.close_all()` catches `KeyError` separately and logs at
  DEBUG, so already-closed sessions don't produce misleading "Error
  closing session" warnings during normal teardown.

### Tests

Test count rises from 111 to ~145. New test files:
- `tests/test_security.py` (path traversal, private IP detection)
- `tests/test_exceptions.py` (typed exceptions raised at expected sites)
- `tests/test_safety.py` extended with granular-flag tests + extended
  `_looks_like_submit` checks.

## [1.1.0] - 2026-04-29

Initial high-ROI improvements: correlation IDs, retry policies, safety
controls (basic), debug mode, semantic locators, browser sessions,
high-level recipes (search_and_open_best_result, find_and_download_file,
web_research), MCP server expanded from 5 to 11 tools.

## [1.0.0] - 2026-04-15

Initial release: search, fetch, extract, download, browser automation,
MCP server with 5 tools.
