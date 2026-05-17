"""v1.6.7 bundled skill: European Commission document register search.

Drives Agent.search_and_extract scoped to the EC subdomains and
returns the top extracted documents as a compact JSON-encoded list
(the output schema can't express list[dict] today).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

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
    max_results = int(inputs.get("max_results") or 5)

    # Site-restrict to the EC domains we care about.
    composed_query = f"({' OR '.join(f'site:{h}' for h in _EC_HOSTS)}) {query}"

    results = await agent.search_and_extract(composed_query, max_results=max_results)

    docs: list[dict[str, str]] = []
    for item in results.pages:
        # Drop non-EC hosts that may slip past site: operators
        if not any(host in item.url for host in _EC_HOSTS):
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
