"""v1.7.0 Wave 6: schema-guided structured extraction.

Covers the deterministic, LLM-free field resolver
(:func:`web_agent.structured.resolve_fields`) and the
:meth:`Recipes.extract_fields` recipe, fully offline (handcrafted HTML +
AsyncMock fetcher/extractor -- no Playwright launch, no network):

1. resolve_fields unit: JSON-LD product (name/price/sku), OpenGraph article
   (title/author/date), labelled DOM (<dl>/<table>); per-field source tagging;
   unresolved listing; source PRIORITY (json-ld beats dom).
2. Fuzzy matching: "price" -> "offers.price"; "product name" -> schema.org
   "name"; alias hits.
3. Bounds: > max_fields capped; a giant value truncated.
4. Recipe extract_fields: happy path; transparent failed-fetch result; strict
   raises; the llm_extractor hook (sync AND async) fills unresolved fields
   tagged 'llm'; a raising hook does not crash.
5. StructuredExtractionResult schema round-trip + backward-compat defaults.
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.config import AppConfig
from web_agent.exceptions import NavigationError
from web_agent.models import (
    ExtractionResult,
    FetchResult,
    FetchStatus,
    StructuredExtractionResult,
)
from web_agent.recipes import Recipes
from web_agent.structured import resolve_fields

# ----------------------------------------------------------------------
# Shared fixtures / builders
# ----------------------------------------------------------------------

_PRODUCT_JSON_LD: list[dict[str, Any]] = [
    {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Acme Mega Widget",
        "sku": "AMW-9000",
        "description": "The best widget money can buy.",
        "brand": {"@type": "Brand", "name": "Acme"},
        "image": {"@type": "ImageObject", "url": "https://cdn.example/w.png"},
        "offers": {
            "@type": "Offer",
            "price": "1,299.00",
            "priceCurrency": "USD",
        },
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": 4.5, "reviewCount": 87},
    }
]

_ARTICLE_HTML = """
<html><head>
<meta property="og:title" content="Markets Rally on News"/>
<meta property="og:description" content="Stocks climbed today."/>
<meta property="og:site_name" content="Example News"/>
<meta property="article:published_time" content="2026-01-15T10:00:00Z"/>
<meta property="article:author" content="Jane Reporter"/>
<meta name="keywords" content="markets, stocks, rally"/>
<meta name="description" content="Meta-level description."/>
</head><body><article>Body text here.</article></body></html>
"""

_DOM_HTML = """
<html><body>
<h1>Spec Sheet</h1>
<dl>
  <dt>Color</dt><dd>Midnight Blue</dd>
  <dt>Weight</dt><dd>2.4 kg</dd>
</dl>
<table>
  <tr><th>Material</th><td>Anodized Aluminium</td></tr>
  <tr><th>Warranty</th><td>2 years</td></tr>
</table>
<form>
  <label for="qty">Quantity</label>
  <input id="qty" value="12"/>
