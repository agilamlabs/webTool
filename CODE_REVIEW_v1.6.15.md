# Brutal full-codebase review — `web_agent` v1.6.15

*Scope: all 34 modules / ~14k LOC. Method: 8 file-disjoint reviewer agents (each reading its files in full), then an adversarial **refute pass** on every critical/high/medium finding — a second agent told to assume the finding is wrong and read the code to disprove it. 49 agents total. Findings that survived the refute pass are "confirmed"; the rest are listed under "Refuted" for transparency.*

## The numbers

| | Raw | After refute pass |
|---|---|---|
| Critical | 1 | **0** (the one "critical" was correctly downgraded to high) |
| High | 12 | **6** |
| Medium | 26 | **15** |
| Low | 34 | **11 confirmed + 34 advisory** |
| **Thrown out as not-real** | — | **7** |

No live RCE, no surviving critical, no secret-exfil-on-the-main-path. The core (the SSRF host gate, the DNS-rebinding TTL cache, the obfuscated-IP normalization, `safe_join_path`, the TabManager lock discipline) is genuinely well-built and has visibly absorbed several prior review passes. *Two* verify agents errored out (StructuredOutput not emitted), so 2 medium+ findings were dropped from the tally — a known gap, not material to the conclusions.

## The one thing to take away

**Almost every confirmed High is the un-fixed identical twin of a bug you already fixed somewhere else.** The prior hardening passes were *point-fixes applied to the exact path named in the finding* — and the same bug class survived untouched in the sibling path that wasn't named:

| You hardened… | …but left the twin open | Confirmed finding |
|---|---|---|
| `fetch` + `_download_httpx` got post-connect peer-IP + per-redirect SSRF checks (C-1b/C-1c) | `fetch_binary`, `classify_url`, and the *swallowed* downloader path did **not** | **FB-1 (high)**, FC-1, DL-1 |
| `save_results` got absolute-path containment (v1.6.14 B-5) | `web_print_page_as_pdf` writes an LLM-supplied **absolute** `output_path` anywhere on disk | **MC-1 (high)** |
| `SkillsConfig`/`WorkspaceConfig` got an `env_prefix` (I-2) | every *other* sub-config still reads **bare** unprefixed env vars → `BLOCK_PRIVATE_IPS=false` silently disables SSRF | **CO-1 (high)** |
| `fetch_many` got a session-path semaphore (C-4) | `web_research` fans out an **unbounded** `gather` with no semaphore | **REC-2 (high)** |
| `api_json` extraction got a size cap (E-6) | binary/HTML extractors return **uncapped** content | CE-1 (medium) |
| `fetch` enforces redirect/rebinding guards | `fill_form_and_extract` drives raw Playwright and **bypasses** them | **REC-1 (high)** |

The lesson for this pass: **fix by bug-class across all call sites, not by line number.** When you patch one of these, grep for every sibling that does the same operation and patch them in the same commit.

## The 6 confirmed Highs — fix these first

1. **CO-1 — config fail-open via bare env vars** (`config.py`). Nested `BaseSettings` sub-configs have no `env_prefix`, so a stray `BLOCK_PRIVATE_IPS=false` / `ALLOW_UPLOAD_OUTSIDE_DOWNLOAD_DIR=true` in the environment silently disables your SSRF and upload fences on a default `AppConfig()`. *Scariest one in the report* — it turns a hardened default into an unhardened one invisibly. (Verifier downgraded critical→high only because it needs attacker-influenced env, not network input.)
2. **FB-1 — `fetch_binary` SSRF/DNS-rebinding gap** (`web_fetcher.py`). The streaming binary path has neither the post-connect peer-IP re-check nor per-redirect `Location` validation that the HTML/download paths got. Re-opens the exact rebinding hole you closed elsewhere.
3. **REC-1 — `fill_form_and_extract` bypasses SSRF guards** (`recipes.py`). Drives a raw Playwright page instead of going through `WebFetcher.fetch`, so the redirect + rebinding re-checks never run for this recipe.
4. **MC-1 — arbitrary file write via `web_print_page_as_pdf`** (`mcp_server.py`). LLM-controlled `output_path` is absolute-capable and uncontained — writes the rendered PDF anywhere the process can. Same class B-5 closed for `save_results`.
5. **GH-1 — GitHub skill query-sanitizer is a no-op** (`builtin_skills/github_release_download`). The comment/docstring claim it strips `site:` and `OR`; the regex strips **neither**, so a prompt-injected input escapes the intended search scope (`site:evil.com`).
6. **REC-2 — `web_research` unbounded fan-out** (`recipes.py`). Skips the per-session semaphore `fetch_many` added, so a large page count opens unbounded concurrent fetches against one session/context.

## Cross-cutting themes (the mediums cluster here)

- **SSRF fence has holes in the *secondary* egress paths** — FB-1, FC-1 (`classify_url` HEAD probe), DL-1 (downloader swallows the SSRF block in a broad `except` and falls through to Playwright), ROBOTS-2 (robots.txt fetch self-checks nothing), REC-1. The main paths are airtight; the side doors aren't.
- **Missing numeric bounds → fail-open or DoS** — CO-2 (deny-list normalization ignores port/IPv6 brackets → entries *never match*), CO-3 (`max_contexts=0` → zero-permit semaphore deadlocks every fetch), CO-4 (negative `max_file_size_mb` deletes every download), BR-3/BR-4 (`KeyboardInput.repeat` / `infinite_scroll_max` unbounded → event-loop pin), CE-1 (uncapped extractor output). A pile of `Field(ge=…, le=…)` annotations closes most of these cheaply.
- **Redaction is partial and fights replay** — AG-2 (skill `inputs`, possibly credentials, written to the audit log unredacted), TRACE-1 (redaction map omits `action.evaluate`, which routinely carries tokens), AG-3/TRACE-2 (redacted traces replay the literal `***REDACTED***` into fill/type fields → silent replay-fidelity break).
- **Crash-the-gate edge case** — UT-1: `socket.getaddrinfo` can raise `UnicodeError` (idna codec), which is a `ValueError`, *not* caught by the `except (gaierror, OSError)` in the SSRF resolver → a crafted hostname crashes the safety check instead of failing closed.
- **Long-lived-server hygiene** — CACHE-1 (DiskCache does all FS I/O synchronously on the event loop — the one module that never got the `to_thread` treatment), and RL-1/ROBOTS-1/TRACE-3 (per-host/per-session dicts never evict → slow leak under an MCP server, fine for CLI).

## What the refute pass threw out (don't chase these)

7 reviewer findings were refuted after reading the code — including a claimed `__eq__`/`__hash__` violation in `_NoCloseContextProxy`, a "navigation ignores timeout" claim, a doctor browser-process leak, and a `prefer_api` uncaught-`RecursionError`. Details + the disproof for each are in the "Refuted" section below, so you don't waste time re-investigating them.

---


## Confirmed — HIGH (6)

#### [REC-1] fill_form_and_extract bypasses the SSRF redirect + DNS-rebinding guards that WebFetcher.fetch enforces

`web_agent/recipes.py:838-945` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **high** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** Unlike every other fetch path in the toolkit, fill_form_and_extract drives Playwright directly: it does a single pre-resolution check_domain_allowed(url) (line 823) and then page.goto(url) (line 840). WebFetcher.fetch is the canonical HTML path and, after page.goto, it (a) re-runs check_domain_allowed on BOTH page.url and response.url to catch 3xx/meta-refresh redirects to a private/denied host (web_fetcher.py 657-669) and (b) runs a post-connect peer-IP check via response.server_addr() to defeat DNS rebinding (web_fetcher.py 671-686, _response_peer_is_private). BrowserManager.new_page/new_context only install a resource-TYPE route filter (browser_manager.py 703-709), no private-IP enforcement -- so the SSRF defense lives ONLY in WebFetcher.fetch. fill_form_and_extract has neither re-check. A public/allow-listed host that 302-redirects to http://169.254.169.254/... (cloud metadata) or rebinds its DNS to an RFC1918 address after the host-level check is followed by Chromium with no second gate; the post-submit page content (potentially internal data) is then extracted and returned. This is a real SSRF/rebind hole reachable from a public recipe (Agent.fill_form_and_extract -> MCP tool).

**Evidence.**

```
        if not check_domain_allowed(url, self._config.safety):
            ...
        async def _drive(page: Page) -> ExtractionResult:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            ...
            html, html_source = await safe_page_content(page)
            final_url = page.url
            ... fr = FetchResult(url=url, final_url=final_url, status=FetchStatus.SUCCESS, html=html, ...)
```

**Suggested fix.** After the navigation settles (and before extracting), repeat the WebFetcher.fetch defenses: (1) re-run check_domain_allowed on page.url AND on the navigation Response.url, returning extraction_method='none' (or raising) on mismatch; (2) when safety.block_private_ips is set, capture the goto Response and call _response_peer_is_private(response) (import from web_fetcher) and reject a private peer. Simplest robust fix: have fill_form_and_extract obtain HTML through WebFetcher rather than a raw page.goto, or factor the post-navigation SSRF re-check in WebFetcher into a shared helper and call it here.

**Verifier (refute pass).** Verified against the actual code; the finding is accurate on all three axes.

EXISTS: recipes.py:823 does a single pre-navigation check_domain_allowed(url). recipes.py:840 does `await page.goto(url, ...)` and DISCARDS the Response (no assignment). Between goto and content capture (lines 840-945) there is NO second check_domain_allowed on page.url/response.url and NO _response_peer_is_private call. Grep confirms _response_peer_is_private exists ONLY in web_fetcher.py (def at 254, used at 677) — it is never imported into recipes.py. safe_page_content (utils.py:274+) is a pure content-capture helper that does no URL/IP validation.

CANONICAL PATH ENFORCES WHAT THIS BYPASSES: WebFetcher.fetch re-runs check_domain_allowed on BOTH page.url and response.url after goto (web_fetcher.py:655-669) and calls _response_peer_is_private(response) for the post-connect DNS-rebind check when block_private_ips is set (web_fetcher.py:676-686). fill_form_and_extract has neither.

NO NEUTRALIZING GUARD: BrowserManager._build_context (browser_manager.py:685-711) only installs a resource-TYPE route filter (resource_type in blocked), no private-IP enforcement. check_domain_allowed (utils.py:647-697) only validates the host of the URL passed to it against cached state — it rejects a literal 169.254.169.254 (lines 684-686) but cannot see a post-navigation 302/meta-refresh redirect or a DNS rebind to RFC1918/IMDS.

REACHABLE: block_private_ips defaults True (config.py:695) so the bypassed guards are meant to be active; allowed_domains defaults empty (config.py:689) so arbitrary public hosts pass the pre-check (utils.py:692-693). Publicly exposed: mcp_server.py:466-489 (web_fill_form_and_extract) -> agent.py:1492-1509 (thin pass-through, no re-check) -> recipes.py:790. Both the session_id branch (recipes.py:950-957) and default branch (958-960) funnel through the same unguarded _drive().

Threat-model nuance (does not refute): exploitation requires the recipe to be pointed at an attacker-influenced URL or a benign host with attacker-controlled redirect/DNS — but that is precisely the model WebFetcher.fetch's own comments (web_fetcher.py:646-648, 671-675) declare in-scope, so the inconsistency is a genuine deliberate-guard bypass reaching cloud metadata/internal services and returning their content. High is correct.

---

#### [REC-2] web_research fans out fetch_smart with an unbounded gather and skips the session-path semaphore fetch_many added (C-4)

`web_agent/recipes.py:601-608` &nbsp;·&nbsp; _concurrency_ &nbsp;·&nbsp; raw **high** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** web_research builds targets = ranked[:max_pages] and then fetch_tasks = [fetch_smart(r.url, session_id=session_id) for r in targets] and asyncio.gather()s them all at once. On the session path, fetch_smart -> fetch -> _do_fetch creates pages via ctx.new_page() directly (web_fetcher.py 576-592), which BYPASSES BrowserManager's context semaphore. fetch_many was given a per-call asyncio.Semaphore(max_pages_per_session_fetch) precisely because 20+ concurrent ctx.new_page() calls on one BrowserContext 'reproducibly crash Chromium's renderer' (web_fetcher.py 824-853, the C-4 fix). web_research does NOT route through fetch_many, so it gets none of that gating -- the inline comment 'bounded by BrowserManager semaphore inside fetcher' is wrong for the session path. Compounding it, the library API does not clamp max_pages (the min(max(max_pages,1),50) / depth clamp exists ONLY in mcp_server.py:452-453, not in Agent.web_research or Recipes.web_research), and max_pages also drives search(max_results=max(max_pages*2,10)) at line 533. A direct Python caller (the documented public API) passing a large max_pages with a session_id triggers exactly the renderer-crash fan-out C-4 was built to prevent.

**Evidence.**

```
        targets = ranked[:max_pages]
        ...
        fetch_tasks = [self._fetcher.fetch_smart(r.url, session_id=session_id) for r in targets]
        fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
```

**Suggested fix.** Gate the fan-out the same way fetch_many does: wrap each fetch_smart in an asyncio.Semaphore(self._config.browser.max_pages_per_session_fetch) when session_id is set (and ideally always cap concurrency), and clamp max_pages/depth inside Recipes.web_research (or Agent.web_research) so the safety bound is enforced for the library API, not only the MCP wrapper.

**Verifier (refute pass).** Confirmed real and reachable, no neutralizing guard on the library path. (a) Offending code exists: recipes.py:600-608 builds targets=ranked[:max_pages] then asyncio.gather()s [fetch_smart(r.url, session_id=session_id) for r in targets] with no semaphore; the inline comment "bounded by BrowserManager semaphore inside fetcher" (lines 603-604) is wrong for the session path. (b) Call chain is real: fetch_smart (web_fetcher.py:359) -> fetch (406) -> _do_fetch (552); the session branch at web_fetcher.py:576-592 calls ctx.new_page() directly with NO semaphore. The only two semaphores in the package are BrowserManager._semaphore (browser_manager.py:161, gates ONLY the ephemeral new_page() context manager) and the per-call asyncio.Semaphore INSIDE fetch_many (web_fetcher.py:843-857). web_research never routes through fetch_many, so it gets neither. (c) No clamp elsewhere defeats it: the min(max(max_pages,1),50)/depth clamp exists ONLY in mcp_server.py:452-453; Agent.web_research (agent.py:1545-1553) passes max_pages straight through and Recipes.web_research (recipes.py:484-493) has no clamp, so a direct Python caller of the documented public API with a large max_pages + session_id hits the uncapped fan-out. The per-host rate limiter (rate_limiter.py:63-87, async with self._locks[host]) serializes per-host only -- research hits distinct hosts, so it does not cap the fan-out. The C-4 fix's own docs (web_fetcher.py:824-834, config.py:110-118) state 20+ concurrent ctx.new_page() on one BrowserContext "reproducibly crash Chromium's renderer," which this code reproduces exactly. Notably even the MCP path is only clamped to <=50, still above the ~20 empirical crash threshold. Minor: the finding's line cites (576-592 vs exact 552 def, 824-853 vs 849) are slightly off but the cited code is substantively correct. Severity high is justified; only mitigants are the session_id precondition and MCP-side clamp limiting blast radius.

---

#### [CO-1] Nested BaseSettings sub-configs read BARE unprefixed env vars -> SSRF/upload fences silently disabled by stray env vars

`web_agent/config.py:87, 658-695, 1179-1185` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **critical** -> confirmed **high** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** Every sub-config (SafetyConfig line 658, BrowserConfig line 87, DownloadConfig line 597, SearchConfig line 471, ...) subclasses pydantic-settings BaseSettings but only AppConfig (line 1174) and SkillsConfig (line 939) declare an env_prefix. A BaseSettings with no env_prefix reads its fields from BARE, unprefixed environment variables. Because AppConfig builds each sub-config via default_factory (lines 1179-1194), the default `AppConfig()` path — the documented happy path `async with Agent() as agent:` — instantiates each sub-config, which then silently pulls bare env vars. Reproduced: with only `BLOCK_PRIVATE_IPS=false` in the environment, `AppConfig().safety.block_private_ips` is False — SSRF protection (RFC1918/loopback/AWS IMDS) is OFF with no error and no log. Same for `ALLOW_UPLOAD_OUTSIDE_DOWNLOAD_DIR=true` (arbitrary-file exfiltration fence dropped), `SAFE_MODE`, `ALLOW_DOWNLOADS`, `HEADLESS`, `ALLOWED_EXTENSIONS=[".exe"]`. The README + module docstring (lines 10-14) promise the prefix is `WEB_AGENT_`, so operators have no reason to expect bare `BLOCK_PRIVATE_IPS`/`REGION`/`LANGUAGE`/`HEADLESS` (all plausible in CI/Docker/shells) to flip security settings. The team already fixed this exact class for SkillsConfig (comment lines 934-939, review I-2) and WorkspaceConfig (lines 963-965) but missed every other sub-config.

**Evidence.**

```
class SafetyConfig(BaseSettings):
    ...
    block_private_ips: bool = True
    allow_upload_outside_download_dir: bool = False
    # no model_config / env_prefix anywhere in this class

# vs SkillsConfig, which got the fix:
    model_config = {"env_prefix": "WEB_AGENT_SKILLS__"}
```

**Suggested fix.** Give every BaseSettings sub-config an explicit, namespaced env_prefix mirroring AppConfig's nesting, e.g. SafetyConfig -> model_config = {"env_prefix": "WEB_AGENT_SAFETY__"}, BrowserConfig -> WEB_AGENT_BROWSER__, DownloadConfig -> WEB_AGENT_DOWNLOAD__, etc. (matching the WEB_AGENT_<SECTION>__ pattern AppConfig already uses). Alternatively, make the sub-configs plain pydantic BaseModel rather than BaseSettings so only AppConfig owns env parsing — they are never instantiated standalone in production. Add a regression test asserting a bare `BLOCK_PRIVATE_IPS=false` does NOT affect `AppConfig().safety.block_private_ips`.

**Verifier (refute pass).** CONFIRMED and reproduced empirically. Structural claim verified via Grep on config.py: SafetyConfig (L658), BrowserConfig (L87), DownloadConfig (L597), SearchConfig (L471), FetchConfig, ExtractionConfig, AutomationConfig, DebugConfig, AuditConfig, CacheConfig, WorkspaceConfig, DiagnosticsConfig all subclass pydantic-settings BaseSettings with NO env_prefix; only SkillsConfig (model_config L939 "WEB_AGENT_SKILLS__") and AppConfig (L1174 "WEB_AGENT_") declare one. AppConfig builds every sub-config via default_factory (L1179-1194), the exact path behind the documented `async with Agent() as agent:`. Empirical repro on pydantic-settings 2.6.1: `BLOCK_PRIVATE_IPS=false python -c "AppConfig().safety.block_private_ips"` returns False (SSRF protection off); baseline with no env returns True. Also confirmed `ALLOW_UPLOAD_OUTSIDE_DOWNLOAD_DIR=true` -> allow_upload_outside_download_dir=True (drops the arbitrary-file exfiltration fence the L721-726 docstring exists to provide), `ALLOW_JS_EVALUATION=true` -> True, `HEADLESS=false` -> False. These flags are the real enforcement gates: web_fetcher.py:676 (`if getattr(...,'block_private_ips',False) and ...` post-connect rebind guard), utils.py:684, downloader.py:339, browser_actions.py:1433 (upload fence), 348/1158/1189 (JS eval). NO neutralizing guard: _apply_safe_mode (config.py:785-809) EXPLICITLY leaves block_private_ips untouched (L789 "intentionally NOT touched"), and safe_mode itself defaults False (and is equally flippable by bare SAFE_MODE). The README/module docstring (L10-14) promise the prefix is WEB_AGENT_, so operators have no reason to expect bare names to take effect; the team already fixed this exact class for SkillsConfig (L934-939, "review I-2") and dodged it for WorkspaceConfig (L963-965, renaming path->workspace_dir to avoid PATH) but missed all others. Downgrading critical->high only because exploitation requires control over (or an unlucky collision in) the bare process environment rather than being remotely triggerable; the silent, no-log bypass of precisely the SSRF + file-exfiltration fences that contain prompt-injection keeps it well above medium. Reviewer's evidence is accurate; the only slightly-overstated bit is ALLOWED_EXTENSIONS=[".exe"] (lists need JSON string form), which does not affect the boolean-flag core.

