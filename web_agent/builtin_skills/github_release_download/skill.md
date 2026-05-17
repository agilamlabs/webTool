---
name: release_download
domain: github.com
description: Find and download a release asset from a GitHub repository
runnable: true
inputs:
  repo:
    type: str
    required: true
    description: "Repository slug like 'owner/name'"
  asset_pattern:
    type: str
    required: false
    default: ""
    description: Substring the asset filename must contain (e.g. "linux-x86_64")
  tag:
    type: str
    required: false
    default: ""
    description: Specific release tag (empty = latest)
output_schema:
  release_tag: str
  asset_url: str
  asset_name: str
  downloaded_path: str
---

## Use case
Locate a release asset on GitHub (binary, source tarball, container
manifest) and download it via the standard webTool downloader pipeline.
The downloader's 3-strategy fallback handles GitHub's redirects to S3
gracefully, including the SSRF re-check on the final asset URL.

## Recommended flow
1. Construct the canonical release URL: `https://github.com/<repo>/releases/latest` (or `/releases/tag/<tag>` when a specific tag is requested).
2. Fetch the release page to locate the assets list.
3. Match the requested `asset_pattern` against asset filenames (case-insensitive substring).
4. Resolve the final `releases/download/<tag>/<asset>` URL (the page lists these directly).
5. Hand the URL to `agent.download(...)` which handles the S3 redirect + size cap + post-redirect safety re-check.

## Known selectors
- Release header: `h1.d-inline.mr-3`
- Assets list: `details.release-assets`
- Asset link: `a[href*="/releases/download/"]`

## Known traps
- The "Source code" tarball/zipball links are auto-generated and DO NOT live under `/releases/download/` -- they're under `/archive/refs/tags/`. Filter by the canonical download path if you want only uploaded assets.
- Releases tagged with leading `v` need that prefix preserved in URLs.
- Some repos use the GitHub API `releases/latest` JSON endpoint, which is much faster than scraping HTML -- consider that for high-frequency calls (subject to API rate limits).

## Output expectation
Returns `{release_tag, asset_url, asset_name, downloaded_path}` after a successful download. ``downloaded_path`` is the absolute path under ``DownloadConfig.download_dir``.