</form>
</body></html>
"""


def _offline_config(**safety: object) -> AppConfig:
    """AppConfig with the private-IP guard OFF so fake hosts don't pay a real
    ~1s getaddrinfo timeout. ``fetch`` is mocked, so the SSRF re-gate inside
    fetch never runs here.
    """
    merged = {"block_private_ips": False, **safety}
    return AppConfig(safety=merged)


def _fetch_result(
    url: str, html: str = "<html></html>", *, status: FetchStatus = FetchStatus.SUCCESS
) -> FetchResult:
    return FetchResult(url=url, final_url=url, status=status, html=html)


def _extraction(
    url: str,
    content: str = "cleaned content",
    *,
    structured_data: Optional[list[dict[str, Any]]] = None,
) -> ExtractionResult:
    return ExtractionResult(
        url=url,
        content=content,
        content_length=len(content),
        extraction_method="raw" if content else "none",
        structured_data=structured_data or [],
    )


def _make_recipes(
    *,
    fetch_side: object = None,
    extract_side: object = None,
    config: Optional[AppConfig] = None,
) -> Recipes:
    """Build a Recipes whose fetcher.fetch + extractor.extract_async are mocks."""
    cfg = config or _offline_config()
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(side_effect=fetch_side)
    extractor = MagicMock()
    extractor.extract_async = AsyncMock(side_effect=extract_side)
    return Recipes(
        search=MagicMock(),
        fetcher=fetcher,
        extractor=extractor,
        downloader=MagicMock(),
        config=cfg,
        browser_manager=MagicMock(),
        sessions=None,
        actions=None,
    )


# ======================================================================
# resolve_fields -- JSON-LD source
# ======================================================================


def test_resolve_json_ld_product_fields() -> None:
    """A product page's JSON-LD resolves name / price / sku / brand / rating /
    currency, each tagged 'json-ld'. Price is numeric-coerced (thousands sep).
    """
    fields, sources, unresolved = resolve_fields(
        ["product name", "price", "sku", "brand", "rating", "currency", "image"],
        json_ld=_PRODUCT_JSON_LD,
        html="<html></html>",
    )
    assert fields["product name"] == "Acme Mega Widget"
    assert fields["price"] == 1299.0  # "1,299.00" -> float
    assert fields["sku"] == "AMW-9000"
    assert fields["brand"] == "Acme"  # nested brand.name collapsed
    assert fields["rating"] == 4.5  # nested aggregateRating.ratingValue
    assert fields["currency"] == "USD"
    assert fields["image"] == "https://cdn.example/w.png"  # nested image.url
    assert all(sources[f] == "json-ld" for f in fields)
    assert unresolved == []


def test_resolve_unresolved_listed() -> None:
    """A field no source carries is reported in ``unresolved`` (not dropped)."""
    fields, sources, unresolved = resolve_fields(
        ["name", "nonexistent_field"],
        json_ld=_PRODUCT_JSON_LD,
        html="<html></html>",
    )
    assert "name" in fields
    assert unresolved == ["nonexistent_field"]
    assert "nonexistent_field" not in sources


def test_resolve_price_matches_nested_offers_price() -> None:
    """Fuzzy: bare 'price' matches the nested 'offers.price' key."""
    fields, sources, _ = resolve_fields(["price"], json_ld=_PRODUCT_JSON_LD, html="")
    assert fields["price"] == 1299.0
    assert sources["price"] == "json-ld"


def test_resolve_alias_cost_matches_price() -> None:
    """Alias table: 'cost' matches a 'price' signal key."""
    fields, _, _ = resolve_fields(["cost"], json_ld=[{"@type": "Product", "price": "9.99"}], html="")
    assert fields["cost"] == 9.99


def test_resolve_product_name_matches_schema_name() -> None:
    """Alias: 'product name' resolves schema.org 'name'."""
    fields, _, _ = resolve_fields(
        {"product name": "the title of the product"},
        json_ld=[{"@type": "Product", "name": "Gizmo"}],
        html="",
    )
    assert fields["product name"] == "Gizmo"


def test_resolve_schema_as_dict_uses_hint_words() -> None:
    """A dict schema's hint words help match a sparsely-named field."""
    # field 'amt' alone wouldn't token-overlap 'price'; the hint 'price' does
    # via the alias group (amount <-> price).
    fields, _, _ = resolve_fields(
        {"amount": "the total price"},
        json_ld=[{"@type": "Product", "price": "42"}],
        html="",
    )
    assert fields["amount"] == 42


# ======================================================================
# resolve_fields -- OpenGraph / meta sources
# ======================================================================


def test_resolve_opengraph_article() -> None:
    """An OpenGraph article resolves title/author/date/summary from og:* and
    keywords from <meta name>.
    """
    fields, sources, _ = resolve_fields(
        {
            "title": "headline",
            "author": "who wrote it",
            "date": "publication date",
            "summary": "short description",
            "keywords": "",
        },
        html=_ARTICLE_HTML,
    )
    assert fields["title"] == "Markets Rally on News"
    assert sources["title"] == "opengraph"
    assert fields["author"] == "Jane Reporter"
    assert fields["date"] == "2026-01-15T10:00:00Z"
    assert fields["summary"] == "Stocks climbed today."  # og:description
    assert fields["keywords"] == "markets, stocks, rally"
    assert sources["keywords"] == "meta"


