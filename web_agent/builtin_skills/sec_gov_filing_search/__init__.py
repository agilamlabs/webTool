"""v1.6.7 bundled skill: SEC EDGAR filing search.

Drives Agent's `search_and_extract` against EDGAR's full-text search,
filters for the requested form type, fetches and extracts the body of
the first matching filing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from web_agent.builtin_skills.github_release_download import _sanitize_query_term
from web_agent.utils import _matches_domain, _normalize_host

if TYPE_CHECKING:
    from web_agent.agent import Agent

# Hosts a legitimate SEC filing can live on. Used as a defense-in-depth
# post-filter (mirrors the ec_europa_document_search skill).
_SEC_HOSTS = ("sec.gov",)


async def run(agent: Agent, url: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Find a company's most recent SEC filing of ``form_type``.

    Inputs:
        company: company name or CIK (validated upstream as str)
        form_type: SEC form type, default "10-K"

    Returns dict matching the skill's output_schema:
        {filing_url, form_type, filing_date, accession_number, extracted_text}

    Strategy: use the Agent's search_and_extract recipe scoped to
    ``site:sec.gov`` so the existing safety / extraction / ranking
    pipeline does the heavy lifting. The skill's job is to compose the
    query and shape the output -- not to reimplement EDGAR navigation.
    """
    # v1.6.16 deep-review fix: sanitize the caller-supplied terms (strip
    # search-operator syntax -- ``site:`` / quotes / boolean OR -- via the
    # SAME shared sanitizer the github_release_download skill uses). Without
    # this a prompt-injection-steered ``company`` like ``'" OR site:evil.com'``
    # could escape the ``site:sec.gov`` fence and surface attacker pages (the
    # downstream domain allowlist only catches them when one is configured).
    company = _sanitize_query_term(inputs.get("company", "") or "")
    form_type = _sanitize_query_term(inputs.get("form_type") or "10-K") or "10-K"

    query = f"site:sec.gov {company} {form_type}".strip()
    results = await agent.search_and_extract(query, max_results=5)

    # v1.6.16 deep-review fix: defense-in-depth host filter (mirrors the
    # ec_europa skill). Even if a result slips past the ``site:`` operator,
    # only sec.gov pages are surfaced -- an off-domain page is never returned
    # as a "filing". Match on the parsed hostname at label boundaries so
    # ``sec.gov.evil.com`` / ``evil.com/?x=sec.gov`` cannot pass.
    pages = [
        p
        for p in results.pages
        if _normalize_host(p.url)
        and any(_matches_domain(_normalize_host(p.url), h) for h in _SEC_HOSTS)
    ]
    if not pages:
        return {
            "filing_url": "",
            "form_type": form_type,
            "filing_date": "",
            "accession_number": "",
            "extracted_text": "",
        }

    # Take the first result whose URL clearly looks like an EDGAR filing
    # document; else fall back to the first (sec.gov) page.
    chosen = next((p for p in pages if "/Archives/edgar/data/" in p.url), pages[0])

    # Best-effort accession-number extraction from the URL path.
    accession_number = ""
    for part in chosen.url.split("/"):
        if part.replace("-", "").isdigit() and len(part) >= 18:
            accession_number = part
            break

    return {
        "filing_url": chosen.url,
        "form_type": form_type,
        "filing_date": chosen.date or "",
        "accession_number": accession_number,
        "extracted_text": chosen.content or "",
    }
