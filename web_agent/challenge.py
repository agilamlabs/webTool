"""Bot-challenge / CAPTCHA interstitial detection (v1.7.0).

Pure, dependency-free structural detection of anti-bot walls: Cloudflare
("Just a moment..."), DataDome, Akamai, PerimeterX/HUMAN, and generic
CAPTCHA gates. Used by :class:`web_agent.web_fetcher.WebFetcher` so an
interstitial served with HTTP 200 no longer masquerades as a successful
fetch, and a 403/503 carrying a managed JS challenge gets a bounded
auto-settle chance before the fetch fast-fails.

Design rules:

- **Structural markers only.** Every marker is a vendor token, script
  URL, or challenge-page title fragment that real challenge pages embed
  (``cf-chl-``, ``/cdn-cgi/challenge-platform/``, ``px-captcha``, ...).
  A news article that merely *mentions* "Cloudflare" in prose must never
  trigger -- the bare vendor name is deliberately NOT a marker.
- **Status-weighted confidence.** 403/429/503 plus any marker scores
  high; HTTP 200 plus a strong marker scores high; HTTP 200 plus a
  single weak marker (e.g. an embedded Turnstile widget on a normal
  page) scores medium and stays below the action threshold.
- **Fast.** The first ~200 KB of HTML is lowercased once; matching is
  plain substring checks plus a few precompiled regexes.

This module stays import-light on purpose (stdlib + ``models`` only) so
the detector can be unit-tested and reused without pulling in Playwright.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import NamedTuple, Optional

from .models import ChallengeInfo, ChallengeKind, ChallengeVendor

# Confidence at or above which the fetcher ACTS on a detection (returns
# BLOCKED / runs the settle loop). Detections below the threshold are
# advisory only -- attached to a SUCCESS FetchResult, never blocking.
# 0.7 sits between "medium" (single weak marker on a 200 page, 0.5-0.6)
# and the lowest actionable score (0.75, weak markers on a
# challenge-shaped page). Public-stable so callers can apply the same
# policy to their own ``detect_challenge`` results.
CHALLENGE_CONFIDENCE_ACTION_THRESHOLD: float = 0.7

# Only the first 200 KB of HTML is scanned. Challenge interstitials are
# small (10-60 KB); the cap bounds regex work on pathological pages.
_SCAN_LIMIT_CHARS = 200 * 1024

_MAX_EVIDENCE = 5

# HTTP statuses bot-mitigation vendors serve challenge / block pages on.
_DENIAL_STATUS_CODES: frozenset[int] = frozenset({403, 429, 503})

# ---------------------------------------------------------------------------
# Marker tables (all lowercase -- matched against lowercased HTML / URL)
# ---------------------------------------------------------------------------

# Cloudflare managed/JS challenge plumbing. Any of these means the page IS
# challenge machinery, not content that talks about it.
_CLOUDFLARE_STRONG_MARKERS: tuple[str, ...] = (
    "cf-chl-",
    "/cdn-cgi/challenge-platform/",
    "window._cf_chl_opt",
    "<title>just a moment",
)
# Turnstile widget plumbing: present on challenge interstitials but ALSO on
# normal pages that embed the widget (login / signup forms) -- weak alone.
_CLOUDFLARE_WEAK_MARKERS: tuple[str, ...] = (
    "cf-turnstile",
    "challenges.cloudflare.com",
)
# Cloudflare block pages (e.g. error 1020) -- the phrase alone could occur
# in prose, so it only counts when a structural support marker is present.
_CLOUDFLARE_BLOCK_PHRASES: tuple[str, ...] = (
    "you have been blocked",
    "<title>attention required",
)
_CLOUDFLARE_BLOCK_SUPPORT: tuple[str, ...] = (
    "cf-error-details",
    "cf-wrapper",
    "cloudflare ray id",
    "cf-ray",
)
_CF_MITIGATED_EVIDENCE = "header:cf-mitigated=challenge"

# Vendors whose markers are all high-precision (never prose): matching any
# of them identifies both the vendor and the challenge kind.
_SIMPLE_VENDOR_MARKERS: tuple[tuple[ChallengeVendor, ChallengeKind, tuple[str, ...]], ...] = (
    ("datadome", "captcha", ("geo.captcha-delivery.com", "captcha-delivery.com", "ddjskey")),
    ("akamai", "js_challenge", ("/_sec/cp_challenge/", "ak-challenge", "sec-cpt-")),
    ("perimeterx", "captcha", ("px-captcha", "_pxappid", "captcha.px-cdn.net")),
)

# Generic CAPTCHA provider scripts. Ubiquitous on legitimate forms, so a
# match only counts when the page is challenge-shaped or the HTTP status
# is itself a denial (403/429/503).
_GENERIC_CAPTCHA_SCRIPT_MARKERS: tuple[str, ...] = (
    "hcaptcha.com/1/api.js",
    "google.com/recaptcha/api.js",
    "recaptcha.net/recaptcha/api.js",
    "google.com/recaptcha/enterprise.js",
)

# Access-denial title fragments shared across vendors' interstitials.
_DENIAL_TITLE_PATTERNS: tuple[str, ...] = (
    "access denied",
    "attention required",
    "verifying you are human",
    "bot detection",
    "just a moment",
    "security check",
    "are you a robot",
    "pardon our interruption",
    "access to this page has been denied",
)

# Challenge-shape heuristics: interstitials carry almost no visible prose.
_TINY_VISIBLE_TEXT_CHARS = 800
_RATIO_MIN_HTML_CHARS = 20_000
_TINY_TEXT_RATIO = 0.02

_TITLE_RE = re.compile(r"<title[^>]*>\s*([^<]{0,300})")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class _Candidate(NamedTuple):
    """One vendor's detection candidate, pre-scored."""

    vendor: ChallengeVendor
    kind: ChallengeKind
    confidence: float
    evidence: list[str]
    strong_hits: int