# ======================================================================
# resolve_fields -- microdata + DOM sources
# ======================================================================


def test_resolve_microdata_itemprop() -> None:
    """[itemprop] elements resolve via content attr or text."""
    html = (
        '<div itemscope itemtype="https://schema.org/Product">'
        '<span itemprop="name">Microdata Widget</span>'
        '<meta itemprop="sku" content="MD-1"/>'
        "</div>"
    )
    fields, sources, _ = resolve_fields(["name", "sku"], html=html)
    assert fields["name"] == "Microdata Widget"
    assert fields["sku"] == "MD-1"
    assert sources["name"] == "microdata"


def test_resolve_dom_dl_and_table() -> None:
    """A <dl>/<dd> + <th>/<td> page resolves labelled fields from the DOM,
    including a <label>+input value.
    """
    fields, sources, unresolved = resolve_fields(
        ["color", "weight", "material", "warranty", "quantity"],
        html=_DOM_HTML,
    )
    assert fields["color"] == "Midnight Blue"
    assert fields["weight"] == "2.4 kg"
    assert fields["material"] == "Anodized Aluminium"
    assert fields["warranty"] == "2 years"
    assert fields["quantity"] == 12  # <label for=qty> + <input value="12"> -> coerced
    assert all(sources[f] == "dom" for f in ("color", "weight", "material", "warranty"))
    assert unresolved == []


# ======================================================================
# resolve_fields -- source PRIORITY (json-ld beats dom)
# ======================================================================


def test_resolve_priority_json_ld_beats_dom() -> None:
    """When both JSON-LD and the DOM carry a field, JSON-LD wins."""
    html = "<html><body><dl><dt>Name</dt><dd>FromDOM</dd></dl></body></html>"
    fields, sources, _ = resolve_fields(
        ["name"],
        json_ld=[{"@type": "Product", "name": "FromJSONLD"}],
        html=html,
    )
    assert fields["name"] == "FromJSONLD"
    assert sources["name"] == "json-ld"


def test_resolve_priority_opengraph_beats_dom() -> None:
    """OpenGraph (higher priority) wins over a same-named DOM label."""
    html = (
        '<html><head><meta property="og:title" content="OG Title"/></head>'
        "<body><dl><dt>Title</dt><dd>DOM Title</dd></dl></body></html>"
    )
    fields, sources, _ = resolve_fields(["title"], html=html)
    assert fields["title"] == "OG Title"
    assert sources["title"] == "opengraph"


# ======================================================================
# resolve_fields -- bounds
# ======================================================================


def test_resolve_max_fields_caps_resolution() -> None:
    """More resolvable fields than max_fields: the excess is reported unresolved."""
    fields, _, unresolved = resolve_fields(
        ["name", "sku", "description"],
        json_ld=_PRODUCT_JSON_LD,
        html="",
        max_fields=1,
    )
    assert len(fields) == 1
    assert len(unresolved) == 2


def test_resolve_value_truncated_to_cap() -> None:
    """A giant value is truncated to max_value_chars."""
    fields, _, _ = resolve_fields(
        ["description"],
        json_ld=[{"description": "Z" * 9000}],
        html="",
        max_value_chars=100,
    )
    assert fields["description"] == "Z" * 100


def test_resolve_dom_pairs_capped() -> None:
    """max_dom_pairs bounds the labelled-DOM scan: a field beyond the cap is
    not resolved from the DOM.
    """
    rows = "".join(f"<dt>K{i}</dt><dd>V{i}</dd>" for i in range(50))
    html = f"<html><body><dl>{rows}</dl></body></html>"
    # Scan only the first pair; K0 resolves, K40 does not.
    fields, _, unresolved = resolve_fields(["k0", "k40"], html=html, max_dom_pairs=1)
    assert fields.get("k0") == "V0"
    assert "k40" in unresolved


# ======================================================================
# resolve_fields -- robustness
# ======================================================================


