"""v1.6.7 bundled skill: GitHub release asset download.

Composes Agent.find_and_download_file against the releases page,
filtering by an asset pattern. The Agent's existing safety stack
(domain check, post-redirect SSRF re-check, size cap) handles the
heavy lifting -- this skill is mainly about query construction.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from web_agent.agent import Agent


# Characters that have meaning to search-engine query parsers
# (site:, OR, quoted phrases, grouping). Stripping these blocks
# scope-escape attacks where a prompt-injected input contains something
# like '" OR site:evil.com"' that would expand beyond github.com.
_QUERY_OPERATOR_CHARS = re.compile(r'[\"\'()\[\]|]')


def _sanitize_query_term(s: str) -> str:
    """Strip search-operator metacharacters from a user-supplied term."""
    return _QUERY_OPERATOR_CHARS.sub("", s).strip()


async def run(agent: Agent, url: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Download a GitHub release asset matching ``asset_pattern``.

    Inputs:
        repo: 'owner/name' slug.
        asset_pattern: substring the asset filename must contain.
        tag: specific release tag (empty = latest).

    Returns:
        {release_tag, asset_url, asset_name, downloaded_path}
    """
    # Sanitize every user-supplied query term to block prompt-injection
    # of search operators (site:, OR, quotes, parens). The downloader's
    # domain allowlist would still catch a non-github asset URL, but
    # only when an allowlist is configured -- under default-open
    # SafetyConfig the only fence is this query scope.
    repo = _sanitize_query_term(inputs.get("repo", ""))
    asset_pattern = _sanitize_query_term(inputs.get("asset_pattern", "") or "")
    tag = _sanitize_query_term(inputs.get("tag", "") or "")

    # Compose a targeted query so the existing find_and_download_file
    # recipe surfaces the right asset URL.
    if tag:
        query = f"site:github.com {repo} releases tag {tag} {asset_pattern}".strip()
    else:
        query = f"site:github.com {repo} releases latest {asset_pattern}".strip()

    download = await agent.find_and_download_file(query)

    # find_and_download_file returns a DownloadResult shape.
    asset_name = ""
    if download.filepath:
        # Best-effort extract of the basename from the local path.
        import os

        asset_name = os.path.basename(download.filepath)

    # Best-effort tag extraction from the asset URL.
    release_tag = tag
    if not release_tag and download.url and "/releases/download/" in download.url:
        try:
            release_tag = download.url.split("/releases/download/", 1)[1].split("/", 1)[0]
        except Exception:
            release_tag = ""

    return {
        "release_tag": release_tag,
        "asset_url": download.url or "",
        "asset_name": asset_name,
        "downloaded_path": download.filepath or "",
    }