---

#### [FB-1] fetch_binary has no post-connect peer-IP check and no per-redirect validation (SSRF / DNS-rebinding gap closed everywhere else)

`web_agent/web_fetcher.py:1082-1167` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **high** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** fetch_binary streams via httpx with follow_redirects=True but (a) never re-checks the actual connected peer IP, and (b) only validates the FINAL resp.url against check_domain_allowed -- intermediate redirect hops are never validated. The sibling Playwright path (fetch, lines 671-686 via _response_peer_is_private) and the sibling httpx download path (downloader._download_httpx, lines 332-346 via _httpx_peer_ip + lines 314-323 per-redirect event hook) BOTH close these holes; fetch_binary closes neither. check_domain_allowed relies on the 30s-TTL cached DNS in utils._resolve_host_addresses, so a host that resolved public at gate time can rebind to 169.254.169.254 / RFC1918 / loopback before the actual TCP connect, and httpx will connect+stream from it. Likewise a whitelisted host can 302 -> http://169.254.169.254/... -> 302 -> whitelisted: httpx connects to the internal hop to follow it, and only the benign final URL is checked. fetch_binary is reached on every binary document fetch (Agent.search_and_extract(extract_files=True), fetch_smart binary routing, recipes.web_research), so this is squarely on a real path. is_private_address is even imported into this module but is only used by the HTML path.

**Evidence.**

```
async with httpx.AsyncClient(
    follow_redirects=True,
    timeout=binary_timeout_s,
    headers={"User-Agent": get_random_user_agent()},
    cookies=cookie_jar,
) as client:
    async with client.stream("GET", url) as resp:
        final_url = str(resp.url)
        if final_url != url and not check_domain_allowed(
            final_url, self._config.safety
        ):
            return FetchResult(... BLOCKED ...)
        # <-- no event_hooks={"response": [_check_redirect]}, no _httpx_peer_ip()/is_private_address() guard
```

**Suggested fix.** Mirror downloader._download_httpx exactly: (1) pass event_hooks={"response": [_check_redirect]} that rejects any 3xx whose Location fails check_domain_allowed; (2) after the stream opens, when getattr(safety,'block_private_ips',False) is True, read the real peer via the same _httpx_peer_ip helper (move it to utils or import it) and refuse if is_private_address(peer_ip). Factor the shared SSRF-egress guard into one helper so a future path can't drift again.

**Verifier (refute pass).** CONFIRMED on all three claims after adversarial review. (1) Code exists as described: web_fetcher.py:1082-1100 opens httpx.AsyncClient(follow_redirects=True) with NO event_hooks and, after client.stream, validates ONLY final_url via check_domain_allowed (lines 1089-1092). No _httpx_peer_ip / is_private_address post-connect check anywhere in the full fetch_binary body (1009-1185). The only SSRF gate is the pre-connect check_domain_allowed(url) at line 1044, which uses the 30s-TTL cached DNS (utils._resolve_host_addresses) and the initial URL only. (2) Both sibling paths DO close both holes: Playwright fetch re-checks the real peer via _response_peer_is_private (web_fetcher.py:676-686) and httpx _download_httpx uses BOTH event_hooks={'response':[_check_redirect]} for per-redirect validation (downloader.py:314-328) AND _httpx_peer_ip + is_private_address post-connect (downloader.py:339-346). (3) The cached-DNS gate is provably insufficient by the codebase's OWN comments: utils.py:531-533 'This is a *mitigation*, not a full close: the authoritative defenses are the post-connect peer-IP re-checks in web_fetcher / downloader (C-1b / C-1c)' and 547-552. So the one guard fetch_binary has is explicitly documented as inadequate against DNS rebinding. Reachability is real: fetch_binary is hit by fetch_smart binary routing (web_fetcher.py:403), Agent.search_and_extract(extract_files=True) (agent.py:591, iterating over search-result file URLs), and recipes.web_research (recipes.py:351,605) — URLs that are attacker-influenceable via search ranking/redirects, not pre-vetted. block_private_ips defaults to True (config.py:695), so the protection the siblings gate on is active by default, yet fetch_binary never performs it. The reviewer's note that is_private_address is imported into web_fetcher.py (line 43) but used only by the HTML path (line 286 via _response_peer_is_private) is also accurate. Two attack vectors are both live: DNS-rebind to 169.254.169.254/RFC1918/loopback within the TTL window before the TCP connect, and a whitelisted host issuing 302->http://169.254.169.254/...->302->whitelisted where httpx connects to the internal hop but only the benign final URL is checked. Severity high is fair and internally consistent — the sibling fixes (C-1b/C-1c) were treated as criticals in the v1.6.14 hardening slice; this is the one streamed-fetch path that was missed, a genuine defense-in-depth SSRF/DNS-rebinding gap on a real path. No neutralizing guard exists elsewhere.

---

#### [MC-1] web_print_page_as_pdf exposes LLM-controlled output_path that escapes the screenshot dir (arbitrary file write)

`web_agent/mcp_server.py:1005-1023` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **high** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** The web_print_page_as_pdf MCP tool forwards an LLM-supplied output_path straight to agent.print_page_as_pdf. Unlike the screenshot path (Agent/BrowserActions.take_screenshot always routes caller paths through safe_join_path at browser_actions.py:750, which contains even absolute paths), the PDF path explicitly BYPASSES containment for absolute paths: browser_actions.py:1706-1709 does `Path(output_path).resolve() if _is_cross_platform_absolute(output_path) else safe_join_path(...)` and then `await page.pdf(path=str(resolved))` with no allow-list/containment check and no SafetyConfig gate. A prompt-injection that reaches this tool can write a rendered PDF to ANY path the process can write (e.g. an autostart/cron location, ~/.config, a web-served directory). The mcp_server.py docstring ("Output path defaults to automation.screenshot_dir.") actively misrepresents the behavior and gives the model no signal that an absolute path escapes — this is the untrusted-input entry point, so the omission matters. Note the asymmetry with web_screenshot, whose `path` IS safely contained, making this almost certainly an unintended gap rather than a deliberate capability.

**Evidence.**

```
    """Render the current page (or ``url``) as PDF via Chromium's
    ``page.pdf()``. Output path defaults to ``automation.screenshot_dir``.
    """
    agent: Agent = ctx.request_context.lifespan_context["agent"]
    r = await agent.print_page_as_pdf(
        url=url,
        output_path=output_path,
        session_id=session_id,
        tab_id=tab_id,
    )
```

**Suggested fix.** Make the PDF path containment match the screenshot path: route output_path through safe_join_path(shot_dir, output_path) unconditionally in browser_actions.print_page_as_pdf (drop the absolute-path bypass), OR gate absolute output_path behind an explicit safety flag analogous to allow_upload_outside_download_dir. At minimum, update the mcp_server.py docstring to state that absolute output_path values write outside the screenshot dir and are accepted from the model verbatim, so operators understand the exposure.

**Verifier (refute pass).** CONFIRMED real and reachable with no neutralizing guard. browser_actions.py:1706-1710 resolves an LLM-supplied output_path via `Path(output_path).resolve()` when `_is_cross_platform_absolute(output_path)` is true, deliberately bypassing `safe_join_path`, then writes via `await page.pdf(path=str(resolved))` at line 1712. The absolute-path detector (utils.py:710-729: Windows-drive, UNC, POSIX `/`, pathlib fallback) reliably routes any absolute path into the bypass branch, and safe_join_path (utils.py:759-760) is exactly the function that would otherwise reject absolutes. Reachability is end-to-end and unguarded: web_print_page_as_pdf (@mcp.tool, mcp_server.py:1005) -> agent.print_page_as_pdf (agent.py:1416, pure passthrough) -> _actions.print_page_as_pdf (browser_actions.py:1646). The ONLY safety check on this path is check_domain_allowed (line 1683), which constrains the source page, not the write destination. No SafetyConfig flag, allow-list, or safe_mode covers the output path (config.py:658-726; safe_mode only flips allow_js/downloads/form_submit). The asymmetry with screenshots is verified: take_screenshot unconditionally routes caller paths through safe_join_path and returns FAILED on absolutes (browser_actions.py:743-759). The project even has the exact gating pattern for filesystem access from untrusted input — upload_file refuses paths outside download_dir unless allow_upload_outside_download_dir=True and logs a WARNING otherwise (browser_actions.py:1433-1459; config.py:721-726 documents the prompt-injection exfil threat) — yet the PDF *write* primitive has no equivalent fence, confirming this is an unintended gap. Docstring misrepresentation is real: mcp_server.py:1014 ('defaults to screenshot_dir') and browser_actions.py:1656-1657 ('Output path goes through safe_join_path') both falsely imply containment; only agent.py:1426 is honest. One reviewer imprecision: the '~/.config' example would NOT work literally (~ is neither expanded nor absolute, so it gets contained by safe_join_path), but literal absolute paths (/home/user/.config/autostart/x.desktop, C:\\Users\\...\\Startup\\x) write directly — bug stands. Severity high (not critical) because the written bytes are a Chromium-rendered PDF of an attacker-influenced page, not arbitrary content, which constrains payload crafting somewhat; but the destination is fully attacker-controlled with no opt-in, enabling overwrite of config/data files or planting PDFs in autostart/web-served dirs.

---

#### [GH-1] Query sanitizer does NOT strip `site:` or `OR` despite comment/docstring claiming it does — search-scope escape via prompt-injected inputs

`web_agent/builtin_skills/github_release_download/__init__.py:18-55` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **high** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** The module comment (lines 18-21) and the run() comment (lines 41-45) both assert that _sanitize_query_term blocks `site:`, `OR`, quotes, and parens, and that under default-open SafetyConfig the composed query scope (`site:github.com`) is 'the only fence'. But the regex `[\"\'()\[\]|]` removes only quotes, parens, brackets, and pipe — it does NOT remove the literal token `site:` (the `:` and letters are not in the class), nor the bareword `OR`, nor spaces. So a prompt-injected `asset_pattern`, `tag`, or `repo` such as `winx64 site:evil.com` or `x OR site:evil.com` survives sanitization verbatim and is concatenated into the query (lines 53/55), producing e.g. `site:github.com owner/name releases latest winx64 site:evil.com`. Search engines treat the extra `site:` as an additional scope, so attacker-controlled hosts surface in results. find_and_download_file then runs check_domain_allowed, but with the default empty allow-list a PUBLIC attacker host passes (only private IPs are blocked by default) — so the skill can be steered to download from an arbitrary attacker-controlled public domain, defeating its github.com confinement. The comment is also a type/contract lie: it names two operators it does not actually strip.

**Evidence.**

```
_QUERY_OPERATOR_CHARS = re.compile(r"[\"\'()\[\]|]")


def _sanitize_query_term(s: str) -> str:
    """Strip search-operator metacharacters from a user-supplied term."""
    return _QUERY_OPERATOR_CHARS.sub("", s).strip()
...
        query = f"site:github.com {repo} releases latest {asset_pattern}".strip()
```

**Suggested fix.** Sanitize the actual operators the comment promises: collapse/remove the `site:` keyword (e.g. regex `(?i)\bsite\s*:` -> ''), drop the bareword boolean operators (`\b(OR|AND|NOT)\b`), and strip the colon. Better, do not rely on string sanitization at all: pin the result domain after search by rejecting any candidate whose host is not exactly `github.com`/`*.github.com`/`*.githubusercontent.com` before calling find_and_download_file, independent of SafetyConfig. Also correct the comment to state precisely what is and isn't stripped.

**Verifier (refute pass).** Verified against the actual code. (1) The sanitizer regex at __init__.py:22 `_QUERY_OPERATOR_CHARS = re.compile(r"[\"\'()\[\]|]")` matches only quotes/parens/brackets/pipe. Empirically tested: `_sanitize_query_term('winx64 site:evil.com')` -> 'winx64 site:evil.com' and `'x OR site:evil.com'` -> 'x OR site:evil.com' — both pass UNCHANGED. So `site:` and the bareword `OR` are NOT stripped, contradicting the comments at lines 18-21 and 41-45 that explicitly name them as blocked. The comment is a genuine contract lie. (2) Reachability is real: run() sanitizes repo/tag/asset_pattern (lines 46-48) then concatenates them verbatim into the query (lines 53/55) and calls find_and_download_file. In recipes.py:363-466 that query is passed verbatim to the search engine (line 401); the ONLY host filter on candidate URLs is `check_domain_allowed(url, self._config.safety)` (lines 407 and 430) — there is NO independent pin to github.com — and `candidates[0]` is downloaded (line 466). A surviving injected `site:evil.com` adds a competing search scope so an evil.com URL can become the chosen candidate. (3) No neutralizing guard: SafetyConfig.allowed_domains defaults to an empty list (config.py:689, default_factory=list; docstring line 661 'Empty allowed_domains means all hosts are allowed'). check_domain_allowed at utils.py:692-693 returns True immediately when allowed_domains is empty; block_private_ips (default True) only blocks RFC1918/loopback/link-local, NOT a public attacker host. So under the documented default-open config a public attacker domain passes, defeating the skill's github.com confinement. Severity calibrated to high (not critical): exploitation requires both attacker-controlled skill inputs (prompt injection) AND the default-open allow-list, plus the search engine honoring the injected site: scope and ranking the hostile URL into the first matching candidate — real preconditions, but a genuine security-boundary bypass compounded by a false hardening claim. The suggested fix (strip `(?i)\bsite\s*:` and `\b(OR|AND|NOT)\b`, and/or pin candidate hosts to github.com/*.github.com/*.githubusercontent.com independent of SafetyConfig) is the correct remedy.

---

## Confirmed — MEDIUM (15)

#### [AG-1] search_and_extract HEAD-probe gather swallows asyncio.CancelledError as 'default to HTML'

`web_agent/agent.py:559-565` &nbsp;·&nbsp; _concurrency_ &nbsp;·&nbsp; raw **high** -> confirmed **medium** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** The extensionless-URL probe uses asyncio.gather(*probe_tasks, return_exceptions=True) and then treats any BaseException result as 'probe failed -> default to HTML' (isinstance(classification, BaseException) at line 563). Since Python 3.8, asyncio.CancelledError is a BaseException (not Exception), and gather(return_exceptions=True) returns a CancelledError of a child task AS A RESULT OBJECT rather than propagating it. So if the caller cancels search_and_extract while the probe gather is in flight, the cancellation is caught here, the item is silently appended to page_items, and the whole pipeline keeps running -- continuing to fetch_many, extract, and hold browser resources after the caller asked to stop. This is the exact bug web_research was explicitly hardened against (recipes.py 614-621, the E-4 fix re-raises CancelledError); search_and_extract was left with the unsafe pattern.

**Evidence.**

```
                probe_results: list[Any] = await asyncio.gather(
                    *probe_tasks, return_exceptions=True
                )
                for item, classification in zip(unknown_items, probe_results, strict=True):
                    if isinstance(classification, BaseException):
                        # Probe failure -> default to HTML, will be caught downstream
                        page_items.append(item)
```

**Suggested fix.** Mirror the web_research E-4 fix: inside the loop, `if isinstance(classification, asyncio.CancelledError): raise classification` before the generic BaseException branch (or narrow the generic branch to `except Exception`-equivalent by checking `isinstance(classification, Exception)` and re-raising anything else).

**Verifier (refute pass).** Offending code exists verbatim. agent.py:559-561 does `await asyncio.gather(*probe_tasks, return_exceptions=True)`, and agent.py:562-565 does `for item, classification in zip(...): if isinstance(classification, BaseException): page_items.append(item)`. Since asyncio.CancelledError subclasses BaseException (not Exception) in Python 3.8+, and gather(return_exceptions=True) returns a child task's CancelledError as a RESULT object (not propagated), a cancellation arriving while the HEAD-probe gather is in flight is swallowed here: the item is appended to page_items and the pipeline proceeds to fetch_many (agent.py:653), extraction, and continues holding browser resources after the caller asked to stop.\n\nReachable in real use: search_and_extract is a public `async def` (agent.py:387). The probe branch is entered under ordinary runtime conditions — `unknown_items` non-empty AND `self._config.safety.probe_binary_urls` true (agent.py:554). classify_url (web_fetcher.py:860) is a genuine awaitable doing a HEAD network probe, so child tasks are legitimately in-flight and cancellable.\n\nNo neutralizing guard: Grep confirms `CancelledError` appears nowhere in agent.py. The only wrapper is _call_scope (agent.py:357-370) = correlation_scope() + audit.scope(), neither of which suppresses or re-raises exceptions; there is no try/except around the gather. So nothing converts/re-raises the swallowed CancelledError.\n\nThe asymmetry the reviewer cites is real: recipes.py:613-621 (web_research, E-4 fix) explicitly does `if isinstance(fr, asyncio.CancelledError): raise fr` BEFORE its generic `isinstance(fr, BaseException)` branch (recipes.py:622), with a comment describing this exact hazard. search_and_extract was left with the unsafe pattern.\n\nSeverity: downgraded high -> medium. The defect is genuine but the window is narrow — cancellation must land during the short HEAD-probe gather specifically (a small slice of the pipeline; the heavier fetch_many at line 653 is a separate await unaffected by this snippet). It degrades cancellation responsiveness (one extra round of fetches + transient resource hold) rather than corrupting data or producing wrong results on the normal path. Real concurrency bug and a real inconsistency with the project's own E-4 invariant, worth fixing by mirroring recipes.py, but impact is moderate, not high.

---

#### [AG-2] apply_domain_skill writes raw skill `inputs` (possible credentials) to the audit log unredacted

`web_agent/agent.py:1205-1208` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **medium** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** apply_domain_skill passes the caller-supplied free-form `inputs` dict straight into the audit scope: _call_scope('apply_domain_skill', {'url': url, 'name': name, 'inputs': inputs}). AuditLogger.scope persists args verbatim -- `"args": dict(args) if args else {}` (audit.py:94) -- with NO redaction, then json.dumps it to the append-only audit.jsonl when audit.enabled is True. Domain skills are exactly the place authenticated/login flows live, so `inputs` routinely carries usernames, passwords, API keys, or session tokens. The trace subsystem was deliberately hardened to redact such secrets (trace_recorder.py _redact_args for action.fill/type/type_text), but the audit path for skill inputs was not given the same treatment -- an asymmetric secret-to-disk leak. (Lower-grade variants: select_dropdown logs raw value/label at agent.py 1262-1269; handle_dialog correctly omits prompt_text and type_text correctly logs only length, so the pattern of redaction is inconsistent across these wrappers.)

**Evidence.**

```
        async with self._call_scope(
            "apply_domain_skill", {"url": url, "name": name, "inputs": inputs}
        ):
            return await self._skills.apply(self, url, name, inputs or {})
```

**Suggested fix.** Do not pass raw `inputs` to the audit scope. Either log a redacted/summarized form (e.g. {'input_keys': sorted(inputs or {})} or run inputs through a redactor that masks values for keys matching password/token/secret/key), or have AuditLogger apply a central redaction pass over args before writing. Align with trace_recorder._redact_args so audit and trace have the same secret-handling guarantees.