def test_resolve_never_raises_on_malformed_html() -> None:
    """Malformed / junk HTML returns whatever resolved, never raises."""
    fields, _, unresolved = resolve_fields(
        ["name"],
        json_ld=[{"name": "Survives"}],
        html="<html><body><dl><dt>unclosed",
    )
    assert fields["name"] == "Survives"
    assert unresolved == []


def test_resolve_schema_list_and_dict_equivalent() -> None:
    """A list schema and a dict schema with empty hints resolve identically."""
    f_list, _, _ = resolve_fields(["name", "sku"], json_ld=_PRODUCT_JSON_LD, html="")
    f_dict, _, _ = resolve_fields(
        {"name": "", "sku": ""}, json_ld=_PRODUCT_JSON_LD, html=""
    )
    assert f_list == f_dict


# ======================================================================
# Recipe extract_fields -- happy path
# ======================================================================


@pytest.mark.asyncio
async def test_extract_fields_happy_path() -> None:
    """A mocked fetch+extract resolves fields from the page's JSON-LD."""

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, "<html><body>product</body></html>")

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, "product page", structured_data=_PRODUCT_JSON_LD)

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.extract_fields(
        "https://shop.example/widget", ["product name", "price", "sku", "missing"]
    )

    assert isinstance(result, StructuredExtractionResult)
    assert result.extraction_method == "structured-signals"
    assert result.fields["product name"] == "Acme Mega Widget"
    assert result.fields["price"] == 1299.0
    assert result.fields["sku"] == "AMW-9000"
    assert result.field_sources["price"] == "json-ld"
    assert result.unresolved == ["missing"]
    assert result.fetch_status is None
    assert result.error_message is None


@pytest.mark.asyncio
async def test_extract_fields_nothing_resolved_method_none() -> None:
    """A successful fetch where nothing resolves -> extraction_method='none'."""

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, "<html><body>nada</body></html>")

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, "nada", structured_data=[])

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.extract_fields("https://x.example/p", ["price"])
    assert result.fields == {}
    assert result.extraction_method == "none"
    assert result.unresolved == ["price"]


# ======================================================================
# Recipe extract_fields -- failure transparency + strict
# ======================================================================


@pytest.mark.asyncio
async def test_extract_fields_failed_fetch_is_transparent() -> None:
    """A non-success fetch returns a transparent error result (no crash):
    extraction_method='none' + fetch_status / status_code / error_message set.
    """

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return FetchResult(
            url=url,
            final_url=url,
            status=FetchStatus.HTTP_ERROR,
            status_code=403,
            html="",
        )

    recipes = _make_recipes(fetch_side=_fetch)
    result = await recipes.extract_fields("https://blocked.example/p", ["price"])
    assert result.extraction_method == "none"
    assert result.fetch_status == "http_error"
    assert result.status_code == 403
    assert result.error_message is not None and "403" in result.error_message
    assert result.fields == {}


@pytest.mark.asyncio
async def test_extract_fields_strict_raises_on_failure() -> None:
    """strict=True raises NavigationError on a failed fetch."""

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return FetchResult(
            url=url, final_url=url, status=FetchStatus.TIMEOUT, html=""
        )

    recipes = _make_recipes(fetch_side=_fetch)
    with pytest.raises(NavigationError):
        await recipes.extract_fields("https://slow.example/p", ["price"], strict=True)


@pytest.mark.asyncio
async def test_extract_fields_blocked_domain() -> None:
    """A denied domain returns a transparent blocked result (no fetch attempted)."""
    cfg = _offline_config(denied_domains=["evil.com"])
    recipes = _make_recipes(config=cfg)
    result = await recipes.extract_fields("https://evil.com/p", ["price"])
    assert result.extraction_method == "none"
    assert result.fetch_status == "blocked"
    assert result.error_message is not None


@pytest.mark.asyncio
async def test_extract_fields_blocked_domain_strict_raises() -> None:
    cfg = _offline_config(denied_domains=["evil.com"])
    recipes = _make_recipes(config=cfg)
    with pytest.raises(NavigationError):
        await recipes.extract_fields("https://evil.com/p", ["price"], strict=True)


# ======================================================================
# Recipe extract_fields -- llm_extractor hook (sync + async)
# ======================================================================


