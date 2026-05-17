"""v1.6.7 bundled skill: SEC EDGAR filing search.

Drives Agent's `search_and_extract` against EDGAR's full-text search,
filters for the requested form type, fetches and extracts the body of
the first matching filing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from web_agent.agent import Agent


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
    company = inputs.get("company", "")
    form_type = inputs.get("form_type") or "10-K"

    query = f'site:sec.gov "{company}" {form_type}'
    results = await agent.search_and_extract(query, max_results=5)

    pages = results.pages
    if not pages:
        return {
            "filing_url": "",
            "form_type": form_type,
            "filing_date": "",
            "accession_number": "",
            "extracted_text": "",
        }

    # Take the first result whose URL clearly looks like an EDGAR
    # filing document; else fall back to the first page.
    chosen = None
    for item in pages:
        if "/Archives/edgar/data/" in item.url:
            chosen = item
            break
    if chosen is None:
        chosen = pages[0]

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
