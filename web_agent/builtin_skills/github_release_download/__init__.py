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
# (quoted phrases, grouping, pipe-as-OR). Stripping these blocks
# scope-escape attacks where a prompt-injected input contains something
# like '" OR site:evil.com"' that would expand beyond github.com.
_QUERY_OPERATOR_CHARS = re.compile(r"[\"\'()\[\]|]")

# Field operators (``site:``, ``inurl:``, ``intitle:``, ...) let an
# injected term re-scope the search to an attacker host (e.g.
# ``site:evil.com``), escaping the intended ``site:github.com`` fence.
# Match the operator *keyword* plus its trailing colon, case-insensitively,
# regardless of whitespace around the colon, so ``site :`` / ``SITE:`` can't
# evade it. We drop only the operator token; any bare value left behind is
# harmless plain text.
_QUERY_FIELD_OPERATORS = re.compile(
    r"(?i)\b(?:site|inurl|intitle|intext|allintext|filetype|ext|cache|related|link)\s*:"
)

# Standalone boolean operators a search engine honours (``OR``/``AND``/
# ``NOT``). Matched as whole words, case-insensitively, so legitimate
# substrings (``ORchestra``, ``transformer``) are preserved.
_QUERY_BOOLEAN_OPERATORS = re.compile(r"(?i)\b(?:OR|AND|NOT)\b")

# Collapse the runs of whitespace that operator removal can leave behind.
_QUERY_WHITESPACE = re.compile(r"\s+")


def _sanitize_query_term(s: str) -> str:
    """Strip search-operator syntax from a user-supplied term.

    Removes (case-insensitively, robustly):
      * quote / paren / bracket / pipe metacharacters,
      * field operators such as ``site:`` (and ``inurl:``, ``filetype:``,
        ...), which would otherwise re-scope the search,
      * standalone boolean operators ``OR`` / ``AND`` / ``NOT``.

    Ordinary query words (including ones that merely *contain* ``or``)
    are kept intact. Collapses the resulting whitespace.
    """
    s = _QUERY_OPERATOR_CHARS.sub("", s)
    s = _QUERY_FIELD_OPERATORS.sub(" ", s)
    s = _QUERY_BOOLEAN_OPERATORS.sub(" ", s)
    return _QUERY_WHITESPACE.sub(" ", s).strip()


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
    # of search operators (site: and other field operators, the boolean
    # OR/AND/NOT, quotes, parens). The downloader's domain allowlist
    # would still catch a non-github asset URL, but only when an
    # allowlist is configured -- under default-open SafetyConfig the
    # only fence is this query scope.
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