def _page_with_only_dom() -> tuple[object, object]:
    """fetch+extract returning a page with a DOM field but NO json-ld, so a
    prose field stays unresolved for the hook to fill.
    """

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, "<html><body><dl><dt>Name</dt><dd>Widget</dd></dl></body></html>")

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, "The sentiment of this article is very positive.")

    return _fetch, _extract


@pytest.mark.asyncio
async def test_extract_fields_sync_llm_hook_fills_unresolved() -> None:
    """A SYNC llm_extractor fills the unresolved subset; results merge as 'llm'."""
    fetch, extract = _page_with_only_dom()
    captured: dict[str, Any] = {}

    def _hook(subset: dict[str, str], content: str) -> dict[str, Any]:
        captured["subset"] = subset
        captured["content"] = content
        return {"sentiment": "positive"}

    recipes = _make_recipes(fetch_side=fetch, extract_side=extract)
    result = await recipes.extract_fields(
        "https://x.example/p",
        {"name": "the name", "sentiment": "overall tone of the article"},
        llm_extractor=_hook,
    )
    # Deterministic resolver got 'name' from the DOM; hook filled 'sentiment'.
    assert result.fields["name"] == "Widget"
    assert result.field_sources["name"] == "dom"
    assert result.fields["sentiment"] == "positive"
    assert result.field_sources["sentiment"] == "llm"
    assert result.unresolved == []
    # The hook was handed ONLY the unresolved subset + the cleaned content.
    assert set(captured["subset"]) == {"sentiment"}
    assert "positive" in captured["content"]
    assert result.extraction_method == "structured-signals"


@pytest.mark.asyncio
async def test_extract_fields_async_llm_hook_awaited() -> None:
    """An ASYNC (coroutine) llm_extractor is awaited and merged."""
    fetch, extract = _page_with_only_dom()

    async def _hook(subset: dict[str, str], content: str) -> dict[str, Any]:
        return {"sentiment": "neutral"}

    recipes = _make_recipes(fetch_side=fetch, extract_side=extract)
    result = await recipes.extract_fields(
        "https://x.example/p",
        ["name", "sentiment"],
        llm_extractor=_hook,
    )
    assert result.fields["sentiment"] == "neutral"
    assert result.field_sources["sentiment"] == "llm"


@pytest.mark.asyncio
async def test_extract_fields_raising_llm_hook_does_not_crash() -> None:
    """A raising llm_extractor leaves fields unresolved + does not crash."""
    fetch, extract = _page_with_only_dom()

    def _hook(subset: dict[str, str], content: str) -> dict[str, Any]:
        raise RuntimeError("model exploded")

    recipes = _make_recipes(fetch_side=fetch, extract_side=extract)
    result = await recipes.extract_fields(
        "https://x.example/p", ["name", "sentiment"], llm_extractor=_hook
    )
    # Deterministic 'name' survived; 'sentiment' stayed unresolved (hook failed).
    assert result.fields["name"] == "Widget"
    assert result.unresolved == ["sentiment"]
    assert "sentiment" not in result.field_sources


@pytest.mark.asyncio
async def test_extract_fields_llm_hook_not_called_when_all_resolved() -> None:
    """The hook is skipped entirely when the deterministic pass resolves all."""

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, "<html></html>")

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, "x", structured_data=_PRODUCT_JSON_LD)

    calls = {"n": 0}

    def _hook(subset: dict[str, str], content: str) -> dict[str, Any]:
        calls["n"] += 1
        return {}

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.extract_fields("https://x.example/p", ["name"], llm_extractor=_hook)
    assert result.fields["name"] == "Acme Mega Widget"
    assert calls["n"] == 0  # nothing unresolved -> hook never invoked


@pytest.mark.asyncio
async def test_extract_fields_bad_llm_hook_return_ignored() -> None:
    """A hook returning a non-dict is ignored (does not crash, no fields added)."""
    fetch, extract = _page_with_only_dom()

    def _hook(subset: dict[str, str], content: str) -> Any:
        return "not a dict"

    recipes = _make_recipes(fetch_side=fetch, extract_side=extract)
    result = await recipes.extract_fields(
        "https://x.example/p", ["name", "sentiment"], llm_extractor=_hook
    )
    assert result.unresolved == ["sentiment"]