**Verifier (refute pass).** Confirmed at the code level. agent.py:1205-1208 passes the raw caller-supplied `inputs` dict into `_call_scope("apply_domain_skill", {"url": url, "name": name, "inputs": inputs})`. `_call_scope` (agent.py:368-370) forwards args unchanged to `self._audit.scope(method, args)`. AuditLogger.scope persists them verbatim -- `"args": dict(args) if args else {}` (audit.py:94) -- then json.dumps the entry to append-only audit.jsonl (audit.py:110-116). NO redaction on the audit path. The asymmetry claim is verified: trace_recorder.py:73-85 (_redact_args) masks secret values for action.fill/type/type_text (map at lines 66-70, applied at 181), but the audit path has no equivalent, so the same codebase redacts secrets in the trace sink but not the audit sink. `inputs` is genuinely free-form (dict[str, Any], agent.py:1191); validate_inputs (domain_skills.py:278-309) only type-coerces/checks required keys and passes through up to MAX_EXTRA_INPUTS=20 extra keys -- it never redacts values. Reachable from the MCP server tool (mcp_server.py:846-863, remote `inputs: Optional[dict]`), the CLI (main.py:179-183, --inputs JSON), and the public Agent API; domain skills are by design where authenticated/login flows live, so credentials in `inputs` are an expected use case, not hypothetical. Severity stays medium (not higher) because the leak is gated behind an opt-in: AuditLogger defaults enabled=False (audit.py:57) and scope() is a no-op when disabled (audit.py:85-87); the data lands in a local file, not transmitted off-host. No guard anywhere neutralizes the audit path when auditing is on. Reviewer's framing, asymmetry, and suggested fix are all accurate.

---

#### [AG-3] replay_trace re-injects the literal '***REDACTED***' string for fill/type actions

`web_agent/agent.py:1650-1658` &nbsp;·&nbsp; _correctness_ &nbsp;·&nbsp; raw **medium** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** replay_trace reconstructs actions from the JSONL written by SessionTraceRecorder.record(). But record() stores _redact_args(method, args), which replaces FillInput.value / TypeInput.text / TypeTextInput.text with the placeholder '***REDACTED***' (trace_recorder.py 66-85, 181). replay_trace reads those redacted args back (load_entries) and rebuilds the Action via TypeAdapter, so replaying a recorded login/form sequence will literally type the string '***REDACTED***' into the username/password/search field instead of the original value. The replay therefore silently does the wrong thing for any sequence containing fill/type actions -- it neither reproduces the original input nor signals that the value is unavailable. This is a footgun: the feature is advertised as 're-execute the action list recorded' but cannot faithfully replay the most common interactive actions.

**Evidence.**

```
            for e in action_entries:
                args = dict(e.get("args") or {})
                # Rebuild the discriminator -- audit args dropped it during
                # exclude=. The method tail IS the action name.
                args["action"] = str(e["method"]).removeprefix("action.")
                action_dicts.append(args)
            adapter = TypeAdapter(list[Action])
            actions = adapter.validate_python(action_dicts)
```

**Suggested fix.** Decide and document the contract: either (a) detect the redaction sentinel when rebuilding fill/type actions and raise a clear error / skip with a warning so the caller knows the value can't be replayed, or (b) keep secrets out of the replay path entirely and require the caller to re-supply sensitive values. Silently typing '***REDACTED***' should not be an outcome.

**Verifier (refute pass).** Verified the full chain end-to-end and found no neutralizing guard. RECORD side: trace_recorder.py:66-70 maps action.fill->value, action.type->text, action.type_text->text; _redact_args (73-85) overwrites that key with "***REDACTED***"; line 181 applies it to the serialized entry. browser_actions.py:510-519 confirms record() runs for every executed action with method=f"action.{action_input.action}" and args=model_dump(...), so a real fill/type secret is genuinely written as the placeholder. REPLAY side: agent.py:1651-1658 reads args verbatim from load_entries(), re-adds the discriminator, and rebuilds Action via TypeAdapter -- no sentinel detection. GUARD CHECK: a project-wide grep shows _REDACTED/_redact_args exist ONLY on the recording side; nothing in replay_trace, execute_sequence, or models inspects for it. models.py confirms FillInput.value (700), TypeInput.text (689), TypeTextInput.text (842) are plain required str, so "***REDACTED***" validates cleanly and is typed into the field. The reviewer's snippet and description match the code exactly. So replaying any trace containing fill/type/type_text silently types the literal placeholder -- neither reproducing the input nor signaling unavailability. Severity stays medium (not higher): the trace feature is opt-in (DiagnosticsConfig.trace_enabled defaults False, config.py), only fill/type-family actions are affected (clicks/navigation replay fine), and the redaction itself is correct/intentional -- the defect is the missing replay-side contract, a correctness footgun rather than a security or data-loss bug.

---

#### [BR-3] KeyboardInput.repeat is unbounded — a single hostile/LLM-supplied action pins the event loop with N keypress round-trips

`web_agent/browser_actions.py:1104-1112` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; raw **high** -> confirmed **medium** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** _do_keyboard loops `for _ in range(action.repeat)` issuing one awaited CDP keyboard.press per iteration. KeyboardInput.repeat (models.py line 784) is `int = Field(default=1)` with NO ge/le bound, and _do_keyboard does not consult _resolve_timeout or any BudgetTracker (the budget tracker is never wired into execute_action). The codebase's own threat model treats action sequences as LLM/prompt-injection-controlled (see the allow_js_evaluation and upload-path comments). An action with repeat=100000000 therefore drives an unbounded sequence of awaited IPC round-trips, monopolising the single Playwright connection / event loop for the whole agent process — a trivial denial-of-service from a value that passes all validation. The same applies to PRESS_KEY combos via repetition at the sequence level, but repeat makes it a one-liner.

**Evidence.**

```
    async def _do_keyboard(self, page: Page, action: KeyboardInput) -> ActionResult:
        for _ in range(action.repeat):
            await page.keyboard.press(action.key)
```

**Suggested fix.** Bound repeat at the model (e.g. Field(default=1, ge=1, le=100)) and clamp defensively in _do_keyboard. More generally, enforce a per-action wall-clock deadline (wrap the loop in asyncio.wait_for using _resolve_timeout) so any repeat-style handler cannot run away.

**Verifier (refute pass).** Offending code confirmed verbatim. models.py:784 `repeat: int = Field(default=1, description=...)` has NO ge/le bound; BaseAction (models.py:646-667) defines no int-bounding validator. browser_actions.py:1105-1106 loops `for _ in range(action.repeat): await page.keyboard.press(action.key)` with no clamp. Reachable: dispatch wires ActionType.KEYBOARD:_do_keyboard (line 1933); KeyboardInput is in the public Action union reachable via execute_action/execute_sequence/execute_single_on_session. No neutralizing guard: execute_sequence's pre-flight (lines 347-380) inspects only EvaluateInput, WaitInput(FUNCTION), and submit clicks — never `repeat`; execute_action (631-684) and the sequence loop (481-485) wrap no wall-clock deadline or budget around handler dispatch. _resolve_timeout (827-828) only sets a per-Playwright-call element timeout, not a loop bound, so each of N presses gets a fresh timeout and the loop runs unbounded. BudgetTracker (utils.py:778) is wired into agent.py/recipes.py but is never referenced in browser_actions.py execution — the reviewer's 'never wired into execute_action' claim is correct. The codebase's own comments (lines 357 'an LLM-controlled sequence can bypass', 997 'LLM-supplied automation', 1448 'prompt-injection') confirm action sequences are treated as attacker-controlled, so repeat=100000000 passing all validation is a real, trivially-triggered DoS. Corroborating asymmetry: ClickXYInput.clicks (models.py:829) uses ge=1, le=3, proving the author bounds repeat-style ints elsewhere — `repeat` is an inconsistent omission. Downgraded from high to medium: `await keyboard.press` yields between iterations so it does NOT block other asyncio tasks (the 'monopolises the event loop' phrasing is imprecise); the impact is stalling the single shared CDP connection / agent progress for an effectively unbounded wall-clock time, with no timeout to recover — a genuine resource-exhaustion foot-gun degrading one agent process, fixable in one line, but not state-corrupting or privilege-escalating.

---

#### [BR-4] ScrollInput.infinite_scroll_max is unbounded, making the infinite-scroll loop's total wall-clock time attacker-controlled

`web_agent/browser_actions.py:909-925` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; raw **medium** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** The infinite-scroll loop iterates `range(action.infinite_scroll_max)`. infinite_scroll_max (models.py line 714) is `int = Field(default=10)` with NO le bound, and infinite_scroll_delay_ms (default 1000) is likewise unbounded. While v1.6.14 A-6 correctly bounds each individual evaluate via asyncio.wait_for(min(timeout,5000)/1000), the OUTER loop count is fully caller-controlled, so total runtime = infinite_scroll_max * (delay + up to ~15s of capped evaluates). An LLM-supplied action with infinite_scroll_max=10_000_000 keeps one sequence (and its Page) busy effectively forever. The per-iteration cap addressed the hung-page case but not the iteration-count DoS.

**Evidence.**

```
            for _ in range(action.infinite_scroll_max):
                prev_height = await asyncio.wait_for(
                    page.evaluate("document.body.scrollHeight"),
                    timeout=scroll_eval_timeout,
                )
                ...
                await asyncio.sleep(action.infinite_scroll_delay_ms / 1000)
```

**Suggested fix.** Add an upper bound on the model (e.g. infinite_scroll_max: Field(default=10, ge=1, le=1000) and infinite_scroll_delay_ms: Field(default=1000, ge=0, le=60000)) and/or impose an overall wall-clock deadline on the whole loop.

