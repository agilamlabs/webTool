"""Pure (no-network) parsing of XML sitemaps and sitemap indexes.

A sitemap is an XML document listing a site's crawlable URLs (the
``urlset`` form), or a list of OTHER sitemaps (the ``sitemapindex`` form).
This module turns the raw XML *text* into a small :class:`SitemapParse`
record used to SEED a same-site crawl (see :class:`web_agent.crawl.SiteCrawler`).

SECURITY: extraction is deliberately REGEX-based, NOT XML-parser-based.
Sitemaps are attacker-influenceable input (any site can serve one), and a
real XML parser is vulnerable to entity-expansion / "billion laughs" DoS
and external-entity (XXE) attacks on hostile input. A regex over ``<loc>``
text cannot expand entities, cannot be made to read local files, and never
raises on malformed / truncated markup -- it simply recovers what it can.
The cost is that we do not validate the XML; that is an acceptable trade
for a best-effort seed source whose output is independently re-scoped and
re-gated by the crawler.

Network fetching of these paths is the crawler's job; this module is pure
so it stays trivially unit-testable offline.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from loguru import logger

# Common conventional locations a sitemap is served from, relative to the
# site origin. The crawler tries ``/sitemap.xml`` first (by far the most
# common); ``/sitemap_index.xml`` is the typical name for an index that
# fans out to child sitemaps.
SITEMAP_PATHS: tuple[str, ...] = ("/sitemap.xml", "/sitemap_index.xml")

# Text of every ``<loc>...</loc>`` element, case-insensitively, tolerant of
# attributes/whitespace on the opening tag and spanning newlines. ``.*?`` is
# lazy so each element is captured individually. We never feed this to an XML
# parser, so entity-expansion / XXE classes of attack do not apply.
_LOC_RE = re.compile(r"<loc\b[^>]*>(.*?)</loc\s*>", re.IGNORECASE | re.DOTALL)

# An index is identified purely by the presence of a ``<sitemapindex`` opening
# tag anywhere in the document (case-insensitive); otherwise it is a urlset.
_SITEMAPINDEX_RE = re.compile(r"<sitemapindex\b", re.IGNORECASE)


@dataclass(frozen=True)
class SitemapParse:
    """Result of parsing one sitemap document.

    Attributes:
        urls: The ``<loc>`` URLs found, whitespace-stripped, HTML-unescaped
            (e.g. ``&amp;`` -> ``&``), with empties dropped, in document
            order, bounded to ``max_urls``. For a ``urlset`` these are page
            URLs to crawl; for a ``sitemapindex`` they are URLs of CHILD
            sitemaps to fetch and parse in turn.
        is_index: True when the document is a ``<sitemapindex>`` (its
            ``urls`` point to other sitemaps), False for a ``<urlset>``.
    """

    urls: list[str] = field(default_factory=list)
    is_index: bool = False


def parse_sitemap(xml_text: str, *, max_urls: int = 1000) -> SitemapParse:
    """Parse sitemap XML *text* into a :class:`SitemapParse` (no network, never raises).

    Detects a sitemap INDEX by the presence of a ``<sitemapindex`` tag
    (case-insensitive); anything else is treated as a ``urlset``. Every
    ``<loc>`` element's text is extracted via regex, stripped,
    ``html.unescape``-d, and kept if non-empty -- stopping once ``max_urls``
    URLs have been collected so a hostile multi-million-entry sitemap cannot
    blow up memory. Malformed or truncated XML does not raise: the regex
    simply recovers whatever well-formed ``<loc>`` elements it can find.

    Args:
        xml_text: Raw sitemap XML as text. May be empty, malformed, or
            truncated.
        max_urls: Upper bound on returned URLs (collection stops early once
            reached). Values <= 0 yield an empty URL list while still
            reporting ``is_index`` accurately.

    Returns:
        A :class:`SitemapParse` with the (bounded) URL list and the
        index/urlset flag.
    """
    if not xml_text:
        return SitemapParse(urls=[], is_index=False)

    is_index = _SITEMAPINDEX_RE.search(xml_text) is not None

    urls: list[str] = []
    if max_urls > 0:
        for match in _LOC_RE.finditer(xml_text):
            loc = html.unescape(match.group(1).strip())
            if not loc:
                continue
            urls.append(loc)
            if len(urls) >= max_urls:
                break

    logger.debug(
        "parse_sitemap: parsed {n} url(s), is_index={idx}",
        n=len(urls),
        idx=is_index,
    )
    return SitemapParse(urls=urls, is_index=is_index)
