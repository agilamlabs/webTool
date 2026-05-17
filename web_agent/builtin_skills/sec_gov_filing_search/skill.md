---
name: filing_search
domain: sec.gov
description: Find and extract a company's most recent SEC filing of a given form type
runnable: true
inputs:
  company:
    type: str
    required: true
    description: Company name or CIK number
  form_type:
    type: str
    required: false
    default: "10-K"
    description: SEC form type (10-K, 10-Q, 8-K, S-1, etc.)
output_schema:
  filing_url: str
  form_type: str
  filing_date: str
  accession_number: str
  extracted_text: str
---

## Use case
Find and extract the most recent SEC filing of a given form type for a
public company. Common downstream uses: pulling 10-K disclosures,
extracting Risk Factors sections, parsing earnings releases (8-K).

## Recommended flow
1. Search EDGAR full-text search for the company name to locate the CIK.
2. Open the company's submissions page (`/cgi-bin/browse-edgar?action=getcompany&CIK=...`).
3. Filter the filings table by the requested form_type.
4. Prefer inline HTML (`/Archives/edgar/data/.../index.html`) over PDF for downstream extraction.
5. Extract the filing URL, form type, accession number, filing date, and the body text via the standard trafilatura pipeline.

## Known selectors
- Company search input: `input#company`
- Filing index table: `table.tableFile2`
- Form-type filter dropdown: `select[name=type]`
- Filing document link: `a[href*="/Archives/edgar/data/"]`

## Known traps
- Some hits redirect to archive-only accession-number pages -- check the `Location` header after a 302.
- Multi-document filings have an `index.htm` page listing exhibits; the primary 10-K body is usually the first document, not exhibits 99.x.
- Avoid raw `.txt` filings unless explicitly requested -- they require iXBRL-aware extraction.
- The "interactive data" links lead to XBRL viewers, not text.

## Output expectation
Returns one record per call shaped like ``{filing_url, form_type, filing_date, accession_number, extracted_text}`` where ``extracted_text`` is the trafilatura-extracted body of the primary filing document.
