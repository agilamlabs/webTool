# syntax=docker/dockerfile:1
#
# Production-grade container for web-agent-toolkit (package: web_agent).
#
# Why the official Playwright Python base image
# ---------------------------------------------
# `mcr.microsoft.com/playwright/python` bakes Chromium + every system
# library Chromium needs + the full font stack (Liberation, Noto, CJK,
# emoji) on top of an official Python build. For a browser-automation
# tool that is the pragmatic, well-maintained choice: it removes the
# 40-line `apt-get install` font/dep block a plain `python:3.12-slim`
# would require and keeps us in lockstep with Playwright's own tested
# dependency set. The tag is pinned via the PLAYWRIGHT_VERSION build arg
# to a version compatible with `playwright>=1.55.0` (pyproject floor).
#
# We `playwright install chromium` after `pip install` so the browser
# binary matches the exact Playwright version pip resolves inside the
# image (the base image ships browsers for ITS Playwright; re-installing
# guarantees coherence when pip resolves a newer 1.5x/1.6x).
#
# Single-stage on purpose: the heavy, cache-friendly layer is the base
# image (browsers + system deps + fonts). The package install on top is
# small. A multi-stage `--prefix` split would save little here and risks
# the runtime interpreter not seeing the copied site-packages (so
# `playwright install` can't find its own module). Layer ordering still
# keeps the build cache warm across edits outside web_agent/.
#
# Build context is the REPO ROOT, not docker/:
#   docker build -f docker/Dockerfile -t web-agent-toolkit:latest .

ARG PLAYWRIGHT_VERSION=v1.55.0-noble

FROM mcr.microsoft.com/playwright/python:${PLAYWRIGHT_VERSION}

# OCI image metadata.
LABEL org.opencontainers.image.title="web-agent-toolkit" \
      org.opencontainers.image.description="Production MCP server + CLI for the Playwright-based agentic web toolkit (web_agent)." \
      org.opencontainers.image.source="https://github.com/agilamlabs/webTool" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.documentation="https://github.com/agilamlabs/webTool/blob/main/docker/README.md"

# Runtime environment.
#   PYTHONUNBUFFERED         -- stream logs immediately (critical for stdio MCP)
#   PLAYWRIGHT_BROWSERS_PATH -- the base image's pre-installed browser cache
#   WEB_AGENT_BASE_DIR       -- resolve all relative output dirs under the
#                               writable, VOLUME-mounted /workspace
# The Playwright base image ships a purpose-built non-root user, `pwuser`
# (uid/gid 1001), with browsers it can already read. We run as that user
# rather than minting a colliding one -- it is the image's intended
# unprivileged account and avoids a uid clash with the baked-in pwuser/ubuntu.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    WEB_AGENT_BASE_DIR=/workspace \
    HOME=/home/pwuser

WORKDIR /opt/web-agent

# Build metadata pip needs first (README.md = long_description;
# web_agent/__init__.py carries the dynamic __version__). Copying the
# package before `pip install` keeps the heavy base layer cached across
# edits to anything OUTSIDE web_agent/.
COPY pyproject.toml README.md LICENSE ./
COPY web_agent/ ./web_agent/

# Install the package with the extras a deployed MCP server needs:
#   [mcp]    -- the web-agent-mcp server entry point
#   [binary] -- PDF (pdfplumber/pypdf) + XLSX + DOCX extractors
# then re-align the Chromium binary with the pip-resolved Playwright.
RUN pip install ".[mcp,binary]" \
    && python -m playwright install chromium

# A workspace owned by the non-root pwuser. The toolkit writes under
# base_dir/{downloads,screenshots,debug,output,.webtool*}; WEB_AGENT_BASE_DIR
# points those at /workspace so a single VOLUME captures all runtime output.
RUN mkdir -p /workspace /home/pwuser \
    && chown -R pwuser:pwuser /workspace /home/pwuser

WORKDIR /workspace
USER pwuser

# Declare the writable runtime surface as a volume so output persists /
# stays out of the writable container layer.
VOLUME ["/workspace"]

# Healthcheck: the v1.7.0 `doctor --quick` path is <1s, launches NO
# browser, and now fails when the Chromium executable is missing. The CLI
# exits non-zero only on an `unusable` summary, so a broken browser stack
# marks the container unhealthy (warnings such as the root advisory or an
# air-gapped network do not trip it).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["web-agent", "doctor", "--quick"]

# Default: launch the MCP server (stdio transport). An MCP client
# (Claude Desktop / Code / Cursor) spawns the container with `-i` and
# speaks JSON-RPC over stdin/stdout -- see docker/README.md.
#
# To run the CLI instead, override the command, e.g.:
#   docker run --rm web-agent-toolkit:latest web-agent search "query"
#   docker run --rm web-agent-toolkit:latest web-agent doctor
ENTRYPOINT ["web-agent-mcp"]