def _normalize_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Lowercase keys AND values for case-insensitive marker checks."""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for key, value in headers.items():
        try:
            out[str(key).strip().lower()] = str(value).strip().lower()
        except Exception:  # pragma: no cover -- defensive against odd mocks
            continue
    return out


def _extract_title(lowered: str) -> str:
    """First ~300 chars of the <title> element text, already lowercased."""
    match = _TITLE_RE.search(lowered)
    return match.group(1).strip() if match else ""


def _matched_denial_title(title: str) -> Optional[str]:
    """Return the first access-denial pattern found in the page title."""
    for pattern in _DENIAL_TITLE_PATTERNS:
        if pattern in title:
            return pattern
    return None


def _visible_text_estimate(lowered: str) -> str:
    """Rough visible-text extraction: drop script/style blocks, then tags."""
    without_blocks = _SCRIPT_STYLE_RE.sub(" ", lowered)
    text = _TAG_RE.sub(" ", without_blocks)
    return _WS_RE.sub(" ", text).strip()


def _looks_challenge_shaped(lowered: str, title: str) -> bool:
    """True when the page has the shape of an interstitial, not content.

    Either the title matches an access-denial pattern, or the visible
    text is tiny -- absolutely (< ~800 chars) or relative to the HTML
    size (< 2% of a 20 KB+ document). Challenge pages are mostly markup
    and challenge JS with a sentence or two of visible prose; real
    content pages carry orders of magnitude more text.
    """
    if _matched_denial_title(title) is not None:
        return True
    if not lowered:
        return False
    text = _visible_text_estimate(lowered)
    if len(text) < _TINY_VISIBLE_TEXT_CHARS:
        return True
    return len(lowered) > _RATIO_MIN_HTML_CHARS and (len(text) / len(lowered)) < _TINY_TEXT_RATIO


def _score(strong_hits: int, weak_hits: int, denial_status: bool, challenge_shaped: bool) -> float:
    """Status-weighted confidence for a marker match set.

    - any strong marker: 0.85 base, +0.10 on a denial status (403/429/
      503), +0.03 with two or more markers -- so "403 + strong" lands at
      the documented 0.95.
    - weak markers only: 0.85 on a denial status ("denial + any marker =
      high"); 0.75 on a challenge-shaped 200 page; 0.5-0.6 on a normal
      200 page (below the action threshold -- an embedded widget must
      never block a fetch).
    """
    total = strong_hits + weak_hits
    if total == 0:
        return 0.0
    if strong_hits > 0:
        confidence = 0.85 + (0.10 if denial_status else 0.0) + (0.03 if total >= 2 else 0.0)
    elif denial_status:
        confidence = 0.85 + (0.05 if total >= 2 else 0.0)
    elif challenge_shaped:
        confidence = 0.75 + (0.05 if total >= 2 else 0.0)
    else:
        confidence = 0.5 + (0.1 if total >= 2 else 0.0)
    return round(min(confidence, 0.98), 2)


def _cloudflare_candidate(
    lowered: str,
    lowered_url: str,
    cf_mitigated: bool,
    denial_status: bool,
    challenge_shaped: bool,
) -> Optional[_Candidate]:
    """Cloudflare: js_challenge / captcha (Turnstile widget) / block_page."""
    strong = [m for m in _CLOUDFLARE_STRONG_MARKERS if m in lowered or m in lowered_url]
    if cf_mitigated:
        strong.append(_CF_MITIGATED_EVIDENCE)
    weak = [m for m in _CLOUDFLARE_WEAK_MARKERS if m in lowered or m in lowered_url]
    block_phrases = [m for m in _CLOUDFLARE_BLOCK_PHRASES if m in lowered]
    block_support = [m for m in _CLOUDFLARE_BLOCK_SUPPORT if m in lowered]
    is_block_page = bool(block_phrases) and bool(block_support) and not strong

    kind: ChallengeKind
    if strong:
        # Challenge-platform plumbing present: a managed / JS challenge
        # (an embedded Turnstile widget on the same page is part of it).
        kind = "js_challenge"
        evidence = strong + weak
        strong_hits, weak_hits = len(strong), len(weak)
    elif is_block_page:
        # "Sorry, you have been blocked" / "Attention Required" with
        # structural Cloudflare support markers: a hard block page.
        kind = "block_page"
        evidence = block_phrases + block_support
        strong_hits, weak_hits = len(block_phrases), len(block_support)
    elif weak:
        # Turnstile plumbing alone: an interactive CAPTCHA-style gate
        # when challenge-shaped; a harmless embedded widget otherwise
        # (the score stays below the action threshold in that case).
        kind = "captcha"
        evidence = weak
        strong_hits, weak_hits = 0, len(weak)
    else:
        return None
    confidence = _score(strong_hits, weak_hits, denial_status, challenge_shaped)
    return _Candidate("cloudflare", kind, confidence, evidence[:_MAX_EVIDENCE], strong_hits)


def _marker_candidate(
    vendor: ChallengeVendor,
    kind: ChallengeKind,
    markers: tuple[str, ...],
    lowered: str,
    lowered_url: str,
    denial_status: bool,
    challenge_shaped: bool,
) -> Optional[_Candidate]:
    """Candidate for a vendor whose markers are all strong/structural."""
    matched = [m for m in markers if m in lowered or m in lowered_url]
    if not matched:
        return None
    # Drop a marker that is a substring of another matched marker so e.g.
    # 'geo.captcha-delivery.com' doesn't double-count 'captcha-delivery.com'.
    deduped = [m for m in matched if not any(m != other and m in other for other in matched)]
    confidence = _score(len(deduped), 0, denial_status, challenge_shaped)
    return _Candidate(vendor, kind, confidence, deduped[:_MAX_EVIDENCE], len(deduped))


def _generic_captcha_candidate(
    lowered: str,
    title: str,
    denial_status: bool,
    challenge_shaped: bool,
) -> Optional[_Candidate]:
    """hCaptcha / reCAPTCHA script present AND the page looks like a gate.

    The script alone is NOT enough -- countless legitimate login, signup,
    and contact pages embed these, and many of them are SHORT (a form is
    not much prose). So on an HTTP-200 response the page must carry an
    access-denial ``<title>`` ("Just a moment" / "Access denied" / ...) --
    a soft-200 interstitial. "Short visible text" alone does NOT qualify a
    200 page: a plain login form (title "Sign in") stays SUCCESS. This
    fixes the v1.7.0 false-positive that hard-BLOCKED short login/signup
    pages merely for embedding reCAPTCHA.

    On a denial status (403/429/503) the status itself is the block
    signal, so a captcha script alone scores high -- a forbidden response
    serving a CAPTCHA is a wall regardless of page shape.
    """
    matched = [m for m in _GENERIC_CAPTCHA_SCRIPT_MARKERS if m in lowered]
    if not matched:
        return None
    denial_title = _matched_denial_title(title)
    if denial_status:
        confidence = 0.9
    elif denial_title is not None:
        # Soft-200 interstitial: some CDNs serve challenges as HTTP 200
        # with a "just a moment" / "access denied" title.
        confidence = 0.75
    else:
        # CAPTCHA widget on an ordinary HTTP-200 page (login / signup /
        # contact) -- not a wall. Leave it as SUCCESS.
        return None
    evidence = list(matched)
    if denial_title is not None:
        evidence.append(f"denial_title:{denial_title}")
    elif challenge_shaped:
        evidence.append("challenge_shaped_page")
    return _Candidate(
        "generic_captcha", "captcha", confidence, evidence[:_MAX_EVIDENCE], len(matched)
    )


def detect_challenge(
    html: str,
    status_code: int | None,
    headers: Mapping[str, str] | None = None,
    final_url: str | None = None,
) -> ChallengeInfo | None:
    """Detect a bot-challenge / CAPTCHA interstitial in a fetched page.

    Args:
        html: Raw page HTML (may be empty -- header-only detection still
            works for e.g. Cloudflare's ``cf-mitigated: challenge``).
            Only the first ~200 KB is scanned.
        status_code: HTTP status of the response, or None when unknown
            (e.g. re-checks after a settle wait, where the displayed DOM
            is the only signal). 403/429/503 raise marker confidence.
        headers: Optional response headers (case-insensitive).
        final_url: Optional post-redirect URL; vendor challenge endpoints
            appearing in it count as markers.

    Returns:
        A :class:`ChallengeInfo` when structural challenge markers are
        found (``confidence`` conveys how sure; callers should act only
        at or above :data:`CHALLENGE_CONFIDENCE_ACTION_THRESHOLD`), or
        ``None`` for ordinary pages -- including pages that merely
        mention vendors in prose.
    """
    lowered = (html or "")[:_SCAN_LIMIT_CHARS].lower()
    lowered_url = final_url.lower() if isinstance(final_url, str) else ""
    normalized_headers = _normalize_headers(headers)
    if not lowered and not normalized_headers:
        return None

    denial_status = status_code in _DENIAL_STATUS_CODES
    title = _extract_title(lowered)
    challenge_shaped = _looks_challenge_shaped(lowered, title)
    cf_mitigated = normalized_headers.get("cf-mitigated", "") == "challenge"

    candidates: list[_Candidate] = []
    cloudflare = _cloudflare_candidate(
        lowered, lowered_url, cf_mitigated, denial_status, challenge_shaped
    )
    if cloudflare is not None:
        candidates.append(cloudflare)
    for vendor, kind, markers in _SIMPLE_VENDOR_MARKERS:
        candidate = _marker_candidate(
            vendor, kind, markers, lowered, lowered_url, denial_status, challenge_shaped
        )
        if candidate is not None:
            candidates.append(candidate)
    generic = _generic_captcha_candidate(lowered, title, denial_status, challenge_shaped)
    if generic is not None:
        candidates.append(generic)

    if not candidates:
        # No vendor fingerprint, but a denial status serving an
        # access-denial interstitial is still worth reporting -- as a
        # LOW-confidence advisory (never actionable on its own).
        denial_title = _matched_denial_title(title)
        if denial_status and challenge_shaped and denial_title is not None:
            return ChallengeInfo(
                vendor="unknown",
                kind="block_page",
                confidence=0.55,
                evidence=[f"denial_title:{denial_title}", f"status:{status_code}"],
                auto_settle_likely=False,
            )
        return None

    # Highest confidence wins; ties break toward more strong markers,
    # then more evidence, then table order (max() keeps the first).
    best = max(candidates, key=lambda c: (c.confidence, c.strong_hits, len(c.evidence)))
    if best.confidence <= 0.0:
        return None

    resolved_kind: ChallengeKind = best.kind
    if status_code == 429:
        # A 429 carrying vendor challenge markers is rate limiting in
        # challenge clothing -- waiting a settle interval won't help.
        resolved_kind = "rate_limit"
    auto_settle_likely = best.vendor == "cloudflare" and resolved_kind == "js_challenge"
    return ChallengeInfo(
        vendor=best.vendor,
        kind=resolved_kind,
        confidence=best.confidence,
        evidence=best.evidence[:_MAX_EVIDENCE],
        auto_settle_likely=auto_settle_likely,
    )
