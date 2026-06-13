# webTool

[![CI](https://github.com/agilamlabs/webTool/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/agilamlabs/webTool/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

**A professional, local Playwright-based agentic web toolkit** — search the web, fetch JavaScript-heavy pages, extract content (including schema-guided fields), download files, and fully automate a real browser, all through one clean async Python API (package `web_agent`).

It is built as a backend/tool for AI agents that must use the **real** web reliably. webTool is the hardened, all-in-one **local alternative to cloud scrape/agent APIs** (Firecrawl, Browserbase, ScrapeGraphAI, and friends): no per-call billing, your data and credentials never leave the machine, it runs in-VPC / on an intranet, and the real-world hardening agents need on the live web — bot-wall honesty, prompt-injection containment, SSRF/robots/rate-limit safety, session+auth reuse — is built in rather than bolted on.

---

## Key capabilities

- **Agentic search** — a multi-provider chain (SearXNG → DuckDuckGo → Playwright) with a per-provider circuit breaker, plus a cheap links-only `search` primitive that skips fetch/extract.
- **Fetch + 3-tier extraction** — render with headless Chromium, then `trafilatura → BeautifulSoup → raw` for HTML, emitting clean markdown, JSON-LD `structured_data`, and **schema-guided field extraction**.
- **Schema-guided fields** — `extract_fields(url, {field: hint})` deterministically maps your fields onto a page's structured signals (JSON-LD / OpenGraph / meta / microdata / labelled DOM); an optional LLM hook fills the rest.
- **Bot-wall / challenge detection** — structural detection of Cloudflare / DataDome / Akamai / PerimeterX / reCAPTCHA / hCaptcha returns an honest `BLOCKED` with an actionable reason, never SUCCESS-with-garbage.
- **Pluggable CAPTCHA resolver hook** — wire `Agent(captcha_resolver=...)` to plug in your own strategy (human handoff, headed handoff, a paid solver API) on a detected wall; the hook's verdict is advisory — the fetcher re-detects to confirm before clearing it.
- **Prompt-injection containment** — hidden-from-humans content (invisible Unicode + `display:none`/off-screen DOM) is stripped before extraction; visible injection is flagged with an advisory risk rating.
- **Token-efficient responses** — content-returning surfaces emit a single representation (markdown by default) with `offset` / `next_offset` paging instead of duplicated megabyte dumps.
- **19 browser actions + set-of-marks targeting** — click / type / scroll / upload / drag / iframe / shadow-DOM and more; `observe()` returns numbered interactive elements you can act on by `ref` instead of guessing selectors.
- **Persistent sessions + auth** — export/import a logged-in `storage_state`: a human logs in once (password / 2FA / CAPTCHA), the agent automates afterwards.
- **Pagination + infinite-scroll collection** — `collect_across_pages` walks `next_link` / `page_param` / `scroll` listings, re-gating safety on every page.
- **PDF / XLSX / DOCX extraction** — `pdfplumber` tables + per-page markers, XLSX, and DOCX, all surfaced as markdown.
- **Proxy + fingerprint coherence** — operator-controlled proxy (http/https/socks5) threaded through every Chromium and httpx path, with UA/OS/locale coherence.
- **In-process observability** — counters + distributions via a `MetricsSnapshot`, plus correlation-ID logging that ties result, audit, and trace together.
- **Safety stack** — SSRF / private-IP egress blocking, robots.txt obedience, per-host rate limiting, path-traversal protection, and an opt-in JSONL audit log.
- **Isolation + CDP on by default** — every `Agent` launches an isolated ephemeral browser profile with a loopback CDP debug port (see the security note in [Configuration](#configuration)).

---

## Install

```bash
# Core (Python API)
pip install -e ".[dev]"
playwright install chromium

# Optional: MCP server (exposes the toolkit to MCP clients)
pip install -e ".[mcp]"

# Optional: PDF (pdfplumber + pypdf) / XLSX / DOCX extractors
pip install -e ".[binary]"
```

Requires Python 3.10+. The package has no system dependencies beyond what `playwright install` pulls in. CSV extraction is stdlib (no extra needed).

---

## Quickstart (Python API)

Everything starts inside `async with Agent() as agent:` — the `Agent` owns the Playwright lifecycle. Public methods return rich result models by default; pass `strict=True` to opt into exceptions.

```python
from web_agent import Agent

async with Agent() as agent:
    # Search the top results and extract their content in one call
    result = await agent.search_and_extract("python web scraping", max_results=5)
    for page in result.extractions:
        print(page.url, len(page.content))

    # Fetch and extract a single JS-heavy page -> clean markdown + JSON-LD
    page = await agent.fetch_and_extract("https://example.com/article")
    print(page.content)            # markdown
    print(page.structured_data)    # JSON-LD blocks
```

**Schema-guided field extraction** — map your fields onto the page's structured signals, deterministically (no LLM call):

```python
result = await agent.extract_fields(
    "https://store.example.com/p/123",
    {"name": "product name", "price": "current price", "sku": "product id"},
)
print(result.fields)         # {"name": "...", "price": "...", "sku": "..."}
print(result.field_sources)  # {"name": "json-ld", "price": "opengraph", ...}
print(result.unresolved)     # fields no structured signal could fill

# For freeform fields, a calling agent can supply its own model:
result = await agent.extract_fields(url, schema, llm_extractor=my_llm_hook)
```

`extract_fields` returns a `StructuredExtractionResult` with `.fields` (resolved field → value), `.field_sources` (the signal each value came from: `json-ld` / `opengraph` / `meta` / `microdata` / `dom`), and `.unresolved`. It is best-effort structured-signal extraction — excellent for product / article / org / event pages that ship JSON-LD or OpenGraph — and the optional `llm_extractor=` hook (Python only) is the local, no-API answer to Firecrawl / ScrapeGraphAI schema extraction.

**Collect across pages** (pagination / infinite scroll):

```python
collection = await agent.collect_across_pages(
    "https://news.example.com/latest", strategy="next_link", max_pages=5,
)
print(collection.stopped_reason, len(collection.pages))
```

**Set-of-marks loop** — observe, then act by `ref` (no brittle selectors):

```python
session = await agent.create_session()
observed = await agent.observe("https://example.com/login", session_id=session.session_id)
for el in observed.elements:
    print(el.ref, el.role, el.name)   # e1 textbox "Email", e2 button "Sign in", ...

from web_agent import LocatorSpec
await agent.interact(
    session_id=session.session_id,
    actions=[{"action": "click", "selector": {"ref": "e2"}}],  # act on the current tab in place
)
```

**Auth reuse** — log in once, automate later:

```python
# After a human logs in within a session:
await agent.export_session_state(session_id, "auth.json")

# In a later process:
session = await agent.import_session_state("auth.json")
page = await agent.fetch_and_extract("https://app.example.com/dashboard",
                                     session_id=session.session_id)
```

---

## MCP server

webTool ships a [Model Context Protocol](https://modelcontextprotocol.io) server that exposes the toolkit to any MCP client.

```bash
pip install -e ".[mcp]"
web-agent-mcp          # speaks MCP over stdio
```

Wire it into Claude Desktop / Claude Code / Cursor via an `mcpServers` entry:

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "web-agent-mcp"
    }
  }
}
```

The server exposes **45 tools**, grouped:

- **Search** — `web_search`, `web_search_links`, `web_search_best`
- **Fetch + extract** — `web_fetch`, `web_extract_fields`, `web_research`, `web_fill_form_and_extract`, `web_collect_pages`
- **Download** — `web_download`, `web_find_and_download`, `web_print_page_as_pdf`
- **Browser automation** — `web_interact`, `web_click_xy`, `web_type_text`, `web_press_key`, `web_screenshot`, `web_handle_dialog`, `web_select_dropdown`, `web_upload_file`, `web_drag_and_drop`, `web_scroll_until_text`, `web_scroll_to_bottom`, `web_click_inside_iframe`, `web_click_shadow_dom`
- **Sessions + auth** — `web_create_session`, `web_list_sessions`, `web_close_session`, `web_export_session`, `web_import_session`, `web_list_tabs`, `web_current_tab`, `web_new_tab`, `web_switch_tab`, `web_close_tab`
- **Observe + diagnostics** — `web_observe`, `web_doctor`, `web_get_cdp_endpoint`, `web_get_owned_cdp_connection_info`, `web_get_remote_cdp_url`, `web_list_traces`, `web_replay_trace`
- **Recipes (domain skills)** — `list_domain_skills`, `get_domain_skill`, `apply_domain_skill`
- **Metrics** — `web_metrics`

> `web_extract_fields(url, schema)` returns the deterministically-resolved fields + their sources (no LLM call). The LLM hook is Python-API only.

MCP content tools return one representation capped at `extraction.default_max_chars` (40000), with `max_chars` / `offset` / `format` per call — the Python API stays uncapped.

---

## Agent integration

webTool is designed to be the web backend an agent reaches for. Three integration paths:

### (a) As an MCP server for any MCP client

Any MCP-compatible client (Claude Desktop, Claude Code, Cursor, OpenAI Codex, …) can drive webTool with the `mcpServers` config above — no glue code. The 45 tools appear directly in the client's tool list.

### (b) As a Python backend inside an agent framework

Inside LangGraph, [OpenClaw](https://github.com/openclaw/openclaw), or a hand-rolled async loop, call `Agent` methods directly — the result models are already structured for a model to consume:

```python
from web_agent import Agent

class WebBackend:
    """Thin tool layer an agent framework can call."""
    def __init__(self, agent: Agent):
        self._agent = agent

    async def search(self, query: str) -> list[dict]:
        resp = await self._agent.search(query, max_results=5)   # links-only, cheap
        return [item.model_dump() for item in resp.results]

    async def read(self, url: str) -> str:
        page = await self._agent.fetch_and_extract(url)
        return page.content                                     # markdown

# One Agent owns the browser for the whole run:
async with Agent() as agent:
    backend = WebBackend(agent)
    hits = await backend.search("vector database benchmarks")
    body = await backend.read(hits[0]["url"])
```

### (c) With function-calling LLMs (including Hermes-style models)

The MCP tool schemas — and the `Agent` methods behind them — map directly onto a function-calling tool list, so any tool-calling model (e.g. [Nous Hermes](https://huggingface.co/NousResearch) or any agentic LLM) can invoke webTool operations. Expose the operations you want as tool specs, then route the model's tool call to the matching `Agent` method:

```python
# 1. Tool specs you hand the model (OpenAI / Hermes function-calling shape):
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web; returns title/url/snippet for each hit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and return its main content as markdown.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]

# 2. Dispatch a model tool call to the matching Agent method:
async def dispatch(agent, name: str, args: dict) -> dict:
    if name == "web_search":
        resp = await agent.search(args["query"], max_results=args.get("max_results", 5))
        return {"results": [r.model_dump() for r in resp.results]}
    if name == "web_fetch":
        page = await agent.fetch_and_extract(args["url"])
        return {"content": page.content, "status": page.fetch_status}
    raise ValueError(f"unknown tool: {name}")

# In your loop: parse the model's tool_call -> await dispatch(agent, call.name, call.arguments)
# -> append the JSON result back as the tool message -> let the model continue.
```

Because the tool surface is plain JSON-schema in and result-model JSON out, the same specs work whether the model speaks the OpenAI function-calling format, the Hermes tool-call format, or your own.

---

## Configuration

`AppConfig` is **programmatic by default** — no file required. You can also load a YAML config (`WEB_AGENT_CONFIG=/path/web_agent.yaml`) and override any field with `WEB_AGENT_<SECTION>__<FIELD>` env vars.

```python
from web_agent import Agent, AppConfig

config = AppConfig(
    browser={"headless": True, "cdp_enabled": False},   # see the CDP note below
    fetch={"retry_policy": "fast"},
    safety={"allowed_domains": ["example.com"], "respect_robots_txt": True},
)
async with Agent(config) as agent:
    ...
```

Security-relevant defaults to know:

- **Isolation + CDP are ON by default.** Every `Agent` launches an isolated ephemeral profile with a loopback CDP debug port. That port is **loopback-bound but unauthenticated** — any local process that can reach loopback can drive the browser. **Set `browser.cdp_enabled=false` on shared / multi-tenant hosts** or when handling sensitive authenticated sessions.
- **Proxy** is inactive unless configured: `WEB_AGENT_PROXY__SERVER` (+ `__USERNAME` / `__PASSWORD` / `__BYPASS`), scheme `http` / `https` / `socks5`.
- **Safety stack** — SSRF / private-IP egress blocking, robots.txt obedience, and per-host rate limiting are on by default; downloads, JS evaluation, and form submission are gated by explicit `safety.allow_*` flags (`safety.safe_mode=True` forces them all off).

See [AGENTS.md](AGENTS.md) for the full configuration surface and [SECURITY.md](SECURITY.md) for the threat model and hardening recommendations.

---

## CAPTCHA / challenge resolution

webTool **detects** bot walls and, by default, returns an honest `BLOCKED` — it ships **no solver of its own**. When you have a resolution strategy (a human in the loop, a headed-browser handoff, a paid solver API, an audio-CAPTCHA transcriber), plug it in as a hook and webTool calls it on a detected wall — across **every** fetch path (search, research, collection, download):

```python
from web_agent import Agent, CaptchaContext, CaptchaResolution

async def resolve(ctx: CaptchaContext) -> CaptchaResolution:
    # ctx.page is the LIVE Playwright page parked on the wall;
    # ctx.challenge.vendor / .kind tell you what you're up against.
    token = await my_solver_api(ctx.challenge.vendor, ctx.url)
    await ctx.page.evaluate(_INJECT_TOKEN_JS, token)
    await ctx.page.click("button[type=submit]")
    return CaptchaResolution(resolved=True, method="my-solver")   # or just: return True

async with Agent(captcha_resolver=resolve) as agent:   # or: agent.captcha_resolver = resolve
    page = await agent.fetch_and_extract("https://gated.example/report")
```

**The hook's verdict is advisory — re-detection is authoritative.** After your hook runs, webTool re-reads the live page and re-runs structural detection; the wall is only cleared when detection itself comes back clean. A hook that returns `resolved=True` while the interstitial still stands does **not** turn a `BLOCKED` into a `SUCCESS`. The loop is bounded by `fetch.captcha_max_attempts`, an async hook by `fetch.captcha_attempt_timeout_s`, and a hook that raises / times out / leaves the page uncapturable is isolated — the wall just stands. Like the `llm_extractor` hook, the resolver is **Python-only and never exposed over the MCP wire** (it runs caller code); an MCP operator wires it where they construct the `Agent`.

---

## Docker

A production, **non-root** image runs the MCP server (and doubles as a CLI runner), with Chromium and its system/font dependencies baked in:

```bash
docker build -f docker/Dockerfile -t web-agent-toolkit:latest .
docker run --rm -i web-agent-toolkit:latest          # MCP server over stdio
```

See [docker/README.md](docker/README.md) for MCP-client wiring, config/proxy mounting, and the Chromium-sandbox trade-off.

---

## Links

- [CHANGELOG.md](CHANGELOG.md) — release history
- [AGENTS.md](AGENTS.md) — architecture, invariants, and the full public API surface
- [SECURITY.md](SECURITY.md) — threat model, defense-in-depth layers, and hardening guidance
- **License** — [Apache-2.0](LICENSE)