**Verifier (refute pass).** Confirmed in code. models.py:714 `infinite_scroll_max: int = Field(default=10, ...)` and :715 `infinite_scroll_delay_ms: int = Field(default=1000)` have NO ge/le bounds. A grep for field_validator/model_validator/ge=/le= across all of models.py returns only `clicks: Field(default=1, ge=1, le=3)` at line 829 — proving no clamp exists for these two fields (and that the author knows the bounding idiom but didn't apply it here). browser_actions.py:909 iterates `for _ in range(action.infinite_scroll_max)` directly over the unbounded value; :918 sleeps `action.infinite_scroll_delay_ms/1000`. The v1.6.14 A-6 fix at :907 (`scroll_eval_timeout = min(timeout, 5000)/1000`) bounds only each individual page.evaluate call, NOT the loop count — exactly as the finding states. Reachability confirmed: external action dicts are parsed via `TypeAdapter(list[Action]).validate_python(...)` at mcp_server.py:334, agent.py:1658, and main.py:105, so `{"action":"scroll","infinite_scroll":true,"infinite_scroll_max":10000000}` validates cleanly and flows into _do_scroll. No per-action or overall wall-clock deadline wraps the call (execute_sequence loop body browser_actions.py:485 → execute_action :654 awaits the handler with no asyncio.wait_for). The only mitigating factor is the early-exit `if new_height == prev_height: break` (:924), which self-limits on a benign page; but an attacker who controls the target page (or supplies a large infinite_scroll_delay_ms, also unbounded) defeats it, so the guard does not neutralize the issue. Severity stays medium: it ties up a single sequence/Page rather than causing a memory/fd leak or multiplicative amplification, and the normal-page break limits real-world blast radius. Suggested fix (add le=1000 / le=60000 bounds and/or an overall deadline) is appropriate.

---

#### [UT-1] getaddrinfo can raise UnicodeError (idna), which escapes the except and crashes the SSRF gate

`web_agent/utils.py:572-578` &nbsp;·&nbsp; _error-handling_ &nbsp;·&nbsp; raw **medium** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** _resolve_host_addresses only catches (socket.gaierror, OSError). But socket.getaddrinfo encodes the host via the 'idna' codec and raises UnicodeError / UnicodeEncodeError for hosts containing surrogate or over-long-label characters (e.g. a URL host like '\udce9xample.com' or a 300-char label). UnicodeError is a subclass of ValueError, NOT OSError, so it propagates out of _resolve_host_addresses -> is_private_address -> check_domain_allowed. In WebFetcher.fetch, check_domain_allowed(url, ...) is the FIRST statement and is NOT inside the try block (web_fetcher.py:423), so instead of returning FetchStatus.BLOCKED the whole fetch() raises an uncaught UnicodeEncodeError. This is a fail-closed violation (a hostile/malformed host crashes the call path rather than being cleanly blocked) and is trivially reachable from any agent input that forwards a malformed URL host.

**Evidence.**

```
    try:
        # info[4] is the sockaddr tuple; index 0 is the IP literal
        addrs = tuple(str(info[4][0]) for info in socket.getaddrinfo(host, None))
    except (socket.gaierror, OSError):
        addrs = ()
```

**Suggested fix.** Widen the except to also swallow UnicodeError: `except (socket.gaierror, OSError, UnicodeError):` and return an empty tuple. (UnicodeError covers UnicodeEncodeError/UnicodeDecodeError from the idna codec.) Optionally also harden check_domain_allowed to treat an un-encodable host as 'no host' / rejected.

**Verifier (refute pass).** Confirmed by code reading + empirical end-to-end test. (a) Offending code exists exactly as cited: utils.py:572-578 catches only `(socket.gaierror, OSError)` around `socket.getaddrinfo(host, None)`. (b) Reachable: `_normalize_host` (utils.py:487-494) returns `urlparse(url).hostname` UNCHANGED — it does not idna-encode or strip surrogates; I verified it returns '\udce9xample.com' intact. `check_domain_allowed` (utils.py:673,685) -> `is_private_address` (638) -> `_resolve_host_addresses` (576) then calls getaddrinfo on that raw host. I empirically confirmed `socket.getaddrinfo('\udce9xample.com', None)` raises UnicodeEncodeError (a UnicodeError/ValueError subclass, NOT OSError/gaierror), so the except clause does not catch it. (c) No neutralizing guard: `is_private_address` only catches `ValueError` INSIDE the per-address loop (line 642), not around the generator call at line 638; `_normalize_host`'s `except Exception` (493) only wraps urlparse, which succeeds. A grep of the whole web_agent package for idna/surrogate/UnicodeError handling returned ZERO matches. Running `check_domain_allowed('http://\udce9xample.com/', SafetyConfig(block_private_ips=True))` raised UnicodeEncodeError ("'idna' codec can't encode character '\\udce9'") instead of returning False. Caller confirmed unguarded: web_fetcher.py:423 calls check_domain_allowed as the first statement of fetch(), outside the try block, so fetch() raises uncaught instead of returning FetchStatus.BLOCKED. Same unguarded pattern at agent.py:531 and 953. This is a real fail-closed violation: hostile/malformed host crashes the call path rather than being cleanly blocked. Severity medium is correct — it is a DoS / broken fail-closed contract on hostile input, not an SSRF bypass (the idna failure happens before any connection, so it cannot reach an internal target). Suggested fix (add UnicodeError to the except tuple) is correct and sufficient.

---

#### [CO-2] _normalize_domain_patterns does not strip port or IPv6 brackets -> deny-list entries silently never match (fail-open)

`web_agent/config.py:56-84` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **high** -> confirmed **medium** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** This validator exists specifically to stop malformed allow/deny entries from 'silently never matching anything' (docstring lines 60-64). It strips scheme, path, query, fragment and leading dots — but NOT the port and NOT IPv6 brackets. Consumers compare against `_normalize_host(url)` (utils.py 487), which returns `urlparse(url).hostname` — port-stripped and bracket-stripped. Result (reproduced): `denied_domains=["evil.com:8443"]` normalizes to `evil.com:8443`, while the actual host is `evil.com`, so `_matches_domain("evil.com","evil.com:8443")` is False — the deny rule is a silent no-op. Same for IPv6: `denied_domains=["[::1]"]` keeps the brackets and never matches host `::1`; `["http://[2001:db8::1]:9999/x"]` -> `[2001:db8::1]:9999` never matches `2001:db8::1`. An operator who writes a natural deny rule with a port (`localhost:8888`, `internal.svc:8080`) or an IPv6 literal gets ZERO protection and ZERO warning — exactly the foot-gun the normalizer claims to eliminate.

**Evidence.**

```
        s = raw.strip().lower()
        if "://" in s:
            s = s.split("://", 1)[1]
        # Strip path / query / fragment
        for sep in ("/", "?", "#"):
            if sep in s:
                s = s.split(sep, 1)[0]
        s = s.strip().lstrip(".")
        if s:
            out.append(s)
    # NOTE: port (':8443') and IPv6 brackets ('[::1]') are never stripped
```

**Suggested fix.** After stripping scheme/path/query/fragment, normalize the host the same way _normalize_host does: feed `"//" + s` (or a synthetic `http://` + s) through urlparse and take `.hostname`, which strips the port and IPv6 brackets uniformly. At minimum, strip a trailing `:<port>` and surrounding `[]`. Add tests for `evil.com:8443`, `[::1]`, and `http://[2001:db8::1]:9999/x` asserting they normalize to a value that `_matches_domain` accepts against the corresponding real host.

**Verifier (refute pass).** Confirmed by reading the code AND empirically reproducing it. config.py:74-83 (_normalize_domain_patterns) strips scheme/path/query/fragment and leading dots but never strips the port or IPv6 brackets. It is wired to both denied_domains and allowed_domains via field_validator(mode="before") at config.py:733-736. The comparator path uses _normalize_host (utils.py:487-494 -> urlparse(url).hostname), which DOES strip port and brackets, then _matches_domain (utils.py:497-509) does exact/suffix equality. Reproduction output: denied_domains=["evil.com:8443"] normalizes to 'evil.com:8443' while host is 'evil.com' -> _matches_domain==False (silent no-op); same for '[::1]'->'[::1]' vs host '::1', 'http://[2001:db8::1]:9999/x'->'[2001:db8::1]:9999' vs host '2001:db8::1', 'localhost:8888'->'localhost:8888' vs 'localhost', 'internal.svc:8080' vs 'internal.svc'. Control cases 'evil.com' and 'https://Evil.com/' both correctly match==True, proving the normalizer's intent and that ports/brackets are the gap. The normalizer's own docstring (config.py:60-64) states its purpose is to stop entries that "silently never match anything" -- exactly the foot-gun reproduced. No other validator strips ports/brackets; existing tests (test_safety.py:54-65, test_browser_url_safety.py, test_exceptions.py) only use bare hostnames, so this is untested, not intentional. Severity moderated from high to medium because the most security-critical sub-cases (localhost:NNNN, internal.svc:NNNN that resolve to private/loopback) are independently blocked by the default block_private_ips=True guard (utils.py:685, config.py:695); the allow-list side fails CLOSED (harmless). The genuinely un-mitigated fail-open is deny-listing a PUBLIC host by port (evil.com:8443), which silently never matches -- a real but narrow correctness/security defect. Suggested fix (feed "//"+s through urlparse and take .hostname) is correct.

---

#### [CO-3] max_contexts has no lower bound -> max_contexts=0 builds a zero-permit semaphore that deadlocks every fetch

`web_agent/config.py:109` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; raw **high** -> confirmed **medium** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** `max_contexts: int = 3` accepts 0 or negative. BrowserManager constructs `self._semaphore = asyncio.Semaphore(config.browser.max_contexts)` (browser_manager.py:161) and every ephemeral page acquisition awaits it. `asyncio.Semaphore(0)` has zero permits, so the first `acquire()` blocks forever — all ephemeral fetches hang indefinitely (no timeout on the acquire itself). A negative value raises ValueError deep inside asyncio at BrowserManager construction time rather than as a clean ConfigError. The codebase added `ge=1`/`ge=0` guards to max_pages_per_call, max_pages_per_session_fetch (line 119-121), max_network_events, rate_limit_per_host_rps in v1.6.14 — this throughput-critical field was left unguarded.

**Evidence.**

```
    max_contexts: int = 3
# consumed at browser_manager.py:161:
#   self._semaphore = asyncio.Semaphore(config.browser.max_contexts)
```

**Suggested fix.** Constrain the field: `max_contexts: int = Field(default=3, ge=1)`. This turns a silent hang / deep asyncio ValueError into a clear pydantic validation error at config construction.

**Verifier (refute pass).** Confirmed in code. config.py:109 declares `max_contexts: int = 3` as a bare int with NO Field constraint (contrast the sibling field max_pages_per_session_fetch at lines 119-122 which has ge=1, le=50). browser_manager.py:161 does `self._semaphore = asyncio.Semaphore(config.browser.max_contexts)` at __init__ time, and the ephemeral fetch path new_context() (line 754, wrapped by new_page() at 769) does `async with self._semaphore:` with NO asyncio.wait_for/timeout around the acquire. I empirically verified asyncio.Semaphore(0).locked() == True (first acquire blocks forever) and asyncio.Semaphore(-1) raises ValueError("Semaphore initial value must be >= 0"). I checked every validator in config.py: the two BrowserConfig model_validators (lines 336, 349) only validate user_agent_mode and isolation/CDP; none clamps or rejects max_contexts, and the line-899 validator belongs to SkillsConfig. So no guard neutralizes it. Two minor reviewer imprecisions, neither fatal: (1) the comment block at config.py:110-118 actually documents max_pages_per_session_fetch, not max_contexts; (2) the bug is reachable ONLY via operator misconfiguration (default 3 is safe) — no untrusted/runtime input flows into this field. That is why I downgrade high->medium: it is a genuine availability/hardening gap (0 => silent permanent hang of all ephemeral fetches; negative => deep asyncio ValueError instead of a clean ConfigError), fully consistent with the ge=1 guards v1.6.14 added to neighboring throughput fields, but it is not a default-path or attacker-reachable defect.

---

#### [CO-5] FetchConfig retry-policy partial override silently reverts the other two delays to hardcoded BALANCED defaults, not the named policy

`web_agent/config.py:563-594` &nbsp;·&nbsp; _correctness_ &nbsp;·&nbsp; raw **medium** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** The docstring (lines 530-534) says 'If the user explicitly sets any numeric retry field, it overrides the policy.' The implementation makes it all-or-nothing in a surprising way: if the user sets ANY one of {max_retries, retry_base_delay, retry_max_delay}, `user_set_numeric` is True and the ENTIRE policy block is skipped — so the other two fields keep their class-level defaults (1.0 / 30.0), which are BALANCED, NOT the requested policy. Reproduced: `FetchConfig(retry_policy='paranoid', max_retries=9)` yields max_retries=9 but retry_base_delay=1.0 and retry_max_delay=30.0 (BALANCED), silently discarding paranoid's 2.0/60.0. A caller asking for 'paranoid' retries while bumping just max_retries quietly gets balanced backoff — the opposite of the 'paranoid' intent on a flaky target.

**Evidence.**

```
        explicit = self.model_fields_set
        numeric_keys = {"max_retries", "retry_base_delay", "retry_max_delay"}
        user_set_numeric = bool(explicit & numeric_keys)

        if not user_set_numeric and self.retry_policy != "balanced":
            ...
            self.max_retries = int(kwargs["max_retries"])
            self.retry_base_delay = float(kwargs["base_delay"])
            self.retry_max_delay = float(kwargs["max_delay"])
```

**Suggested fix.** Apply the named policy as the baseline, then overlay only the numeric fields the user explicitly set: start from get_retry_policy(self.retry_policy), then for each of the three keys NOT in model_fields_set, take the policy value; for keys the user set, keep theirs. That honors 'paranoid + my max_retries' as 'paranoid delays with my retry count'.

**Verifier (refute pass).** Confirmed at code and runtime level. config.py:569-573 computes user_set_numeric = bool(model_fields_set & {max_retries, retry_base_delay, retry_max_delay}); if ANY one is set, the entire policy block (591-593) is skipped, leaving the other two at class-level defaults 1.0/30.0 (which equal BALANCED, per utils.py:459), NOT the requested policy. Empirically reproduced: FetchConfig(retry_policy='paranoid', max_retries=9) -> max_retries=9, retry_base_delay=1.0, retry_max_delay=30.0 (paranoid's 2.0/60.0 from utils.py:460 are silently discarded). Also FetchConfig(retry_policy='fast', max_retries=2) -> 2/1.0/30.0 instead of fast's 0.5/5.0. No guard/clamp/warning neutralizes it: no warnings.warn on this conflict (the only warning in config.py at 901-906 is the unrelated deprecated `enabled` field), and the Literal guard (line 555) only validates the policy NAME. The existing tests in tests/test_retry_policies.py:57-64 are blind to the bug -- they assert only the single field the user set (e.g. max_retries==99) and never check the other two fields, so the regression went uncaught. The docstring (530-534) says set-field 'overrides the policy', which the per-field field descriptions and the 'paranoid = flaky target / eventual success' intent make a genuine silent footgun. Medium is right, not higher: failure degrades to the well-tested BALANCED defaults (no crash/security/data-loss), it is config-time only, and there is a trivial workaround (set all three numerics, or none). Suggested fix (overlay only user-set fields on top of get_retry_policy baseline) is correct. Note: via env vars the same FETCH__MAX_RETRIES=9 + paranoid yields 3/1.0/30.0 because this pydantic-settings version leaves env-populated fields out of model_fields_set -- a related quirk but outside the filed kwargs-path finding.

---

#### [FC-1] classify_url HEAD probe has no peer-IP check and no per-redirect validation -- SSRF probe to internal hosts

`web_agent/web_fetcher.py:915-946` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **high** -> confirmed **medium** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** classify_url issues an httpx HEAD with follow_redirects=True and only validates the final resp.url host. Like FB-1, intermediate redirect targets are never validated and the connected peer IP is never checked, so a DNS-rebinding host or a whitelisted host that 302s through an internal address causes a real request to that internal host. classify_url is called by fetch_smart, recipes.find_and_download_file (line 434) and agent.py (line 556) for any extensionless URL whenever SafetyConfig.probe_binary_urls is True (the default), so an attacker-supplied search result that passes the host gate can be used to probe internal services / cloud metadata. The pre-gate at line 893 only blocks the ORIGINAL url; it does nothing about rebinding or redirect hops. The 'swallow all errors' contract means even a connection to an internal host that errors leaks timing/reachability.

**Evidence.**

```
async with httpx.AsyncClient(
    follow_redirects=True,
    timeout=10.0,
    headers={"User-Agent": get_random_user_agent()},
    cookies=cookie_jar,
) as client:
    resp = await client.head(url)
    final_url = str(resp.url)
    if final_url != url and not check_domain_allowed(final_url, self._config.safety):
        ...
        return "unknown"
    # <-- no per-redirect Location hook, no peer-IP private check
```

**Suggested fix.** Add the same event_hooks redirect guard and post-connect peer-IP check as FB-1. For a HEAD probe a redirect hook is the cheapest fix; combine with a peer-IP check on the response. Reuse the shared egress-guard helper.

**Verifier (refute pass).** CODE CONFIRMED at web_fetcher.py:915-946: classify_url opens httpx.AsyncClient(follow_redirects=True) and does client.head(url), validating ONLY the final host (line 925: `if final_url != url and not check_domain_allowed(final_url, ...)`). There is no per-redirect Location hook and no connected-peer-IP check — exactly as the reviewer states.

THE GAP IS REAL AND THE CODEBASE ITSELF PROVES IT. The project fixed this same threat class on every OTHER httpx/navigation egress path but missed classify_url:
- downloader.py:314-346 (the referenced FB-1 / v1.6.14 C-1(c)): GET path uses `event_hooks={"response":[_check_redirect]}` to re-validate EACH redirect Location (314-323), PLUS a post-connect `_httpx_peer_ip(response)`+`is_private_address()` rebinding guard (339-346).
- web_fetcher.py:671-686 (C-1(b)): navigation path runs `_response_peer_is_private(response)` post-connect.
classify_url has neither, yet the very same `_httpx_peer_ip` helper exists (downloader.py:100) and could be reused.

DNS-REBINDING IS NOT NEUTRALIZED ELSEWHERE. The host gate (check_domain_allowed -> is_private_address) relies on a 30s TTL DNS cache (utils.py:525-585), and utils.py:531-533 explicitly says "This is a *mitigation*, not a full close: the authoritative defenses are the post-connect peer-IP re-checks in web_fetcher / downloader (C-1b / C-1c)." classify_url lacks that authoritative defense, so a rebound host or an allowed host that 302s through an internal hop reaches the HEAD unchecked.

REACHABILITY CONFIRMED: probe_binary_urls defaults to True (config.py:738); classify_url is called from fetch_smart (web_fetcher.py:401), recipes.py:434, and agent.py:556 for any extensionless URL — including attacker-supplied search results.

SEVERITY DOWNGRADED high->medium: two real mitigations already blunt the worst cases — the pre-gate (line 893) blocks an originally-private URL, and the final-URL check (line 925) blocks redirect chains whose FINAL hop is private. What remains unguarded is genuine but narrower than FB-1: it is a HEAD probe (no response body returned to caller, so no data exfiltration — only up/down + timing signal via the swallow-all-errors path), with the host-level gate already present. The exploitable residue is intermediate-redirect-hop probing and DNS-rebinding of internal services. A real defense-in-depth SSRF gap worth the suggested event_hooks + peer-IP fix, but the HEAD-only/no-body blast radius is materially smaller than the GET path the reviewer compares it to, so medium is the honest rating.

---

#### [DL-1] SSRF block from _download_httpx is swallowed by broad except and falls through to the Playwright strategies

`web_agent/downloader.py:278-295` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **high** -> confirmed **medium** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** _download_httpx raises NavigationError (a WebAgentError -> Exception subclass) when a redirect resolves to a denied/private host (lines 319, 342, 351). download() wraps Strategy 1 in `except Exception as e:` (line 278), which catches that NavigationError, logs it as a benign 'httpx failed ... trying Playwright browser', and falls through to Strategy 2/3 which re-attempt the SAME url via a real browser. The browser strategies only re-validate AFTER page.goto/expect_download (post-connect), so Chromium will actually navigate to / connect to the SSRF target before the post-hoc gate fires, and a server that behaves differently to the browser UA could even succeed. A hard security stop is silently downgraded into a retry against the hostile target -- fail-open behavior that defeats the C-1c hardening in the same function.

**Evidence.**

```
        except Exception as e:
            logger.info(
                "httpx failed for {url}: {e}, trying Playwright browser",
                url=url,
                e=e,
            )
            if self._debug.enabled:
                self._debug.capture_no_page(e, "httpx_download", context={"url": url})

        # Strategy 2 or 3 depending on URL type
        if _is_web_page_url(url):
            result = await self._save_page_with_playwright(url, filepath, session_id)
        else:
            result = await self._download_with_playwright(url, filepath, session_id)
```

**Suggested fix.** Catch NavigationError (and DomainNotAllowedError) explicitly BEFORE the generic `except Exception`, and return a BLOCKED DownloadResult immediately instead of falling through. Only transport-level failures (httpx.ConnectError/HTTPStatusError/etc.) should trigger the Playwright fallback.

**Verifier (refute pass).** Code exists as described. download() catches Strategy-1 failures with `except Exception as e:` at downloader.py:278 (logs benign "httpx failed ... trying Playwright browser") and falls through to Strategy 2/3 at lines 287-291. _download_httpx raises NavigationError at lines 319 (redirect to denied host), 342 (post-connect private peer IP / DNS-rebinding guard, C-1c), and 351 (final-URL denied). NavigationError subclasses WebAgentError -> Exception (exceptions.py:21,29), so it IS caught by line 278 (the specific `except httpx.HTTPStatusError` at line 268 does not catch it). Reachable: the original url is hard-gated up front (line 185), so the vector is an allowed host that 302-redirects to a denied/private host, or DNS rebinding where the peer IP is private. On fall-through, the browser strategies call page.goto(url) BEFORE re-validating: _do_save_page goto at line 442, check at 447; _download_with_playwright goto at 625/651, _blocked_by_redirect at 627/653. I confirmed there is NO network-level host/IP route guard in the browser context — browser_manager.py route handlers (lines 703-709, 418-422) only abort by resource_type — so Chromium genuinely connects to / follows the redirect to the internal target before the post-hoc gate fires. A hard security stop (notably the strongest signal, the peer-IP rebinding block) is thus silently downgraded into an actual browser connection to the SSRF target, defeating the C-1c hardening for the httpx path: fail-open. Downgrading from high to medium because the post-goto checks (check_domain_allowed(page.url) at 447, _blocked_by_redirect(download.url) at 627/653) DO return BLOCKED and prevent the denied content from being written to disk, so data is not exfiltrated to the caller's filesystem; the reviewer's "could even succeed" overstates the page-save path since the final host is re-validated regardless of UA. But the SSRF connection itself does occur, which is a legitimate weakening of an explicit security control. Suggested fix (catch NavigationError/DomainNotAllowedError before the generic except and return BLOCKED, letting only transport errors trigger Playwright fallback) is correct.

---

#### [CACHE-1] DiskCache performs all filesystem I/O synchronously on the event loop (unlike every sibling module)

`web_agent/cache.py:107-118, 133-160, 168-173` &nbsp;·&nbsp; _performance_ &nbsp;·&nbsp; raw **medium** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** Every disk operation in DiskCache is a blocking sync call executed directly on the asyncio event loop while holding self._lock: get() does path.read_text() (line 109), set() does write_text() (135) then _evict_if_needed() which runs glob()+N*stat() (147-151) and unlink() in a loop, and clear() globs+unlinks (168-173). The trace_recorder (B-6) and the page-content helper deliberately offload blocking disk I/O via asyncio.to_thread for exactly this reason, but DiskCache never does. On a cache dir with thousands of entries, a single set() blocks the whole loop for the duration of the glob+stat sweep — stalling all concurrent fetches/searches. This is a hot path: caching is opt-in but, once enabled, set() runs on every successful fetch and search.

**Evidence.**

```
files = list(self._dir.glob("*.json"))
        total = sum(f.stat().st_size for f in files)
        if total <= self._max_bytes:
            return
        files.sort(key=lambda f: f.stat().st_mtime)  # oldest first
```

**Suggested fix.** Wrap the blocking bodies of get/set/_evict_if_needed/clear in asyncio.to_thread (as trace_recorder._append_line does), or move the entire critical section into one to_thread call. At minimum offload the glob+stat sweep in _evict_if_needed, which is the worst offender.

**Verifier (refute pass).** Code confirmed exactly as described in cache.py. get() does synchronous json.loads(path.read_text(...)) under self._lock (line 109); set() does path.write_text(...) (135) then await self._evict_if_needed() (139); _evict_if_needed runs files=list(self._dir.glob("*.json")) (147), total=sum(f.stat().st_size for f in files) (148), files.sort(key=...st_mtime) (151), and f.unlink() loop (152-160); clear() globs+unlinks (168-173). None of these use asyncio.to_thread — all run on the event loop. The sibling-pattern claim is accurate: trace_recorder.py:194 does `await asyncio.to_thread(self._append_line, ...)` with an explicit comment (191-194) that to_thread keeps the loop responsive during disk I/O, and search_providers.py:210 also uses to_thread. Reachability confirmed: web_fetcher.py:484/504 call cache.get/set on every fetch and every SUCCESS fetch; search_engine.py:141/176 call get/set on every search and non-empty response. Cache is opt-in (agent.py:290-298, gated on cache_cfg.enabled) but, once enabled, these run on every request. No guard neutralizes it — there is no offload anywhere in cache.py. Note the worst offender is slightly understated by the reviewer: the glob + N*stat() sweep at lines 147-148 runs on EVERY set() to compute total (the <=max_bytes early-return at 149 happens only AFTER the full stat sweep), so on a dir with thousands of entries every write issues thousands of stat() syscalls on the loop, serialized under the lock, stalling all concurrent fetches/searches. Genuine performance issue inconsistent with the codebase's own B-6 remediation. Severity medium is fair: not a correctness bug, opt-in, and individual small read/write are sub-ms; pathological only at scale, but the glob+stat-on-every-write is a real loop-blocking hot-path concern.

---

#### [WS-1] markdown_skills_only gate checks an un-normalized path, so '..' escapes the domain-skills/ confinement

`web_agent/workspace.py:124-137, 193-201` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **medium** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** _check_write_allowed enforces 'must be under domain-skills/' by inspecting Path(rel_path).parts[0] on the RAW caller string. But the actual write target is p = self._resolve(rel_path) (write_file line 196), which runs safe_join_path and .resolve(), collapsing '..'. The two disagree: write_skill('../notes/x') -> write_file('domain-skills/../notes/x.md'). safe_join_path resolves that to <workspace>/notes/x.md (still inside the workspace root, so it's allowed), and the mode gate sees parts[0]=='domain-skills' and suffix '.md' and ALSO allows it. Net effect: in the default markdown_skills_only mode an LLM-driven write_skill/write_file call can drop .md files anywhere in the workspace (notes/, root next to helpers.py), defeating the mode's documented 'writes must be under domain-skills/' guarantee. Impact is bounded (stays inside the workspace; suffix gate still blocks .py; escaped files aren't auto-loaded as skills), but it's a confinement-gate that validates the wrong (pre-normalization) path.

**Evidence.**

```
            parts = p.parts
            if not parts or parts[0] != SKILLS_DIR:
                raise WorkspaceError(
                    f"Mode 'markdown_skills_only': writes must be under "
                    f"'{SKILLS_DIR}/' (got {rel_path!r})."
                )
```

**Suggested fix.** Run the mode gate against the RESOLVED path relative to root, not the raw string. E.g. compute resolved = self._resolve(rel_path); rel = resolved.relative_to(self.root()); then check rel.parts[0]==SKILLS_DIR and rel.suffix=='.md'. That makes the gate agree with the actual write location.

**Verifier (refute pass).** Confirmed real and reachable; no guard neutralizes it. workspace.py write_file (line 197) calls _check_write_allowed(rel_path) on the RAW caller string, while the actual target is p=self._resolve(rel_path) (line 196 -> safe_join_path -> .resolve(), utils.py line 765) which collapses '..'. The markdown_skills_only gate (lines 124-137) checks Path(rel_path).suffix and Path(rel_path).parts[0]; pathlib does NOT collapse '..' in .parts, so Path('domain-skills/../notes/x.md').parts == ('domain-skills','..','notes','x.md') -> parts[0]=='domain-skills' (allow) and suffix=='.md' (allow). safe_join_path's relative_to(base) check (line 768) passes because the resolved path (<workspace>/notes/x.md) is still inside the workspace root, so it does NOT raise. The two gates disagree exactly as described. Verified at runtime with a real Workspace in default markdown_skills_only mode: write_skill('../notes/x') wrote <workspace>/notes/x.md; write_file('domain-skills/../notes/y.md') wrote <workspace>/notes/y.md; write_file('domain-skills/../evil.md') wrote <workspace>/evil.md (workspace root, next to helpers.py) -- all succeeded. Control test ('../../../outside.md') was correctly blocked by safe_join_path, so impact is bounded to inside the workspace. list_skills() returned empty, confirming escaped files aren't auto-loaded as skills. Severity medium is fair: a confinement gate validates the wrong (pre-normalization) path and fails open within the workspace, defeating the documented 'writes must be under domain-skills/' guarantee, but impact is bounded (stays in workspace, .md suffix gate still blocks .py so no code exec, escaped files not auto-registered). Suggested fix (gate the resolved path relative to root) is correct.

---

#### [CE-1] Binary/HTML extractors return uncapped content; only the api_json path got the E-6 cap

`web_agent/content_extractor.py:416-437` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; raw **medium** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** The E-6 fix capped api_json content at 512 KiB (line 363-365) because re-serialization can balloon a body. But the PDF, XLSX, DOCX, CSV and trafilatura/bs4/raw branches all return `content=text` with `content_length=len(text)` and NO cap. The upstream byte cap (web_fetcher max_file_size_mb, default large) bounds the *input* blob but a 100 MB PDF/CSV still yields ~100 MB of extracted text. The character budget does NOT save this: utils.BudgetTracker.add_chars (utils.py:817-827) only RAISES after the fact, it never truncates, and the single-URL extract path in agent.search_and_extract (agent.py:458) appends `extract(fr)` to results WITHOUT ever calling budget.add_chars — so the oversized content is returned to the MCP client regardless. This blows downstream token budgets and serialization memory, exactly the harm the api_json cap was added to prevent, but for the much more common PDF/CSV/HTML paths.

**Evidence.**

```
            text = "\n\n".join(p for p in parts if p).strip()
            if not text:
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    content=None,
                )
            ...
            return ExtractionResult(
                url=url,
                title=title,
                content=text,
                extraction_method="pdf",
                content_length=len(text),
            )
```

**Suggested fix.** Apply a single shared content cap (e.g. min(safety.max_chars_per_call, a hard ceiling)) at the end of ContentExtractor.extract for every branch, mirroring the api_json max_api_content cap, and set content_length to the truncated length. Truncate-with-flag is preferable to relying on add_chars, which raises rather than bounds and isn't even called on the single-URL path.

**Verifier (refute pass).** Confirmed by direct reading. Only the api_json branch caps content: content_extractor.py:363-365 sets max_api_content=512*1024 and content_length=len(pretty). Every other branch returns content=text with content_length=len(text) and NO cap — PDF (431-437), XLSX (479-484), DOCX (527-533), CSV (572-577), trafilatura (623-635), bs4 (690-698), raw (713-718). The main extract() method (130-263) returns per-branch results verbatim with no central cap, and ExtractionResult.content (models.py:296) has no length validator.

Reachability and absence of a neutralizing guard are confirmed: upstream input is bounded only by download.max_file_size_mb, default 100 MB (config.py:601; enforced via streaming accumulator at web_fetcher.py:1141-1157), so a 100 MB PDF/CSV yields ~100 MB of extracted text. The reviewer's named guards do not save it: BudgetTracker.add_chars (utils.py:817-827) only RAISES, never truncates; and the single-URL path agent.py:457-460 appends extract(fr) and returns WITHOUT ever calling budget.add_chars. Even stronger than the finding states — the multi-result binary extraction path (agent.py:584-597, extract_files=True, the canonical PDF/CSV search route) calls extract(bin_fr) and pages.append(extraction) with NO add_chars call whatsoever. And on the HTML multi-result path where add_chars IS called (agent.py:673), the over-budget extraction is still appended in full in the except block (674-689) before breaking. So oversized content reaches the MCP client on every path.

Severity: medium is correct. The asymmetry and the harm (token-budget/serialization blow-up the E-6 cap was meant to prevent) are real, but it requires fetching a genuinely large document, is config-capped at 100 MB, and is a resource-bounding gap rather than memory corruption/RCE. Suggested fix (a single shared truncate-with-flag cap at the end of extract() for every branch) is appropriate.

---

#### [EC-1] EC host confinement uses substring-in-URL instead of hostname match — trivially bypassed

`web_agent/builtin_skills/ec_europa_document_search/__init__.py:36-37` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **medium** (confirmed) &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** The skill's only host confinement after search is `if not any(host in item.url for host in _EC_HOSTS): continue`. This is a substring test against the entire URL string, not a parsed-hostname check. A result URL like `https://evil.com/?ref=ec.europa.eu`, `https://ec.europa.eu.attacker.com/x`, or `https://attacker.com/ec.europa.eu` all contain an _EC_HOSTS substring and therefore PASS the 'drop non-EC hosts that may slip past site: operators' guard — exactly the case the guard exists to catch. The result body (capped to 5000 chars) is then returned to the caller as a trusted 'EC document'. Contrast domain_skills.get_for_url (same repo), which correctly does `host == skill_domain or host.endswith('.' + skill_domain)` on the parsed hostname.

**Evidence.**

```
    for item in results.pages:
        # Drop non-EC hosts that may slip past site: operators
        if not any(host in item.url for host in _EC_HOSTS):
            continue
```

**Suggested fix.** Parse the host and match on label boundaries: `from urllib.parse import urlparse; h = (urlparse(item.url).hostname or '').lower(); if not any(h == d or h.endswith('.' + d) for d in _EC_HOSTS): continue`.

**Verifier (refute pass).** Code exists verbatim: ec_europa_document_search/__init__.py:36-37 is `if not any(host in item.url for host in _EC_HOSTS): continue` — a raw substring test against the whole URL string, not a parsed hostname. The reviewer's three bypass examples all hold: `https://evil.com/?ref=ec.europa.eu`, `https://ec.europa.eu.attacker.com/x`, and `https://attacker.com/ec.europa.eu` each contain an _EC_HOSTS substring, so `any(...)` is True and they PASS the guard whose stated purpose (line 36 comment) is to "Drop non-EC hosts that may slip past site: operators." Reachable: `results.pages` originate from external search providers (agent.py:506 -> search), and `item.url` is the result URL; `site:` operators are advisory and evadable via SEO/open-redirect pages — exactly the case the comment anticipates. The matched item's body is then returned to the caller capped at 5000 chars (line 44) as a trusted EC document. No guard elsewhere neutralizes it: the only upstream host check is check_domain_allowed (agent.py:531), but SafetyConfig.allowed_domains defaults to an empty list (config.py:689) and an empty allow-list means allow-all (utils.py:692-693, documented config.py:661), and denied_domains is also empty by default — so evil.com passes upstream. The SSRF/private-IP guard (utils.py:684-686) still applies but is irrelevant to host-confinement spoofing. Contrast claim is accurate: the repo's correct label-boundary pattern (`host == d or host.endswith('.'+d)` on urlparse hostname) exists in utils._matches_domain (509), domain_skills.get_for_url (468-473), recipes.py (277,284), and web_fetcher.py (1005), making this substring check a genuine inconsistency. Severity is medium, not higher: impact is content/trust spoofing (a non-EC page mislabeled as authoritative EC content), not RCE or SSRF, and exploitation requires getting a crafted URL into the EC-scoped search results.

---

## Confirmed — LOW (11)

#### [BR-2] Concurrent execute_sequence on the same session_id stacks duplicate dialog listeners and clobbers shared _PAGE_DIALOG_STATES, corrupting dialog routing

`web_agent/browser_actions.py:477-479` &nbsp;·&nbsp; _concurrency_ &nbsp;·&nbsp; raw **high** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: medium

**What's wrong.** For a session-persistent tab, execute_sequence unconditionally does page.on('dialog', dialog_handler) and _PAGE_DIALOG_STATES[page] = dialog_state. There is NO per-session serialization: Agent.interact()/_call_scope only wrap a correlation+audit scope, so two interact(..., session_id=sid) coroutines awaited concurrently both target tab_mgr.current() (the SAME Page) and both register their own bound handler on it. Result: (a) two live 'dialog' listeners fire for a single dialog — the first accepts/dismisses, the second hits Playwright's 'Dialog is already handled' and raises inside the listener; (b) _PAGE_DIALOG_STATES[page] is a single dict slot, so sequence B overwrites sequence A's state and a DialogInput executed in A silently configures B's DialogResponse/prompt_text. The v1.6.14 A-2 finally-block remove_listener fixes sequential stacking but does nothing for the concurrent case. This corrupts shared cross-sequence state and can throw on a common path (any site that pops an alert/confirm during concurrent automation).

**Evidence.**

```
            dialog_handler = dialog_state.handle
            page.on("dialog", dialog_handler)
            _PAGE_DIALOG_STATES[page] = dialog_state
```

**Suggested fix.** Serialize access to a session's current tab — e.g. acquire the session/TabManager lock for the duration of an execute_sequence that targets a persistent tab, or maintain a per-Page set/stack of dialog states keyed by sequence rather than a single-slot dict so concurrent sequences don't overwrite each other. At minimum, detect an already-registered handler on the Page and refuse to run a second concurrent sequence against the same tab.

**Verifier (refute pass).** The cited code exists verbatim. For a reused session-persistent tab, execute_sequence unconditionally does page.on("dialog", dialog_state.handle) and _PAGE_DIALOG_STATES[page] = dialog_state (browser_actions.py:477-479). _PAGE_DIALOG_STATES is a single-slot WeakKeyDictionary[Page,_DialogState] (line 243), and _do_dialog reads that one slot via .get(page) (lines 1048-1051). No serialization exists on the path: Agent._call_scope is correlation+audit only (agent.py:368-370), Agent has no asyncio.Lock at all (grep: no matches), interact() just awaits execute_sequence (agent.py:860-870), tab_mgr.current() is sync and lock-free (tab_manager.py:229-233), and SessionManager._lock guards only create/close while get_tab_manager is unlocked (session_manager.py:173-182). So two interact(...,session_id=sid) coroutines awaited concurrently DO both target the same current() Page and both register a distinct bound handler + overwrite the shared state slot. The v1.6.14 A-2 finally remove_listener (lines 597-605) removes only the sequence's own bound method, so it fixes sequential stacking but not concurrent overlap — the reviewer is correct on all three code points. Both described symptoms are mechanically plausible: two live listeners -> second hits Playwright "Dialog is already handled"; single state slot -> sequence B clobbers A's DialogResponse/prompt_text.

BUT reachability is weak, which is why I downgrade from high to low. (1) No call site in the entire repo runs interact/execute_sequence concurrently on one session — every test and example is strictly sequential await-then-await. (2) The library explicitly frames a session as "an explicit user resource — the user knows when they're created and closed" (session_manager.py:8-10) and documents only sequential usage. (3) Concurrent automation against a SINGLE shared tab is contraindicated regardless of dialogs — two sequences would already fight over focus/navigation/DOM on the same Page; the dialog corruption is one symptom of a "don't drive one tab from two coroutines" misuse, not a uniquely dialog-specific trap. (4) Impact is bounded and largely contained: the "already handled" throw occurs inside Playwright's listener dispatch and any resulting sequence failure is funneled through execute_action's broad except Exception (browser_actions.py:671-684) into a FAILED ActionResult, not a process crash; the silent state clobbering only manifests on the narrow intersection of (concurrent same-session interact) AND (an alert/confirm/prompt firing during the overlap window). It is a legitimate latent hardening gap — consistent with the maintainers' own C-4 defense of shared-context concurrency in fetch_many (config.py:110-131) — but the trigger is an undocumented, unexercised, contraindicated pattern with bounded, mostly-caught impact, so "high" overstates it; low is the honest rating.

---

#### [SM-1] create() can orphan a freshly-built BrowserContext if TabManager construction raises

`web_agent/session_manager.py:105-128` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** create_persistent_context() succeeds at line 106 and returns a live BrowserContext, but the very next line `TabManager(ctx, ...)` (line 112) runs OUTSIDE the try/except (which starts at line 115). TabManager.__init__ calls `ctx.on('page', self._on_new_page)` (tab_manager.py:63); if that raises (context already closing, Playwright transport error), the exception propagates out of create() while `ctx` has never been stored in self._sessions and is never closed — a leaked Chromium BrowserContext for the process lifetime. The inner try/except (115-123) only protects the page-creation/UA-probe block, not the context allocation + TabManager wiring. The same leak occurs for any exception between line 106 and the registration at 126-128 that is not the page block.

**Evidence.**

```
        async with self._lock:
            ctx = await self._bm.create_persistent_context(block_resources=False)
            tab_mgr = TabManager(ctx, network_collector=self._network_collector)

            ua = None
            try:
                initial_page = await ctx.new_page()
                ...
            except Exception:
                ua = None

            info = SessionInfo(session_id=session_id, name=name, user_agent=ua)
            self._sessions[session_id] = ctx
```

**Suggested fix.** Wrap from `ctx = await ...create_persistent_context(...)` through registration in a try/except that, on any failure before/while registering, does `with suppress(Exception): await ctx.close()` and re-raises. I.e. guarantee the context is either registered or closed.

**Verifier (refute pass).** Structural claim is accurate. In session_manager.py create(): line 106 builds a live ctx; line 112 `TabManager(ctx, ...)` and line 125 `SessionInfo(...)` both run OUTSIDE the try/except (which spans only 115-123 and merely sets ua=None). Registration `self._sessions[session_id]=ctx` is at line 126. If anything raises between 106 and 126 outside the page block, ctx is never stored in _sessions/_info/_tabs and never added to _pending_close, so it is unrecoverable: Agent.__aexit__ -> close_all() (session_manager.py:184-203) only iterates self._sessions.keys(), and _call_scope (agent.py:357-370) is a pure audit/correlation wrapper with no cleanup. So no guard elsewhere neutralizes the orphan window — the reviewer did not misread the code, and the suggested try/except + suppress(Exception).close() matches the codebase's own pattern (start() rollback at browser_manager.py:481-503; v1.6.14 F-3 _pending_close at 75-80/155-169). However reachability is very weak: the only concrete trigger the reviewer names, `ctx.on("page", self._on_new_page)` (tab_manager.py:63), is Playwright's synchronous EventEmitter.on listener registration — a local dict append with no IO that does not inspect context liveness and realistically never raises for a just-returned context; the hypothesized 'transport error / context closing' does not apply to .on(). SessionInfo(...) at line 125 likewise won't reject a generated token session_id or a str/None ua. Additionally, in named-persistent-profile mode create_persistent_context returns a _NoCloseContextProxy (browser_manager.py:666-674) whose underlying context is always closed by BrowserManager.stop(), so no real Chromium leak occurs there — the genuine leak only exists in the ephemeral new_context() sub-path (browser_manager.py:685). Net: a real but purely theoretical hardening gap with no demonstrated real-world trigger; downgrade medium -> low.

---

#### [CO-4] max_file_size_mb has no lower bound -> negative value aborts/deletes every download (downloader paths use the raw value)

`web_agent/config.py:601` &nbsp;·&nbsp; _correctness_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** `max_file_size_mb: int = 100` accepts <= 0. The streaming downloader paths compute `max_bytes = self._config.download.max_file_size_mb * 1024 * 1024` raw (downloader.py:306, 441, 557). With a negative value max_bytes is negative, so the chunk guard `total + len(chunk) > max_bytes` is true on the first byte and every download aborts; the post-save guard deletes every file as 'oversize'. Zero behaves the same. Notably web_fetcher.py:1066 ALREADY defends this with `max(1, self._config.download.max_file_size_mb)` — proving the maintainers know the raw value is dangerous, but the fix was applied at one call site instead of on the field, leaving the three downloader.py paths exposed.

**Evidence.**

```
    max_file_size_mb: int = 100
# downloader.py:306 (no max(1, ...) guard):
#   max_bytes = self._config.download.max_file_size_mb * 1024 * 1024
# web_fetcher.py:1066 (defensive guard exists only here):
#   max_bytes = max(1, self._config.download.max_file_size_mb) * 1024 * 1024
```

**Suggested fix.** Constrain on the field: `max_file_size_mb: int = Field(default=100, ge=1)`, then drop the now-redundant `max(1, ...)` band-aid in web_fetcher.py so there is a single source of truth.

**Verifier (refute pass).** Code confirmed exactly as described. config.py:601 is `max_file_size_mb: int = 100` — a bare int with no Field(ge=...) constraint and no field/model validator on DownloadConfig (the validators at config.py:336/349/563/785/899/1210 all belong to BrowserConfig/FetchConfig/other classes, not DownloadConfig), so pydantic accepts <=0. All three downloader.py paths use the raw value: line 306 (httpx stream), line 441 (Playwright save-page), line 557 (_enforce_size_cap post-save). With a negative value max_bytes<0, and the chunk guard at line 365 `if total + len(chunk) > max_bytes` is true on the first non-empty chunk (0+len > negative), so every httpx download raises and unlinks the partial file (lines 366-375); the post-save stat guard at line 562 `if size > max_bytes` also deletes every file (size>=0 > negative). Zero behaves the same for any real content. web_fetcher.py:1066 confirmed to defend with `max(1, ...)`, proving the hazard is known but the fix was applied at only one call site — the three downloader.py paths are genuinely unguarded. So the bug exists and is reachable. HOWEVER I downgrade medium->low: the trigger is an OPERATOR-set config knob (default is a sane 100, not attacker-controlled), and the failure mode is fail-CLOSED (downloads refuse to write / are deleted) — a visible, self-inflicted denial-of-functionality, not silent corruption, no oversized write ever slips through, and no security impact. It is a legitimate but low-severity missing-validation/consistency gap; the suggested `Field(default=100, ge=1)` is the correct single-source-of-truth fix.

---

#### [FE-1] 429 Retry-After is not honoured on in-loop retries; the limiter is never re-acquired inside the retry wrapper

`web_agent/web_fetcher.py:688-703` &nbsp;·&nbsp; _correctness_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** On HTTP 429 the HTML path calls _signal_429 (which extends RateLimiter._next_allowed[host]) then raises a plain retryable Exception, with a comment claiming 'the next acquire(host) waits Retry-After ... so async_retry retries with the new wait honoured'. But RateLimiter.acquire(host) is called exactly once per top-level fetch() (line 493 in fetch(), before _do_fetch). The retry loop lives inside _do_fetch -> _fetch_with_retry and never re-acquires the limiter, so the extended deadline is ignored for every in-loop retry -- those wait only the async_retry exponential jitter (capped at retry_max_delay, default 30s), not Retry-After. The limiter extension only affects the NEXT independent top-level fetch/download/fetch_binary call on that host. So a server asking for Retry-After: 120 is hammered again ~within 30s during the same fetch(). The behavior is suboptimal and the docstring is materially wrong.

**Evidence.**

```
                host = urlparse(url).hostname or ""
                retry_after = self._signal_429(response, url, host)
                raise Exception(
                    f"HTTP 429 Too Many Requests for {url}"
                    + (f" (Retry-After: {retry_after}s)" if retry_after is not None else "")
                )
```

**Suggested fix.** Either re-acquire the rate limiter at the top of _fetch_with_retry (so the extended _next_allowed is honoured on each attempt), or have the 429 branch await asyncio.sleep(min(retry_after, cap)) directly before raising, or surface Retry-After to async_retry so it can override its jitter. At minimum fix the docstring so it doesn't claim a guarantee the code doesn't provide.

**Verifier (refute pass).** Mechanism confirmed across three files. (1) web_fetcher.py: grep shows RateLimiter.acquire(host) is called only at line 493 (in fetch(), BEFORE _do_fetch at 498) and line 1064 (binary path). The HTML retry loop is the @async_retry-decorated _fetch_with_retry (lines 574-601) inside _do_fetch; it never re-acquires. (2) The 429 branch (lines 690-701) calls _signal_429 -> RateLimiter.notify_429 (extends _next_allowed[host]) then raises a plain retryable Exception, with the docstring at 691-694 claiming 'the next acquire(host) waits Retry-After ... so async_retry retries with the new wait honoured.' (3) async_retry (utils.py 124-155) re-invokes func directly after only `await asyncio.sleep(jitter)` where jitter = min(base_delay*2^(attempt-1), max_delay)*uniform(0.5,1.0); it does NOT call acquire. (4) notify_429 (rate_limiter.py 132-151) writes _next_allowed[host], which is read ONLY inside acquire() (79-84); since in-loop retries bypass acquire, the extended deadline is ignored for them. config.py 556-558 confirms defaults max_retries=3, retry_base_delay=1.0, retry_max_delay=30.0. So a server returning Retry-After:120 is re-hit by in-loop retries after only the async_retry jitter, not 120s, and the docstring's guarantee is materially wrong for in-loop retries. No guard elsewhere neutralizes this — the limiter extension only benefits the NEXT top-level fetch on that host. Downgrade to low rather than medium: blast radius is bounded — with default max_retries=3 there are at most 2 in-loop retries (~1s then ~2s pre-jitter, well under the 30s cap), so it is ~2 premature requests within a few seconds per fetch(), not sustained hammering; the cross-call limiter extension (clamped to 300s via MAX_RETRY_AFTER_SECONDS) does still work; and the binary path is not retry-wrapped so it is unaffected and its docstring is correct. Genuine but minor correctness + wrong-docstring defect.

---

#### [DL-2] allowed_extensions download allowlist is bypassed by extensionless URLs and checks the URL ext, not the saved filename

`web_agent/downloader.py:210-223` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** The extension allowlist is only enforced when `ext` is truthy: `if ext and allowed_exts and ext not in allowed_exts`. _get_url_extension returns '' whenever the last path segment has no dot, so any extensionless URL (e.g. https://host/download, very common for content-disposition-driven downloads) skips the allowlist entirely and is saved regardless of type -- the response Content-Type is never checked against the allowlist either. Separately, the check uses the URL's extension while the file is written under the caller-supplied `filename`: download(url='https://x/a.pdf', filename='payload.exe') passes the allowlist on '.pdf' but writes 'payload.exe'. Both undermine the stated purpose of allowed_extensions as a download-type fence.

**Evidence.**

```
        ext = _get_url_extension(url)
        allowed_exts = self._config.download.allowed_extensions
        if ext and allowed_exts and ext not in allowed_exts:
            return DownloadResult(... status=FetchStatus.BLOCKED ...)
```

**Suggested fix.** Validate the extension of the FINAL saved filename (derived or caller-supplied) rather than the URL, and decide a policy for extensionless results (reject, or require Content-Type membership in an allowlist). If allowed_exts is non-empty, an empty/disallowed final extension should be BLOCKED, not silently allowed.

**Verifier (refute pass).** Both defects are confirmed in the actual code. (1) Extensionless bypass: downloader.py:213 gates the allowlist behind `if ext and allowed_exts and ext not in allowed_exts`; `_get_url_extension` (line 83-88) returns "" when the last path segment has no dot, so any extensionless URL (e.g. content-disposition downloads) short-circuits the `if ext` clause and skips the allowlist entirely. The default `allowed_extensions` is a non-empty list (config.py:602-624), so the fence is active by default and genuinely bypassed. (2) Wrong target: `ext` is derived from `url` (line 211), but the file is written to `filepath` built from the caller-supplied `filename` (lines 246-252 -> open() at line 359), so download(url='x/a.pdf', filename='payload.exe') passes the .pdf check and writes payload.exe. No neutralizing guard exists: Content-Type is captured (line 356) but only stored in the result (line 389), never compared to the allowlist; safe_join_path (utils.py:732-772) only blocks path traversal/absolute paths, not extensions. So the reviewer's description is accurate and reachable. I downgrade medium->low on impact: the toolkit is a programmatic API gated upstream by allow_downloads (line 197) and a domain allow/deny gate (line 185); the filename-mismatch vector requires the trusted caller to pass its own malicious filename (self-inflicted), and the extensionless vector still lands in a size-capped, sandboxed download dir that the toolkit never executes. It is a real defense-in-depth weakening of allowed_extensions, but not a high-impact security hole.

---

#### [DL-3] Playwright page-save loads unbounded page.content() into memory before the size cap can fire

`web_agent/downloader.py:486-531` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** _do_save_page enforces max_file_size_mb only via (a) the optional Content-Length header pre-check and (b) len(html.encode('utf-8')) AFTER safe_page_content() has already materialized the entire DOM string in memory. A server that omits Content-Length (trivial) and returns a multi-GB HTML body forces page.content()/evaluate(outerHTML)/CDP getOuterHTML to buffer the whole document in the Python process before the in-memory check rejects it. Unlike the httpx path (which streams with a running cap and aborts mid-stream), the page-save path has no way to bound memory; the cap only prevents the disk write, not the heap blowup. Reachable from Downloader.download() Strategy 2 for any web-page URL.

**Evidence.**

```
        html, html_source = await safe_page_content(page)
        ...
        # In-memory pre-check: stop before write if the rendered DOM is too large.
        encoded_size = len(html.encode("utf-8"))
        if encoded_size > max_bytes:
            return DownloadResult(... HTTP_ERROR ...)
```

**Suggested fix.** Treat an absent/zero Content-Length combined with a successful navigation as a signal to fetch the body via a bounded httpx stream instead, or cap page.content() length defensively (e.g. abort the navigation when the document request's Content-Length is missing for untrusted hosts). At minimum document that the page-save cap is disk-only and the memory cost is unbounded.

**Verifier (refute pass).** Code confirms the finding. downloader.py:486 calls `safe_page_content(page)`, which at utils.py:359 does `await page.content()` — an atomic call that materializes the entire DOM as a Python str. The in-memory cap check at downloader.py:517-519 (`len(html.encode('utf-8')) > max_bytes`) runs only AFTER the full string is resident (and encode() transiently doubles it). The Content-Length pre-check at lines 462-479 fires only `if cl_raw:` and is trivially bypassed by a chunked/omitted Content-Length; moreover for a rendered page Content-Length describes the original response, not the post-render DOM. By contrast the httpx path (lines 360-370) streams in 8192-byte chunks with a running `total + len(chunk) > max_bytes` guard that aborts mid-stream, so the reviewer's contrast is accurate. Reachable: download() (166) -> httpx Strategy 1 fails (264-285) -> `_is_web_page_url` branch (288) -> _save_page_with_playwright -> _do_save_page; the cap is disk-only, memory is unbounded. So the bug is real, reachable, and ungated. I downgrade medium->low because: (1) SSRF/private-IP is independently blocked (utils.py:684-686 plus redirect re-checks at 447 and 314-323), so there is no integrity/SSRF impact — pure resource exhaustion; (2) it requires an already-allow-listed (malicious/compromised) host; (3) the Chromium renderer must hold the multi-GB DOM in ITS heap before page.content() returns, so a truly pathological body tends to OOM/hang the renderer and surface as a caught exception (downloader.py:416 -> NETWORK_ERROR) rather than a clean Python heap blowup; (4) it is a single transient allocation per call freed on return, not a sustained leak; default cap is 100 MB (config.py:601) so legitimate use is unaffected. It is a genuine defensive-bound/documentation gap, hence low rather than medium.

---

#### [RL-1] Per-host state dicts grow unbounded for the process lifetime

`web_agent/rate_limiter.py:52-57, 87, 150-151` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** _next_allowed, _locks (defaultdict), and _429_counts each gain one permanent entry per distinct host the agent ever touches, and are never pruned. RateLimiter is constructed once per Agent (agent.py:270). For a long-lived process — e.g. the MCP server (serve-mcp) holding one Agent and fetching arbitrary attacker/LLM-chosen hosts — these dicts grow without bound across the whole run. defaultdict[str, asyncio.Lock] is the heaviest (a Lock object per host). The same unbounded-growth pattern repeats in RobotsChecker (ROBOTS-1) and SessionTraceRecorder (TRACE-3). The DNS cache in utils.py was given a 2048-entry bound for precisely this reason; these dicts have none.

**Evidence.**

```
        self._next_allowed: dict[str, float] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # v1.6.12: tally of 429 events seen per host. Internal-only for
        # now; hooks for a future adaptive-rps policy. No getter exposed
        self._429_counts: dict[str, int] = {}
```

**Suggested fix.** Bound the host maps (e.g. an OrderedDict/LRU with a max size, or periodically drop hosts whose _next_allowed is far in the past and whose lock is unlocked). Even a simple cap matching _DNS_CACHE_MAXSIZE would close the leak.

**Verifier (refute pass).** The finding is factually accurate and reachable, with no neutralizing guard. Confirmed in rate_limiter.py: _next_allowed (line 52) is written at lines 87 and 150; _locks defaultdict (line 53) autovivifies one asyncio.Lock per host at line 75 (self._locks[host]); _429_counts (line 57) is written at line 151. I read all 152 lines — there is no eviction, max-size, or pruning anywhere; every map only grows by distinct host. Reachability confirmed: RateLimiter is constructed once per Agent (agent.py:269-270), the MCP server holds a single Agent for the whole process lifetime via lifespan (mcp_server.py:122-130), and acquire(host) is called with urlparse(url).hostname from the downloader (downloader.py:242) and web_fetcher — i.e. arbitrary, caller/LLM-controlled hosts — so distinct hosts accumulate permanent entries for the run. The asymmetry the reviewer cites is real: the DNS cache in utils.py IS bounded (_DNS_CACHE_MAXSIZE=2048, soonest-expiry eviction at lines 582-585) while these dicts are not. The cross-referenced RobotsChecker shares the same unbounded pattern (robots.py:54 _cache, robots.py:61 _locks). Note the v1.6.14 C-1 cap at line 144 only bounds the per-event Retry-After delay, NOT the dict growth — so it does not neutralize this. I downgrade medium→low: the per-host footprint is only a few hundred bytes (one float + one int + one asyncio.Lock + three dict string keys), there is no correctness impact and no fast-path DoS, and meaningful leakage requires the long-lived process to touch hundreds of thousands to millions of distinct hosts. It is a genuine but slow, low-impact unbounded-growth leak sitting at the low/medium boundary.

---

#### [ROBOTS-1] RobotsChecker per-host cache and lock dicts never evict

`web_agent/robots.py:54, 61, 89-94` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** _cache and _locks gain a permanent entry per host and are never pruned; entries past TTL are re-fetched in place but never removed. Like RateLimiter, RobotsChecker is created once per Agent and lives as long as the process (e.g. the MCP server). A run that touches many distinct hosts accumulates a (float, RobotFileParser) tuple plus an asyncio.Lock per host forever. RobotFileParser instances can be non-trivial in size for large robots.txt files.

**Evidence.**

```
        self._cache: dict[str, tuple[float, RobotFileParser | None]] = {}
        ...
        self._locks: dict[str, asyncio.Lock] = {}
```

**Suggested fix.** Add a size bound / LRU eviction to _cache and _locks (drop the lock together with its cache entry when evicted), mirroring the DNS cache's bounded approach in utils.py.

**Verifier (refute pass).** Confirmed by reading robots.py in full (133 lines). The offending code exists exactly as described: robots.py:54 declares self._cache and :61 declares self._locks as plain dicts with no bound. is_allowed() at :89 does self._locks.setdefault(host, asyncio.Lock()) — adding a permanent lock per host — and at :91-94, on TTL expiry it OVERWRITES the cache entry in place (self._cache[host] = (...)) rather than deleting it. There is no pop/popitem/clear/eviction anywhere in the file or class; no guard neutralizes the growth. Reachability confirmed: agent.py:274-278 instantiates self._robots = RobotsChecker(...) once per Agent (gated only on safety.respect_robots_txt), and an Agent lives for the whole process (the MCP-server case), so every distinct host visited leaks one (float, RobotFileParser|None) tuple plus one asyncio.Lock forever. The reviewer's comparison to the bounded DNS cache is accurate and confirms intent: utils.py:534-585 shows the same v1.6.14 hardening pass deliberately bounded the DNS cache (_DNS_CACHE_MAXSIZE=2048 with oldest-entry eviction at 582-584), while robots.py — edited in the SAME v1.6.14 C-10 change per the :55-60 comment — was left unbounded. RateLimiter (rate_limiter.py:52-57) shares the identical unbounded-per-host pattern, so the framing holds. The bug is genuine. I downgrade medium->low because the dicts are keyed by HOST, not per-request: growth is bounded by distinct-host cardinality (tens to low thousands typically), each entry is small (a parsed RobotFileParser plus a tiny Lock), there is no functional/security impact and no crash under realistic loads — only gradual memory growth in an extremely long-lived, high-host-cardinality process. The suggested fix (size-bound/LRU mirroring the DNS cache, evicting the lock with its cache entry) is correct.

---

#### [ROBOTS-2] robots.txt fetch performs no private-IP/SSRF check of its own (blind-SSRF lever; fail-open to caller)

`web_agent/robots.py:101-115` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** _fetch_and_parse builds url = f"{scheme}://{host}/robots.txt" and fetches it with a fresh httpx client with no is_private_address / check_domain_allowed gate. Today the two real callers (web_fetcher.fetch line 423, downloader line 185) run check_domain_allowed BEFORE is_allowed, so the SSRF check happens first — but that check resolves DNS via the 30s-TTL cache in utils._resolve_host_addresses, while httpx here re-resolves the host independently. Under DNS rebinding (public at check time, internal when robots fetches) this issues a request to the internal host. C-5 correctly disabled redirects, and robots returns no body to the caller (blind), so impact is limited to: a request being made to the internal endpoint, an existence/timing oracle, and the fetched rules influencing whether the subsequent page fetch proceeds. It is also fully fail-open if block_private_ips is set False (operator's choice) — robots will then happily fetch http://169.254.169.254/robots.txt. The module shouldn't rely entirely on callers ordering their gates correctly.

**Evidence.**

```
        url = f"{scheme}://{host}/robots.txt"
        try:
            ...
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                resp = await client.get(url, headers={"User-Agent": self._user_agent})
```

**Suggested fix.** Add a defense-in-depth guard at the top of _fetch_and_parse: if the caller's SafetyConfig has block_private_ips and is_private_address(host) is True, skip the fetch and return None (treat as allow-all, matching existing semantics). Pass the relevant flag in, or have RobotsChecker hold a reference to the safety policy.

**Verifier (refute pass).** Offending code confirmed exactly as described. robots.py:107 builds url=f"{scheme}://{host}/robots.txt" and :114 fetches via a fresh httpx.AsyncClient with no is_private_address/check_domain_allowed gate; RobotsChecker.__init__ (robots.py:44-61, constructed at agent.py:275) holds no SafetyConfig reference. Both callers run the SSRF gate before robots (web_fetcher.py:423 then :452; downloader.py:185 then :226), so a STATICALLY private host is blocked at utils.py:685 before robots runs — the finding concedes this. The real (narrow) lever is DNS rebinding: check_domain_allowed→is_private_address resolves via the 30s-TTL cache (_resolve_host_addresses, utils.py:534/540-586) while _fetch_and_parse's httpx re-resolves independently, and the post-connect peer-IP rebinding guards C-1b (web_fetcher.py:676) and C-1c (downloader.py:339) inspect only the MAIN fetch/download peer — they do NOT cover the separate robots.txt httpx client. So no guard elsewhere neutralizes the robots path; the rebind vector is genuinely reachable. Impact is limited though: C-5 (robots.py:109-115) disabled redirects, no body is returned to the caller (blind SSRF), so the yield is just a request issued to the internal /robots.txt, an existence/timing oracle, and influence over whether the subsequent fetch proceeds — within a ≤30s window requiring attacker-controlled DNS. The block_private_ips=False vector the finding cites is not robots-specific: that flag short-circuits is_private_address for ALL paths (utils.py:684-685), so it is documented operator opt-out, not a distinct robots defect. Real defense-in-depth gap, but blind/low-yield and contingent — low, not medium.

---

#### [TRACE-1] Trace secret-redaction map omits action.evaluate (arbitrary JS that routinely embeds tokens)

`web_agent/trace_recorder.py:61-70` &nbsp;·&nbsp; _security_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** _SENSITIVE_ARG_BY_METHOD redacts only fill/type/type_text. But EvaluateInput (action 'evaluate', models.py:803-809) carries an arbitrary JavaScript `expression`, which is exactly where credentials show up in practice (e.g. localStorage.setItem('access_token','eyJ...'), or fetch(url,{headers:{Authorization:'Bearer ...'}})). When trace_enabled, the full expression is written verbatim to <session_id>.jsonl via action_input.model_dump(...) at browser_actions.py:515. The stated intent of v1.6.14 B-8 was to keep 'user-typed secrets' out of the trace; this leaves a whole secret-bearing action class unredacted. (WaitInput.value can also hold a JS function body, and SelectInput.value an option value — lower risk, same gap.)

**Evidence.**

```
_SENSITIVE_ARG_BY_METHOD: dict[str, str] = {
    "action.fill": "value",
    "action.type": "text",
    "action.type_text": "text",
}
```

**Suggested fix.** Add "action.evaluate": "expression" (and consider "action.wait": "value"). Better: switch _redact_args to redact by a denylist of known-sensitive keys across all action types, or redact every field not on an explicit safe-to-log allowlist, so a newly-added secret-bearing action isn't silently un-covered.

**Verifier (refute pass).** Code confirmed exactly as described, no misread, no neutralizing guard. trace_recorder.py:66-70 maps only action.fill/type/type_text -> their secret field. _redact_args (lines 80-85) returns args UNCHANGED when method is not a key. browser_actions.py:514-515 sets method=f"action.{action_input.action}" and args=model_dump(exclude_none=True, exclude={"tab_id"}); for EvaluateInput (models.py:803-809, action="evaluate", field expression:str) the method is "action.evaluate" — absent from the map — so the full JS expression is written verbatim to <session_id>.jsonl (record() at lines 181/189/194). The grep for redact/scrub/sanitize confirms _redact_args is the ONLY trace-redaction path; nothing downstream catches it. WaitInput.value (models.py:794-797, "JS function body") and SelectInput.value are the same uncovered category, so the B-8 intent ("keep user-typed secrets out of the trace") is genuinely under-served — a correctly-diagnosed defense-in-depth gap with a trivial fix. Downgraded medium->low because: (1) trace_enabled defaults to False (config.py:1115-1116), so the leak requires explicit opt-in; (2) secrets in an evaluate expression are operator/LLM-authored JS, not a guaranteed credential channel like a password field — presence is probabilistic, not structural; (3) output is a local diagnostics file, not transmitted. Real and worth fixing, but conditional reachability keeps it below medium.

---

#### [TRACE-2] Secret redaction silently breaks replay fidelity (passwords replay as the literal '***REDACTED***')

`web_agent/trace_recorder.py:73-85, 178-184` &nbsp;·&nbsp; _correctness_ &nbsp;·&nbsp; raw **medium** -> confirmed **low** &nbsp;·&nbsp; verifier confidence: high

**What's wrong.** The module's headline feature is replay: Agent.replay_trace reconstructs Action objects from the JSONL and re-executes them (agent.py:1639-1673). But the same JSONL is what B-8 redaction writes, with the secret value replaced by '***REDACTED***'. So replaying any recorded login/fill sequence reconstructs FillInput(value='***REDACTED***') / TypeInput(text='***REDACTED***') and types the literal placeholder into the field — the replay does not reproduce the original run and will fail authentication, with no error or warning. The module docstring ('reconstructs the Action ... and re-executes them against a fresh page') and replay_trace's contract both imply faithful re-execution. Redaction (write-time security) and replay (read-time fidelity) are in direct conflict on the same file with no reconciliation.

**Evidence.**

```
    redacted = dict(args)
    redacted[key] = _REDACTED
    return redacted
...
                "args": _redact_args(method, args),
```

**Suggested fix.** Make the conflict explicit: either (a) document that traces containing fill/type/evaluate secrets are NOT replayable and have replay_trace warn when it encounters a '***REDACTED***' sentinel in a value/text field, or (b) keep secrets out of the trace entirely (skip recording sensitive actions) rather than writing a placeholder that looks replayable but isn't.

**Verifier (refute pass).** Mechanism confirmed. _redact_args (trace_recorder.py:83-84) writes '***REDACTED***' into the value/text arg for action.fill/type/type_text in the serialized JSONL (used at line 181). The secret-bearing fields are real: FillInput.value (models.py:700), TypeInput.text (689), TypeTextInput.text (842), and _SENSITIVE_ARG_BY_METHOD (66-70) maps exactly those. Agent.replay_trace reads that SAME JSONL (agent.py:1639), rebuilds args (1652), restores the discriminator (1655), validates into Action objects (1658), and executes via execute_sequence (1673). A redacted fill therefore reconstructs as FillInput(value='***REDACTED***') and types the literal placeholder, silently failing auth. No guard neutralizes it: a repo-wide search for 'REDACTED'/'redact' hits only the writer and CHANGELOG — replay has no sentinel detection or warning, and no test exercises the redact-then-replay path. The docstring (lines 19-21) and config description (config.py:1019-1020, 1122) advertise faithful re-execution with no caveat, so the silent-fidelity-break is real and matches the description. Reachable: replay is a live, exposed feature (CLI main.py:195; MCP web_replay_trace mcp_server.py:1067). I downgrade medium->low rather than refute, because: (a) double opt-in — both trace_enabled and recording are off by default; (b) only the credentialed sub-case breaks — click/wait/navigate/select/type-of-non-secret replay perfectly, so 'replay is broken' is overstated; (c) it is a correctness/fidelity defect, not a security hole — redaction is the safe default (you don't want plaintext passwords in a replayable file) and there is no crash, corruption, or wrong original-run result, only a failed replay. The reviewer's suggested fix (warn on the sentinel during replay, or skip recording sensitive actions) is appropriate.

---

## Refuted by the verify pass (7) — flagged by a reviewer, thrown out after reading the code

#### [BR-5] NavigateInput BACK/FORWARD/RELOAD ignore the configured action timeout (and wait_until), so they hang for the full Playwright default regardless of config  — REFUTED

`web_agent/browser_actions.py:1019-1024` &nbsp;·&nbsp; _correctness_ &nbsp;·&nbsp; reviewer claimed **medium**

**Claim.** _do_navigate computes nothing via _resolve_timeout: GOTO honors action.wait_until, but BACK/FORWARD/RELOAD call page.go_back()/go_forward()/reload() with NO timeout and NO wait_until argument. They therefore fall back to Playwright's context-level default navigation timeout (commonly 30s) irrespective of automation.default_action_timeout or action.timeout. A sequence author who sets a tight timeout to bound a flaky page gets it silently ignored for any history/reload navigation, and a server that stalls the reload blocks the sequence for the full default. This is an inconsistency between the GOTO path (timeout-aware via wait_until) and the other three directions (timeout-blind).

**Why it was thrown out.** The finding's headline thesis is refuted by the code. Claim: BACK/FORWARD/RELOAD "fall back to Playwright's context-level default navigation timeout (commonly 30s) irrespective of config" and "hang for the full Playwright default regardless of config." This is false. browser_manager.py:697 calls ctx.set_default_navigation_timeout(self._config.browser.navigation_timeout) (and line 410 for the persistent context), and config.py:108 sets navigation_timeout: int = 45000. So the fallback is the CONFIGURED 45s navigation timeout, not an unconfigurable Playwright hardcoded default. The timeout is bounded and IS configurable via browser.navigation_timeout.

The claimed GOTO-vs-others inconsistency on timeout is also false. browser_actions.py:1018 GOTO calls page.goto(action.url, wait_until=action.wait_until) WITHOUT a timeout= argument too. So GOTO is governed by the same context-level navigation_timeout as go_back/go_forward/reload (1019-1024). None of the four directions pass action.timeout / _resolve_timeout(action.timeout) (827-828, which feeds automation.default_action_timeout). There is therefore no "timeout-aware vs timeout-blind" split on the timeout dimension — all four behave identically w.r.t. timeout.

The only true residual is that GOTO passes action.wait_until while BACK/FORWARD/RELOAD use Playwright's default wait_until ("load") and ignore NavigateInput.wait_until (models.py:743, default "networkidle"). That is a genuine but cosmetic inconsistency: it does NOT cause hangs and does NOT ignore the action timeout. Since the finding's load-bearing claims (ignores configured timeout, hangs for full default, GOTO timeout-aware) are all incorrect, the finding as filed is not a real bug; at most a low-severity wait_until-consistency nit remains.

---

#### [BR-6] take_screenshot session path opens an untracked page on the persistent context that auto-registers as a phantom tab and bypasses NetworkCollector  — REFUTED

`web_agent/browser_actions.py:790-797` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; reviewer claimed **medium**

**Claim.** In the session branch, take_screenshot does page = await ctx.new_page() directly on the persistent BrowserContext instead of going through TabManager. Two side effects: (1) the context's ctx.on('page', _on_new_page) listener (registered by TabManager) fires and auto-registers this throwaway page as a brand-new tab with a generated tab_id and an _opened_at entry; it is then closed in the finally, which fires _evict_on_close — but for the window between creation and close it is a live, listable tab, and _evict_on_close, if this page had been made current, would pick an arbitrary remaining tab as current. (2) The NetworkCollector is never attached to this page, so network/api/download capture silently misses everything this screenshot navigation does, unlike the ephemeral _bm.new_page() path which attaches. The result is inconsistent observability and transient phantom tabs in list_tabs() under concurrent use.

**Why it was thrown out.** Code exists as quoted: take_screenshot's session branch (browser_actions.py:790-797) does page = await ctx.new_page() on the persistent context (ctx from self._sessions.get), bypassing TabManager. Every session's context carries a TabManager (session_manager.py:112) whose __init__ registers ctx.on("page", self._on_new_page) (tab_manager.py:63), so the throwaway page IS auto-registered (tab_manager.py:96-99) as a transient tab until page.close() at line 797 triggers _evict_on_close. That transient-tab portion is real but purely cosmetic and self-healing.

However, the finding's two load-bearing claims are FALSE:
(2) "NetworkCollector is never attached, so capture silently misses everything" is refuted: the very _on_new_page listener that registers the tab also calls self._network_collector.attach(page) (tab_manager.py:114-115), with the same off-by-default gating (network_collector.py:107) as every other path. Capture IS wired.
(current-tab corruption) "_evict_on_close would pick an arbitrary remaining tab as current" does not occur: _on_new_page explicitly never sets _current_tab_id (tab_manager.py:76-78), so the throwaway page is never current, and _evict_on_close's guard `if self._current_tab_id == tab_id` (line 130) is false. The reviewer even hedges this with "if this page had been made current" — it isn't.

"resource-leak" is mis-categorized: the page is closed in finally, _evict_on_close pops _tabs/_opened_at, and _reverse is a WeakKeyDictionary — nothing leaks. The only true residual is a transient phantom-tab/_opened_at entry visible to a concurrent list_tabs() during the screenshot, which is minor observability noise, not the reported harm. Marking real=false because the finding as filed (resource leak, lost network capture, corrupted current tab) does not hold.

---

#### [BR-1] _NoCloseContextProxy violates the __eq__/__hash__ contract (equal proxy vs raw ctx, different hashes)  — REFUTED

`web_agent/browser_manager.py:139-145` &nbsp;·&nbsp; _correctness_ &nbsp;·&nbsp; reviewer claimed **medium**

**Claim.** __eq__ returns True when comparing a proxy to the raw BrowserContext it wraps (`self._ctx is other`), but __hash__ returns id(self._ctx). A raw BrowserContext uses the default object hash, which is NOT id(ctx) (CPython rotates/derives it; verified hash(obj) != id(obj)). So proxy == raw_ctx is True while hash(proxy) != hash(raw_ctx). This breaks the fundamental invariant 'a == b implies hash(a) == hash(b)'. Any dict/set that stores one form and looks up the other (e.g. SessionManager._pending_close: set[BrowserContext], or any future context-keyed mapping) will silently MISS the lookup, exactly contradicting the docstring's promise that 'caller-side dicts keyed by context behave as if they always saw one context.' Today it's latent (named-profile close() is a no-op so _pending_close never sees the proxy), but it is a real correctness trap waiting for the next caller that mixes proxy and raw ctx in a set/dict.

**Why it was thrown out.** The technical premise is correct and I verified it empirically: in CPython the default object hash is the rotated id (`hash(o) << 4 == id(o)`), so for a raw BrowserContext `hash(raw_ctx) = id(ctx) >> 4`, while the proxy's `__hash__` (browser_manager.py:144-145) returns `id(self._ctx)`. Since `__eq__` (lines 139-142) makes `proxy == raw_ctx` True via `self._ctx is other`, the invariant `a == b ⟹ hash(a) == hash(b)` is genuinely violated. The suggested fix (`return hash(self._ctx)`) is correct.

HOWEVER, the rubric requires the bug be REACHABLE as described, and it is not — the finding itself concedes it is "latent." I traced the only candidate sink, SessionManager._pending_close (session_manager.py:80, `set[BrowserContext]`). The flow is: create() stores the proxy in `_sessions` (a *str*-keyed dict, line 65/126 — proxy hash irrelevant there); close() pops the SAME proxy object (line 144) and, only if `ctx.close()` raises, does `_pending_close.add(ctx)` (line 168); close_all() does `list(self._pending_close)` then iterates (lines 208-212). This is pure add+iterate of one object form — NO cross-form lookup (no `.remove`/`.discard`/`in`-test that mixes proxy and raw ctx) exists anywhere (grep confirms). A set's `add`+`list` never compares against a different-form key, so the hash/eq mismatch can never cause a missed lookup today. Additionally the proxy's `close()` is a guaranteed no-op that never raises (lines 116-118), so the `except` branch populating `_pending_close` is never even reached for a proxy — exactly as the reviewer admits.

So: the contract violation objectively exists (a real footgun/code-quality defect worth the one-line fix), but it produces no reachable bug in the current codebase and is structurally neutralized. Severity downgraded from medium to low; not "real" per the reachability bar.

---

#### [CO-6] No validate_assignment -> safe_mode and CDP/isolation invariants are construction-only; runtime mutation silently bypasses them  — REFUTED

`web_agent/config.py:785-809` &nbsp;·&nbsp; _api-design_ &nbsp;·&nbsp; reviewer claimed **medium**

**Claim.** None of the config models set `model_config['validate_assignment']=True` (only env_prefix is set). The @model_validator(mode='after') hooks (_apply_safe_mode line 785, _validate_isolation_and_cdp line 349, _apply_retry_policy line 563) therefore run ONLY at construction. After `AppConfig(safety={'safe_mode':True})` forces allow_* to False, a later `config.safety.allow_js_evaluation = True` STICKS (reproduced) — re-enabling arbitrary in-page JS despite safe_mode. The same applies to flipping cdp_host to a non-loopback value or attach_existing_browser=True post-construction: the loopback/attach guards never re-fire. The code even documents protection it doesn't provide: the comment at lines 804-808 says 'a caller mutating allow_coordinate_clicks back to True at runtime should not silently revert to allow' and pins coordinate_click_unknown_policy='block' as the safeguard — but nothing stops the caller from also mutating coordinate_click_unknown_policy back to 'allow', because assignment isn't validated. The actual safety net is the use-time checks in browser_actions/web_fetcher, not these validators.

**Why it was thrown out.** Mechanically the finding's facts check out and reproduce: SafetyConfig/BrowserConfig set no model_config['validate_assignment'] (config.py:658, 87; only env_prefix exists on SkillsConfig:939 and AppConfig:1174). The @model_validator(mode='after') hooks _apply_safe_mode (785), _validate_isolation_and_cdp (349), _apply_retry_policy (563) run only at construction. I reproduced it under pydantic 2.13.3 / pydantic-settings 2.6.1: SafetyConfig(safe_mode=True) sets allow_js_evaluation=False and coordinate_click_unknown_policy='block', then c.allow_js_evaluation=True and c.coordinate_click_unknown_policy='allow' both stick (validate_assignment is None). So claims (a) and (b) are accurate.

But this is NOT a reachable security bug. The reviewer's own description concedes the authoritative gate is the use-time check, and that is exactly what I found: every safety flag is read fresh from self._config.safety.<flag> at the moment of each operation (browser_actions.py:348/1158/1189/1294, downloader.py:197, web_fetcher.py, recipes.py, agent.py). The after-validator is merely a construction-time convenience that collapses allow_* when safe_mode=True; it was never the enforcement point. Crucially, a grep across the whole package shows ZERO assignments to safety flags from request/untrusted data and no setattr/model_copy in mcp_server.py — the LLM/MCP/web-content input flows through EvaluateInput actions that are GATED BY the flag, never able to MUTATE it. For the 'bypass' to occur, the trusted application code that built the config must itself write allow_js_evaluation=True (or a non-loopback cdp_host, read at browser_manager.py:256/375) post-construction — which is indistinguishable from constructing the config with that value, i.e. the owner deliberately overriding its own kill-switch, not a privilege escalation. The 'self-defeating documentation' claim also overstates the code: the comment at 804-808 explicitly says 'Defensive only' and guards the narrow case of a caller flipping allow_coordinate_clicks back to True while forgetting the policy; it does not claim tamper-proofing against a caller who also resets the policy. Net: valid API-design/hardening suggestion (re-run validators on assignment or freeze the models), but no untrusted-reachable safe_mode bypass exists, so real=false; severity low, not medium.

---

#### [MC-2] Single shared Agent across concurrent MCP tool calls races on shared mutable state (debug buffer reset)  — REFUTED

`web_agent/mcp_server.py:121-132` &nbsp;·&nbsp; _concurrency_ &nbsp;·&nbsp; reviewer claimed **medium**

**Claim.** lifespan() constructs ONE Agent and shares it across every tool invocation for the server's lifetime. FastMCP dispatches each JSON-RPC tool call as its own asyncio task, so two tool calls can be in flight concurrently. Several Agent methods mutate process-global state on that shared instance without any per-call isolation or lock: the recipe entrypoints call `self._debug.reset()` (agent.py:1465 and siblings) on the single shared DebugCapture (agent.py:262), so an overlapping web_research + web_search_best (both legal, both accept session_id) will clear/interleave each other's debug capture buffer, producing corrupted/empty diagnostics attributed to the wrong call. The browser stays warm by design, but the docstrings invite concurrent use ("All tools accept an optional session_id") with no warning that the Agent is not safe under concurrent tool dispatch.

**Why it was thrown out.** The finding misreads what DebugCapture is. It claims a shared "debug capture buffer" gets cleared/interleaved, "producing corrupted/empty diagnostics attributed to the wrong call." No such buffer exists. DebugCapture (debug.py:29-188) holds exactly ONE mutable field: `_capture_count = 0` (line 38). `reset()` (lines 185-187) only sets it to 0. `_capture_count` is purely a rate limiter on the NUMBER OF ARTIFACT FILES written to disk — `_under_limit()` (lines 64-65) gates writes against `max_artifacts_per_call` (default 5, config.py:824). It accumulates no diagnostics. Captured artifacts are independent files written to `debug_dir/{cid}/{timestamp}-{label}.{suffix}` (debug.py:60-62), where `cid` comes from `get_correlation_id()` — a contextvar set per-call by `correlation_scope()` (agent.py:368), which is task-local and concurrency-safe by construction. So each call's artifacts are correctly bucketed by its own correlation_id regardless of overlap; nothing is "attributed to the wrong call." The real per-URL diagnostics the finding conflates with this (agent.py:418) flow through return values + the contextvar, not through DebugCapture. Additionally `DebugConfig.enabled` defaults to False (config.py:820), so capture_page/capture_no_page short-circuit (debug.py:85, 163) and `_capture_count` is never even touched in the default config — `reset()` is a no-op. The only conceivable real effect, and only when debug is explicitly enabled, is that an overlapping `reset()` could zero the other call's counter and let it write slightly more than 5 artifact files — a benign over-count of disk files in diagnostics mode, NOT corrupted/empty diagnostics. The shared-Agent + concurrent-asyncio-dispatch premise is correct (one Agent at mcp_server.py:129), but the asserted harm does not exist in the code.

---

#### [CE-2] prefer_api re-serialization can raise uncaught RecursionError (and json.loads doesn't catch MemoryError)  — REFUTED

`web_agent/content_extractor.py:351-354` &nbsp;·&nbsp; _error-handling_ &nbsp;·&nbsp; reviewer claimed **medium**

**Claim.** On the prefer_api path, after parsing a captured XHR body, the code pretty-prints it with json.dumps but only catches (TypeError, ValueError). json.dumps on a deeply-nested structure raises RecursionError, which is NOT a subclass of ValueError, so it propagates out of _extract_from_api_candidates and out of ContentExtractor.extract, crashing the extraction for that page. The body_text is capped at 256 KiB, but a 256 KiB payload can nest several hundred levels deep — comfortably enough for json.dumps recursion to blow when the structure is just under the parse limit (json.loads succeeds, json.dumps overflows). Separately, the json.loads at line 327 catches RecursionError but not MemoryError, an inconsistency with _extract_json_ld (line 64) which deliberately catches MemoryError for the same adversarial-payload reason. Reachable only when capture_response_bodies=True and prefer_api=True, but it turns a hostile API body into a hard failure instead of a clean fallback.

**Why it was thrown out.** The code exists verbatim (content_extractor.py:351-354 catches only (TypeError, ValueError) around json.dumps(parsed, indent=2)), and the extract() caller at line 229 has no surrounding try/except, so a RecursionError WOULD propagate. But the claimed trigger is not reachable. The finding's premise is "json.loads succeeds, json.dumps overflows." I tested this on the actual interpreter (CPython 3.13.7, recursion limit 1000): max nesting depth json.loads tolerates = 2998 (lists and dicts alike); max depth json.dumps(parsed, indent=2) tolerates = 2998 — identical. At the exact deepest depth loads accepts, dumps(indent=2) succeeds; a boundary scan over depths 2900-3100 found ZERO depths where loads succeeds but dumps(indent) fails. CPython 3.12+ unified the C-stack recursion check, so decode and indented-encode ceilings coincide. Thus if line 326 json.loads succeeded, line 352 json.dumps cannot raise RecursionError; and a too-deep payload makes json.loads raise RecursionError FIRST at line 327, which IS already caught there (RecursionError is in that except clause) -> clean return None -> HTML fallback. The reviewer's own "several hundred levels" figure is ~5x below the ~2998 ceiling. On the MemoryError consistency point: body_text is hard-capped at 256 KiB byte-wise before decode (network_collector.py:393-396), so json.loads allocates a bounded structure and won't realistically MemoryError; _extract_json_ld's MemoryError guard (line 64) is justified by its different, uncapped full-HTML input, so the asymmetry is defensible, not a reachable defect. Path also gated behind capture_response_bodies=True AND prefer_api=True (both default False). The two except clauses could harmlessly add RecursionError/MemoryError for defense-in-depth symmetry, but no reachable crash exists.

---

#### [DOC-1] Doctor browser-launch probe can leak a Chromium process if cancelled by the 5s per-check timeout mid-launch  — REFUTED

`web_agent/doctor.py:144-163` &nbsp;·&nbsp; _resource-leak_ &nbsp;·&nbsp; reviewer claimed **medium**

**Claim.** _check_browser_launch is invoked via _timed -> asyncio.wait_for(..., timeout=5.0). If chromium.launch() is still in flight at the 5s deadline (cold start on a slow/loaded host can exceed 5s — the docstring itself says ~3-5s cold), wait_for cancels the coroutine. The cancellation can land after the Chromium subprocess has been spawned by the driver but before `browser` is bound, so `await browser.close()` never runs. The `async with async_playwright()` __aexit__ will fire during cancellation cleanup, but a browser that launch() spawned and never returned to user code is not guaranteed to be torn down — leaving an orphaned headless Chromium process. Because the per-check budget (5s) is right at the lower bound of cold launch time, this is a realistic trigger on a busy CI box, and doctor offers no compensating outer cleanup.

**Why it was thrown out.** Trigger is plausible but the claimed leak does not occur. doctor.py:152 wraps the launch in `async with async_playwright()`, so on the wait_for cancellation (doctor.py:46-48, budget 5.0s at line 33) Python guarantees __aexit__ runs during unwind. __aexit__ (playwright async_api/_context_manager.py:53-57) calls connection.stop_async(), which (_connection.py:322-325) does request_stop() + wait_until_stopped(). request_stop() (_transport.py:96-99) CLOSES STDIN TO THE NODE DRIVER PROCESS, and wait_until_stopped()->run() then awaits self._proc.communicate() (_transport.py:170), waiting for the driver process to actually exit. The Chromium subprocess is a child of the Node driver, NOT of the Python interpreter; when the driver's controlling pipe closes, the driver kills every browser it spawned. Therefore whether the Python `Browser` wrapper was ever bound to the `browser` variable is irrelevant to OS-process teardown — the finding's premise ('a browser launch() spawned but never returned to user code is not guaranteed to be torn down') is false for this pipe-transport design. A single wait_for issues one task.cancel(); the __aexit__ awaits complete normally (not re-cancelled), so the pipe-close + communicate() reap finishes. The suggested browser.close()/try-finally fix is redundant: close() only releases the wrapper handle, while the actual process teardown already happens at the transport/driver layer on context-manager exit, which fires regardless. Only residual edge is a wedged driver causing communicate() to hang (wait_until_stopped has no inner timeout) — but that is a hang, not the silent orphaned-process leak described, and not what the finding claims.

---

## Advisory — low severity, not independently verified (34)

These are real-looking but low-impact; by design only critical/high/medium findings went through the refute pass. Skim, don't prioritize.

| ID | Location | Category | Issue |
|----|----------|----------|-------|
| REC-3 | `web_agent/recipes.py:440-444` | correctness | find_and_download_file extensionless fallback can never match file_types=['doc'] and always drops binary_other |
| REC-4 | `web_agent/recipes.py:143-163` | maintainability | Dead module-level _resolve_domain_hints that silently diverges from the instance method actually used |
| AG-4 | `web_agent/agent.py:1687-1688` | correctness | save_results derives a dotfile/empty filename from queries with no alphanumerics |
| BR-7 | `web_agent/browser_actions.py:1813-1816` | security | observe() URL-rollback navigates back to prev_url without re-validating it against the safety policy |
| BR-8 | `web_agent/browser_actions.py:1622-1631` | error-handling | scroll_until_text swallows every exception with bare except: pass / continue, masking real failures (closed page, navigation errors) |
| BR-9 | `web_agent/browser_actions.py:148-171` | security | Submit/destructive-click heuristics are trivially bypassable and fail-open, giving a false sense of allow_form_submit safety |
| BR-10 | `web_agent/browser_actions.py:361-371` | maintainability | Redundant WaitInput(FUNCTION) JS-eval gate: pre-flight in execute_sequence duplicates the now-authoritative handler-level gate in _do_wait |
| BR-2 | `web_agent/browser_manager.py:106` | resource-leak | _NoCloseContextProxy __slots__ omits __weakref__, so it cannot be used as a WeakKeyDictionary key |
| SM-2 | `web_agent/session_manager.py:105-121` | concurrency | create() holds the global session lock across a page.evaluate round-trip, serializing all session ops behind a slow page |
| BR-3 | `web_agent/browser_manager.py:416-422` | error-handling | Resource-blocking route handlers are unguarded; route.abort()/continue_() can raise into Playwright's dispatcher on teardown |
| BR-4 | `web_agent/browser_manager.py:116-118` | api-design | _NoCloseContextProxy.close() silently swallows an explicit close() with a misleading no-op |
| CO-7 | `web_agent/config.py:489, 520, 106-107, 144-145, 638, 647` | correctness | Multiple security/throughput-relevant integers lack lower bounds (negative/zero accepted silently) |
| CO-8 | `web_agent/config.py:32-53` | security | _is_loopback_host diverges from is_private_address: misses octal/decimal/short-form loopback literals |
| CO-9 | `web_agent/config.py:1210-1229` | correctness | _resolve_paths uses Path.is_absolute() which is host-OS-dependent, unlike the _is_cross_platform_absolute helper used elsewhere |
| MO-1 | `web_agent/models.py:175-183, 732` | api-design | Documented invariants not enforced: FetchResult.binary/html mutual-exclusivity and ScreenshotInput.quality range |
| FC-2 | `web_agent/web_fetcher.py:993-1006` | security | _cookies_for_session forwards Secure-flagged session cookies to plaintext http:// targets |
| NC-1 | `web_agent/network_collector.py:284-295` | concurrency | Response-body capture tasks are only drained from WebFetcher.fetch; on download/automation paths they run fire-and-forget against a closing page |
| CACHE-2 | `web_agent/cache.py:133-141, 107-112` | correctness | Non-atomic cache writes leave permanently-unreadable files after a crash |
| TRACE-3 | `web_agent/trace_recorder.py:114-119, 159-161` | resource-leak | _counters dict grows one permanent entry per session_id |
| TRACE-4 | `web_agent/trace_recorder.py:114, 159-201` | performance | Single global lock held across the to_thread write serializes trace I/O across ALL sessions |
| DEBUG-1 | `web_agent/debug.py:38, 64-65, 85, 138, 163, 178` | concurrency | _capture_count limit is racy and can be overshot under concurrent captures |
| DEBUG-2 | `web_agent/debug.py:45-62` | correctness | Artifact filenames can collide under concurrency (no uniqueness counter) |
| CORR-1 | `web_agent/correlation.py:82-104` | api-design | Import-time logger.configure(patcher=...) clobbers (and is clobbered by) any other loguru patcher |
| MAIN-1 | `web_agent/main.py:102-105` | error-handling | run_interact reads/parses the actions JSON with no error handling or size bound |
| AUDIT-1 | `web_agent/audit.py:90-105` | security | Audit scope writes args and repr(exception) with no redaction |
| OWN-1 | `web_agent/ownership.py:72-79` | security | Ownership token file has a brief world-readable window before chmod 0o600 |
| CE-3 | `web_agent/content_extractor.py:307-322` | correctness | prefer_api selects the LARGEST captured JSON body, silently preferring analytics blobs over the real API response |
| MC-3 | `web_agent/mcp_server.py:124-125` | api-design | lifespan does a global logger.remove() that wipes any loguru handlers configured by an embedding process |
| MC-4 | `web_agent/mcp_server.py:452-453` | maintainability | web_research clamps depth to 3 while documenting only depth=1 is supported (doc/behavior lie) and web_search doc says 'max ~20' vs clamp of 50 |
| SP-1 | `web_agent/search_providers.py:216-229` | security | Literal private-IP result filtering exists only for SearXNG, not DDGS/Playwright |
| SP-2 | `web_agent/search_providers.py:283-289` | correctness | max_results is unbounded and flows straight into Google `num=` and provider slices |
| EC-2 | `web_agent/builtin_skills/ec_europa_document_search/__init__.py:27-47` | correctness | Negative max_results truncates to a single doc instead of erroring or returning none |
| SE-1 | `web_agent/search_engine.py:147-150` | correctness | Cache-hit path mutates the dict returned by the cache backend |
| DS-1 | `web_agent/domain_skills.py:254-268` | correctness | Input int/float coercion has no range or NaN/Inf guard |

## Per-cluster health summaries (reviewer verdicts)

### browser-actions
*Files:* `web_agent/browser_actions.py`, `web_agent/tab_manager.py`

These two files are mature and have clearly absorbed many prior review passes — the SSRF re-checks after every navigation, the JS-eval self-gating at the handler level, the upload-path containment, the dialog-listener removal in finally, and the careful WeakKeyDictionary lifecycle in tab_manager.py are all solid and the lock discipline in TabManager is genuinely well-reasoned (the _on_new_page no-await-between-mutations invariant is correct). The remaining defects are subtle and cluster in two areas: (1) input-validation gaps where caller/LLM-supplied action fields are unbounded — KeyboardInput.repeat and ScrollInput.infinite_scroll_max have no le bound, so a single hostile action can pin the event loop; and (2) per-session concurrency, which nobody serializes — two concurrent interact() calls on the same session_id share one persistent Page, stacking two live dialog listeners and clobbering the shared _PAGE_DIALOG_STATES entry, which both corrupts dialog routing and trips Playwright's "Dialog already handled". The single most concrete security regression is print_page_as_pdf accepting an ABSOLUTE output_path and writing the rendered PDF anywhere on disk with no containment check — the exact arbitrary-write class that save_results was hardened against in v1.6.14 B-5, left wide open here and even documented as intended. Path traversal for relative paths is correctly defended everywhere via safe_join_path; the absolute-path escape hatch is the gap. Nothing here is a slam-dunk RCE, but the PDF write and the dialog race are real and reachable.

### agent-recipes
*Files:* `web_agent/agent.py`, `web_agent/recipes.py`

These two files are mostly thin, well-documented orchestration over hardened lower-level primitives, and the prior review passes show: SSRF gating, redirect/peer-IP re-checks, LFI containment on replay_trace, and budget tracking are all present and correct on the MAIN paths (search_and_extract, fetch_and_extract, and the fetch_smart-based recipes all delegate to WebFetcher.fetch/fetch_binary, which do final-URL + post-connect peer-IP checks). The subtle, high-value defects cluster in the places that DON'T go through that canonical path: (1) Recipes.fill_form_and_extract drives a raw Playwright page.goto with only a pre-DNS host check and none of the redirect/peer-IP DNS-rebinding defenses the rest of the toolkit carefully applies -- a genuine SSRF/rebind hole on a public recipe; (2) Recipes.web_research builds an unbounded asyncio.gather of fetch_smart over a caller-controlled max_pages and, on the session path, bypasses the per-call semaphore that fetch_many added specifically to stop ctx.new_page() fan-out from crashing the renderer (the library API is also unclamped -- the max_pages/depth clamp lives only in the MCP wrapper); (3) search_and_extract's HEAD-probe gather swallows asyncio.CancelledError as "default to HTML", the exact mistake web_research was explicitly fixed for; (4) Agent.apply_domain_skill writes raw skill `inputs` (which for a login skill carry usernames/passwords/tokens) verbatim into the audit JSONL, while the trace path was hardened to redact fill/type secrets -- an asymmetric credential-to-disk leak. Plus a correctness footgun where replay_trace re-injects the literal "***REDACTED***" string for fill/type actions, and minor dead code / edge-case filename issues. Health: solid skeleton, but the "off the golden path" branches re-introduce classes of bug the golden path already solved.

### config-models
*Files:* `web_agent/config.py`, `web_agent/models.py`

config.py is the security spine of the toolkit and it has a serious, reproducible fail-open hole: every sub-config (SafetyConfig, BrowserConfig, DownloadConfig, SearchConfig, ...) is a pydantic-settings BaseSettings with NO env_prefix, so each independently reads BARE, unprefixed environment variables. The team already discovered this exact bug class for SkillsConfig (review I-2 -> added env_prefix) and WorkspaceConfig (the PATH foot-gun) but left every other sub-config exposed. The consequence is that a bare `BLOCK_PRIVATE_IPS=false` silently disables SSRF protection on the default `AppConfig()` path (`async with Agent()`), and `ALLOW_UPLOAD_OUTSIDE_DOWNLOAD_DIR=true`, `SAFE_MODE`, `ALLOWED_EXTENSIONS`, `HEADLESS` etc. are all hijackable the same way — none of this is documented (the README only promises the `WEB_AGENT_` prefix). On top of that, the domain-pattern normalizer that exists specifically to stop deny-list entries from "silently never matching" forgets to strip the port and IPv6 brackets, so `denied_domains=["evil.com:8443"]` or `["[::1]"]` is a no-op deny — another fail-open. Several security/throughput-relevant integers (max_contexts, max_file_size_mb, max_results, timeouts) lack the lower bounds that comparable v1.6.14 fields got, with max_contexts=0 deadlocking all fetches via a zero-permit semaphore and a negative max_file_size_mb aborting/deleting every download. Validators are construction-only (no validate_assignment) so safe_mode and the CDP loopback/isolation invariants can be silently bypassed by runtime field mutation — the code even documents a protection it does not actually provide. models.py is comparatively healthy (mostly plain data carriers); its only issues are documented-but-unenforced invariants. Overall: the hardening intent is clearly present, but the env-var namespacing and deny-list normalization gaps are real, exploitable holes that defeat the SSRF and upload fences under realistic conditions.

### fetch-download-network
*Files:* `web_agent/web_fetcher.py`, `web_agent/downloader.py`, `web_agent/network_collector.py`

These three files are mature and have clearly survived several SSRF-hardening passes — the Playwright HTML path (`fetch`) and the httpx download path (`_download_httpx`) both got proper post-connect peer-IP re-checks (C-1b/C-1c) and per-redirect validation. The problem is that the hardening was applied unevenly: the two OTHER network egress paths in `web_fetcher.py` — `fetch_binary` (httpx streaming) and `classify_url` (httpx HEAD) — were left with neither a post-connect peer-IP check NOR a per-redirect `Location` hook, so they re-open exactly the DNS-rebinding and redirect-to-internal SSRF holes the sibling paths closed. Worse, `downloader.download()` catches the `NavigationError` that `_download_httpx` raises on an SSRF block with a blanket `except Exception` and silently falls through to the heavier Playwright strategies, downgrading a hard security stop into a retry against the same hostile target. There are also several real correctness/robustness issues: the documented "429 Retry-After is honoured on retry" behavior is false for in-loop retries (the limiter is never re-acquired inside the retry wrapper), the `allowed_extensions` download allowlist is trivially bypassed by extensionless URLs and is checked against the URL rather than the saved filename, and the Playwright page-save path loads unbounded `page.content()` into memory before its size cap can fire. None of these are crash-on-common-path bugs, but the SSRF inconsistencies are genuinely exploitable under the same threat model the codebase already takes seriously elsewhere.

### mcp-content-doctor
*Files:* `web_agent/mcp_server.py`, `web_agent/content_extractor.py`, `web_agent/doctor.py`

These three files are in good shape relative to the rest of the toolkit: mcp_server.py is almost entirely thin pass-through wrappers with the real guards (path containment for upload/replay, dropdown validation, domain allow-lists, budget enforcement) correctly living in agent.py / browser_actions.py, and I confirmed those guards exist rather than assuming them. content_extractor.py has clearly been hardened (RecursionError/MemoryError catches in JSON-LD, the E-6 api_json content cap, JSON-LD block cap). doctor.py is defensively written (per-check wait_for + broad except, never raises). The remaining issues are subtle: (1) the one genuinely sharp edge is web_print_page_as_pdf exposing an LLM-controlled output_path that — unlike web_screenshot, which routes through safe_join_path — bypasses containment for absolute paths in browser_actions and becomes an arbitrary file-write primitive, with an mcp_server.py docstring that actively misrepresents this as "defaults to screenshot_dir"; (2) the single shared Agent created in lifespan is mutated by per-recipe self._debug.reset() with no concurrency guard, so overlapping MCP tool calls race on shared telemetry state; (3) the binary/HTML extractors still emit uncapped content (only api_json got a cap), and the char budget raises-after-the-fact rather than truncating; (4) a RecursionError path in the prefer_api json.dumps; (5) a Chromium-process leak window if the doctor browser-launch probe is cancelled by its 5s wait_for mid-launch. None are trivially exploitable on the default config, but #1 is a real prompt-injection-reachable write primitive and deserves a fix.

### browsermgr-session-utils
*Files:* `web_agent/browser_manager.py`, `web_agent/session_manager.py`, `web_agent/utils.py`

These three files are genuinely mature and have clearly survived multiple review passes — the SSRF host gate, DNS-rebinding TTL cache, obfuscated-IP-literal handling, cross-platform absolute-path detection, retry/backoff, budget tracker, and the persistent-context lifecycle are all carefully built and well-documented. That said, the hunt turned up several real, non-cosmetic problems. The single most concrete bug is in utils.is_private_address: socket.getaddrinfo can raise UnicodeError (an idna codec error, which is a ValueError subclass, NOT OSError), so the except (gaierror, OSError) in _resolve_host_addresses lets it escape and crash check_domain_allowed for a malformed host — turning a should-be-BLOCKED into an unhandled exception on the very first line of WebFetcher.fetch. The _NoCloseContextProxy in browser_manager violates the Python __eq__/__hash__ invariant (a proxy compares equal to the raw context it wraps but hashes differently) and additionally cannot be weak-referenced because its __slots__ omits __weakref__ — both are latent footguns directly contradicting the proxy's own docstring promise that context-keyed dicts "behave as if they always saw one context." SessionManager.create can orphan a freshly-built BrowserContext if TabManager construction (outside the try/except) raises, and it holds the global session lock across a page.evaluate round-trip that can stall every other session op. The two unguarded resource-blocking route handlers can raise into Playwright's dispatcher on context teardown. None are remote-code-exec class, but the UnicodeError crash and the eq/hash violation are worth fixing.

### search-skills
*Files:* `web_agent/search_providers.py`, `web_agent/search_engine.py`, `web_agent/domain_skills.py`, `web_agent/builtin_skills/__init__.py`, `web_agent/builtin_skills/github_release_download/__init__.py`, `web_agent/builtin_skills/sec_gov_filing_search/__init__.py`, `web_agent/builtin_skills/ec_europa_document_search/__init__.py`

These files are in good shape overall — the prior review passes are visible (literal-private-IP filtering in SearXNG, MAX_EXTRA_INPUTS cap, single-shot unavailable-provider logging, correct CancelledError propagation since the broad excepts only catch Exception, clean page-context lifetimes via async-with). No critical RCE/path-traversal/secret-leak bugs survive here. The one genuinely dangerous finding is in the GitHub skill: its query sanitizer's comment and docstring claim it strips `site:` and `OR`, but the regex strips neither, so a prompt-injected input can inject `site:evil.com` and escape the github.com scope — and the code itself documents that under default-open SafetyConfig the query scope is "the only fence". The EC skill compounds the theme by "confining" results with a substring check on the whole URL rather than a hostname check, which a crafted URL trivially defeats. The rest are defense-in-depth inconsistencies (private-IP result filtering exists only for SearXNG, not DDGS/Playwright), unbounded `max_results`, and minor edge/footgun issues. The skill-registry, parser, and search-orchestrator core logic are correct and race-free (registry dicts are write-once at construction).

### infra-small
*Files:* `web_agent/trace_recorder.py`, `web_agent/workspace.py`, `web_agent/debug.py`, `web_agent/cache.py`, `web_agent/rate_limiter.py`, `web_agent/robots.py`, `web_agent/audit.py`, `web_agent/ownership.py`, `web_agent/correlation.py`, `web_agent/exceptions.py`, `web_agent/main.py`, `web_agent/__init__.py`, `web_agent/__main__.py`

These files are, on the whole, the most carefully-hardened part of the codebase — path traversal, SSRF gating, redaction, and cancellation have all clearly been worked over in prior passes, and most of the "obvious" holes are genuinely closed. The residual issues are subtle and fall into three buckets. (1) Resource management: every per-host/per-session bookkeeping dict in this slice (RateLimiter._next_allowed/_locks/_429_counts, RobotsChecker._cache/_locks, SessionTraceRecorder._counters) grows for the entire process lifetime with no eviction — fine for a CLI run, a slow leak for the long-lived MCP-server Agent. (2) cache.py is the odd module out: while trace_recorder/audit went to the trouble of moving disk I/O off the loop with asyncio.to_thread, DiskCache does every read/write/glob/stat/unlink synchronously on the event loop while holding its lock, and its non-atomic full-file writes leave permanently-unparseable cache files on crash. (3) Redaction/replay coherence: the v1.6.14 B-8 trace redaction map only covers fill/type/type_text and misses action.evaluate (arbitrary JS that routinely carries tokens), so a real secret class still lands on disk verbatim; and the same redaction silently breaks replay fidelity (a recorded password replays as the literal "***REDACTED***"). robots.py is a blind-SSRF lever that depends entirely on its callers gating the host first (they do today), and the workspace markdown_skills_only gate checks an un-normalized path so ".." escapes the domain-skills/ confinement (though it stays inside the workspace and stays .md). None of these is a slam-dunk RCE, but several are real and worth fixing.