# ======================================================================
# StructuredExtractionResult schema round-trip + defaults
# ======================================================================


def test_structured_result_schema_round_trips() -> None:
    r = StructuredExtractionResult(
        url="https://x/p",
        fields={"name": "Widget", "price": 9.99},
        field_sources={"name": "json-ld", "price": "opengraph"},
        unresolved=["color"],
        extraction_method="structured-signals",
        correlation_id="cid-1",
    )
    dumped = r.model_dump_json()
    restored = StructuredExtractionResult.model_validate_json(dumped)
    assert restored == r
    assert restored.fields["price"] == 9.99
    assert restored.field_sources["name"] == "json-ld"


def test_structured_result_backward_compat_defaults() -> None:
    """A minimal result (only url) fills sane additive defaults."""
    r = StructuredExtractionResult(url="https://x")
    assert r.fields == {}
    assert r.field_sources == {}
    assert r.unresolved == []
    assert r.extraction_method == "none"
    assert r.fetch_status is None
    assert r.status_code is None
    assert r.error_message is None
    assert r.correlation_id is None


# ----------------------------------------------------------------------
# Review fixes: conservative coercion + token-aware matching
# ----------------------------------------------------------------------


class TestCoercionPreservesIdentifiers:
    """Numeric coercion must NOT corrupt identifiers (SKU/ZIP/phone)."""

    @pytest.mark.parametrize(
        "value",
        ["007", "0099", "02134", "+15551234567", "00000"],
    )
    def test_leading_zero_or_plus_kept_as_string(self, value: str) -> None:
        fields, _src, _u = resolve_fields(
            {"code": "the code"}, json_ld=[{"code": value}], html="", content=""
        )
        assert fields["code"] == value  # exact string, not int-coerced

    @pytest.mark.parametrize(
        "value,expected",
        [("1,299.00", 1299.0), ("1299", 1299), ("0", 0), ("0.5", 0.5), ("12.5", 12.5)],
    )
    def test_genuine_quantities_still_coerced(self, value: str, expected: object) -> None:
        fields, _src, _u = resolve_fields(
            {"price": "the price"}, json_ld=[{"price": value}], html="", content=""
        )
        assert fields["price"] == expected


class TestMatchingRejectsGarbage:
    """A field must stay unresolved rather than bind to an unrelated key."""

    def test_price_not_bound_to_price_currency(self) -> None:
        fields, _s, unresolved = resolve_fields(
            {"price": "the price"}, json_ld=[{"priceCurrency": "USD"}], html="", content=""
        )
        assert "price" not in fields
        assert "price" in unresolved

    def test_author_not_bound_to_authored_on(self) -> None:
        _fields, _s, unresolved = resolve_fields(
            {"author": "who wrote it"}, json_ld=[{"authoredOn": "2020"}], html="", content=""
        )
        assert "author" in unresolved

    def test_price_not_bound_to_long_dom_label(self) -> None:
        html = "<dl><dt>Price Match Guarantee</dt><dd>Yes we match</dd></dl>"
        _fields, _s, unresolved = resolve_fields(
            {"price": "the price"}, json_ld=[], html=html, content=""
        )
        assert "price" in unresolved

    def test_name_not_bound_to_username(self) -> None:
        html = "<dl><dt>Username</dt><dd>jdoe</dd></dl>"
        _fields, _s, unresolved = resolve_fields(
            {"name": "the name"}, json_ld=[], html=html, content=""
        )
        assert "name" in unresolved

    def test_real_nested_and_wrapper_paths_still_resolve(self) -> None:
        # The tightening must NOT break legitimate wrapper-path matches.
        fields, sources, _u = resolve_fields(
            {"price": "the price", "author": "who wrote it"},
            json_ld=[{"offers": {"price": "19.99"}, "author": {"name": "Jane Doe"}}],
            html="",
            content="",
        )
        assert fields["price"] == 19.99
        assert fields["author"] == "Jane Doe"
        assert sources["price"] == "json-ld"
