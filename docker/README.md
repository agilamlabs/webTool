# Docker: production container for the web_agent MCP server + CLI

A **non-root** image that runs the `web_agent` Model Context Protocol
(MCP) server out of the box, and doubles as a CLI runner. Built on the
official Playwright Python base image, so Chromium, all of its system
dependencies, and the full font stack (Liberation / Noto / CJK / emoji) are
baked in.

> This image deploys the MCP **server** (`web-agent-mcp`). For the
> self-hosted **SearXNG** search backend, see the separate
> [`docker/searxng/`](searxng/) quickstart -- the two are independent.

## Contents

- [`Dockerfile`](Dockerfile) -- non-root, healthchecked, browsers from the Playwright base image.
- [`docker-compose.yml`](docker-compose.yml) -- build + config surface for the server.

## TL;DR

```bash
# Build (context is the REPO ROOT, not docker/):
docker build -f docker/Dockerfile -t web-agent-toolkit:latest .

# Sanity-check the image (no browser launch, <1s):
docker run --rm web-agent-toolkit:latest web-agent doctor --quick

# Run the CLI:
docker run --rm web-agent-toolkit:latest web-agent search "python web scraping"

# Run the MCP server interactively (stdio -- see the honesty note below):
docker run --rm -i web-agent-toolkit:latest
```

## Honesty note: stdio MCP server vs. a network service

The web_agent MCP server speaks MCP over **stdio**
(`mcp.run(transport="stdio")`). It is **not** an HTTP/network daemon -- there
are no ports to publish. The MCP **client** (Claude Desktop, Claude Code,
Cursor, ...) is what launches the server process and talks to it over
stdin/stdout. Consequences:

- `docker run -i` (interactive, stdin attached) is the correct way to run
  the server -- the client pipes JSON-RPC into the container's stdin.
- A bare `docker compose up` of the server with nothing attached to stdin
  just idles (or exits at EOF). That's expected, not a bug.
- The Compose file is therefore framed as a **build + configuration
  surface** (volumes, env, caps) plus a `docker compose run` convenience,
  rather than a pretend web service. See the comments in
  [`docker-compose.yml`](docker-compose.yml).

## Wire into an MCP client (Claude Desktop / Claude Code / Cursor)

Point the client at a `docker run -i` invocation. Example
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "web_agent": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "web-agent-workspace:/workspace",
        "web-agent-toolkit:latest"
      ]
    }
  }
}
```

- `-i` is **required** (the client speaks over stdin/stdout).
- `--rm` discards the container per session; the named volume keeps
  downloads/screenshots/output across sessions.
- The image's `ENTRYPOINT` is `web-agent-mcp`, so no command is needed.

To pass config / proxy env to the client-spawned server, add `-e` flags:

```json
"args": [
  "run", "--rm", "-i",
  "-e", "WEB_AGENT_PROXY__SERVER=http://proxy.internal:3128",
  "-v", "web-agent-workspace:/workspace",
  "web-agent-toolkit:latest"
]
```

## Run the CLI

The same image runs the CLI by overriding the command:

```bash
docker run --rm web-agent-toolkit:latest web-agent doctor
docker run --rm web-agent-toolkit:latest web-agent search "query" --max-results 5
docker run --rm \
  -v web-agent-workspace:/workspace \
  web-agent-toolkit:latest \
  web-agent download "https://example.com/report.pdf"
```

Files land under `/workspace` (downloads, screenshots, output, debug); mount
a volume there to keep them.

## Mounting a YAML config

The toolkit reads a YAML config via the `WEB_AGENT_CONFIG` env var:

```bash
docker run --rm -i \
  -v "$PWD/config:/config:ro" \
  -e WEB_AGENT_CONFIG=/config/web_agent.yaml \
  -v web-agent-workspace:/workspace \
  web-agent-toolkit:latest
```

`web-agent doctor` validates the file via its `config_file_parse` check.
Individual settings can also be set directly with `WEB_AGENT_*` env vars
(e.g. `WEB_AGENT_SEARCH__MAX_RESULTS=20`,
`WEB_AGENT_BROWSER__HEADLESS=true`) -- no file required.

## Proxy / environment

`ProxyConfig` is inactive unless `WEB_AGENT_PROXY__SERVER` is set:

```bash
docker run --rm -i \
  -e WEB_AGENT_PROXY__SERVER="http://proxy.internal:3128" \
  -e WEB_AGENT_PROXY__USERNAME="proxyuser" \
  -e WEB_AGENT_PROXY__PASSWORD="proxypass" \
  -e WEB_AGENT_PROXY__BYPASS="localhost,127.0.0.1,.internal" \
  web-agent-toolkit:latest
