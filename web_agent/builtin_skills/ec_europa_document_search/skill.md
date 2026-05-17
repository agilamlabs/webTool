---
name: document_search
domain: ec.europa.eu
description: Search the European Commission document register for policy documents
runnable: true
inputs:
  query:
    type: str
    required: true
    description: Topic keywords (e.g. "MiFID II review", "AI Act delegated acts")
  max_results:
    type: int
    required: false
    default: 5
    description: Maximum documents to return
output_schema:
  documents: str
  count: str
---

## Use case
Locate policy / consultation / regulatory documents on the European
Commission's portal. Useful for compliance research, market-structure
work, and regulatory tracking. The EC's own search is patchy across
subdomains (ec.europa.eu, eur-lex.europa.eu, finance.ec.europa.eu);
this skill composes a domain-restricted general search and extracts
top results with the standard pipeline.

## Recommended flow
1. Construct a `site:ec.europa.eu OR site:eur-lex.europa.eu OR site:finance.ec.europa.eu` query.
2. Run search_and_extract through the agent's standard search pipeline (SearXNG / DDGS / Playwright fallback).
3. Filter results to keep only ec.europa.eu / eur-lex.europa.eu / finance.ec.europa.eu hosts.
4. Return up to `max_results` extracted citations with titles, URLs, and snippets.

## Known selectors
- Document title on EUR-Lex: `h1.title-name`
- Document body on EUR-Lex: `div#text > p`
- Consultation summary on ec.europa.eu: `div.consultation-summary`
- "Documents" tab listing: `ul.document-list li a`

## Known traps
- ec.europa.eu and eur-lex.europa.eu disagree about cookies/consent banners; the cookies pop-up sometimes blocks page rendering -- the safety pipeline's `handle_dialog` accepts these automatically.
- Some documents are PDF-only -- enable binary extraction via `extract_files=True` to pull the body.
- The EU portal occasionally redirects to a generic "page not found" with a 200 status code -- watch the title.
- Multi-language documents: the URL contains a `/EN/` segment (or `/FR/`, etc.). Lock to English explicitly if needed.

## Output expectation
Returns a JSON-encoded list of `{title, url, snippet, body}` documents.
The list is encoded as a string (output_schema declares ``documents: str``
because Pydantic-style schemas don't currently express list types).
``count`` is the number of documents returned, as a string.
