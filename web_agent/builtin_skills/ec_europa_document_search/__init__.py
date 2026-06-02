"""v1.6.7 bundled skill: European Commission document register search.

Drives Agent.search_and_extract scoped to the EC subdomains and
returns the top extracted documents as a compact JSON-encoded list
(the output schema can't express list[dict] today).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from web_agent.utils import _matches_domain, _normalize_host

if TYPE_CHECKING:
    from web_agent.agent import Agent


_EC_HOSTS = (
    "ec.europa.eu",
    "eur-lex.europa.eu",
    "finance.ec.europa.eu",
)


async def run(agent: Agent, url: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Search EC subdomains for policy documents matching ``query``."""
    query = inputs.get("query", "")
    # v1.6.16 EC-2: a negative/zero/garbage max_results previously slipped
    # through and made the ``len(docs) >= max_results`` break fire after the
    # first doc (silently truncating to a single result). Coerce defensively,
    # fall back to the default for nonsensical values, and cap to a ceiling.
    try:
        max_results = int(inputs.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5
    if max_results < 1:
        max_results = 5
    max_results = min(max_results, 50)

    # Site-restrict to the EC domains we care about.
    composed_query = f"({' OR '.join(f'site:{h}' for h in _EC_HOSTS)}) {query}"

    results = await agent.search_and_extract(composed_query, max_results=max_results)

    docs: list[dict[str, str]] = []
    for item in results.pages:
        # Drop non-EC hosts that may slip past site: operators. Match on
        # the *parsed hostname* at label boundaries (exact host or a
        # subdomain), not a substring of the raw URL -- otherwise
        # ``https://ec.europa.eu.evil.com/`` or
        # ``https://evil.com/?x=ec.europa.eu`` would falsely pass.
        host = _normalize_host(item.url)
        if not host or not any(_matches_domain(host, d) for d in _EC_HOSTS):
            continue
        docs.append(
            {
                "title": item.title or "",
                "url": item.url,
                "description": item.description or "",
                "body": (item.content or "")[:5000],  # cap each doc
            }
        )
        if len(docs) >= max_results:
            break

    return {
        "documents": json.dumps(docs),
        "count": str(len(docs)),
    }