```

Scheme may be `http`, `https`, or `socks5`. The proxy threads through every
Chromium launch and the httpx side-paths.

## Security: non-root + the Chromium sandbox trade-off

**The image runs as a non-root user (`pwuser`, uid 1001 -- the Playwright
base image's purpose-built unprivileged account).** It owns a writable
`/workspace` (declared as a `VOLUME`) and `/home/pwuser`; nothing runs as
root at runtime.

**The Chromium sandbox trade-off (read this):** Chromium's strongest
isolation uses a **setuid sandbox**, which a non-root container cannot use
without extra kernel capability. To keep the image working out of the box,
the toolkit **auto-detects the container** (via `/.dockerenv`) and falls
back to `--no-sandbox` -- the same behaviour `web-agent doctor` reports under
its `container_sandbox` check. The image does **not** bake in `--no-sandbox`
beyond that app-level auto-detection.

If your threat model needs the Chromium sandbox **preserved** instead of
disabled, run with one of these and set
`WEB_AGENT_BROWSER__DISABLE_CHROMIUM_SANDBOX=false`:

- **Preferred -- user-namespace sandbox (no `SYS_ADMIN`):**

  ```bash
  docker run --rm -i \
    --security-opt seccomp=unconfined \
    -e WEB_AGENT_BROWSER__DISABLE_CHROMIUM_SANDBOX=false \
    web-agent-toolkit:latest
  ```

  Requires the host to permit unprivileged user namespaces.

- **Fallback -- grant `SYS_ADMIN` (broader privilege; use only if the above
  is unavailable):**

  ```bash
  docker run --rm -i \
    --cap-add=SYS_ADMIN \
    -e WEB_AGENT_BROWSER__DISABLE_CHROMIUM_SANDBOX=false \
    web-agent-toolkit:latest
  ```

`--cap-add=SYS_ADMIN` is the broadest of the two; prefer the
user-namespace option where the host supports it.

## Healthcheck

The image ships a `HEALTHCHECK` that runs `web-agent doctor --quick`:

- The v1.7.0 quick path takes **<1s**, launches **no** browser, and **fails
  when the Chromium executable is missing** -- so a broken browser stack
  marks the container **unhealthy**.
- The CLI exits non-zero (`2`) only on an `unusable` summary. Warnings
  (including the `not_running_as_root` advisory, or no outbound network in
  an air-gapped deployment) do **not** trip the healthcheck.

Check it:

```bash
docker inspect --format '{{.State.Health.Status}}' <container>
```

## Docker Compose

```bash
# Build:
docker compose -f docker/docker-compose.yml build

# One-shot CLI:
docker compose -f docker/docker-compose.yml run --rm web-agent-mcp web-agent doctor

# Interactive MCP server (stdio):
docker compose -f docker/docker-compose.yml run --rm web-agent-mcp
```

The Compose file carries the volume, env passthrough (including commented
`WEB_AGENT_CONFIG` + `WEB_AGENT_PROXY__SERVER` examples), the healthcheck,
and the commented `security_opt` / `cap_add` options for the
sandbox-preserving run.

## Image notes

- **Base:** `mcr.microsoft.com/playwright/python` pinned via the
  `PLAYWRIGHT_VERSION` build arg (default a tag compatible with
  `playwright>=1.55.0`). Override at build time:

  ```bash
  docker build -f docker/Dockerfile \
    --build-arg PLAYWRIGHT_VERSION=v1.55.0-noble \
    -t web-agent-toolkit:latest .
  ```

- **Extras installed:** `[mcp]` (the server) + `[binary]` (PDF via
  `pdfplumber`/`pypdf`, XLSX, DOCX). CSV is stdlib.
- **Browser coherence:** the build re-runs `playwright install chromium`
  after `pip install` so the browser binary matches the Playwright version
  pip resolves inside the image.
- **OCI labels** (`org.opencontainers.image.*`) are set for title, source,
  license, and docs.
