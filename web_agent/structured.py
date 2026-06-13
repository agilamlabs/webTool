"""Deterministic, LLM-free field resolver for schema-guided extraction (v1.7.0 Wave 6).

webTool makes NO LLM calls in-package -- it is a TOOL for agents that HAVE an
LLM. So schema-guided structured extraction ("give a schema of field names ->
get typed fields back", the headline capability of Firecrawl / ScrapeGraphAI /
AgentQL) is implemented here as a DETERMINISTIC resolver that maps each
requested field name to the strongest available STRUCTURED page signal.

This genuinely covers a large slice of the real web: e-commerce, articles,
organizations, and events almost universally ship JSON-LD / OpenGraph /
microdata / labelled DOM that names exactly the fields a caller asks for
(price, name, sku, author, date, rating, ...). For freeform-prose fields a
deterministic resolver cannot reach, :meth:`Recipes.extract_fields` accepts an
OPTIONAL injected LLM-extractor hook (Python-API only) so the calling agent can
fill the remainder with its own model.

Signal sources, tried in PRIORITY order (first match wins), each tagged on the
returned ``field_sources`` map:

1. ``json-ld``    -- flattened ``<script type="application/ld+json">`` objects
   (already parsed upstream into ``ExtractionResult.structured_data`` and
   passed in here, so we never re-parse the script blocks). Handles common
   schema.org shapes incl. nested ``offers.price`` / ``brand.name`` /
   ``aggregateRating.ratingValue`` / ``address.*``.
2. ``opengraph`` -- ``<meta property="og:..."/"product:..."/"article:...">``.
3. ``meta``      -- ``<meta name="...">`` (description, keywords, author, ...).
4. ``microdata`` -- ``[itemprop]`` elements (``content`` attr or text).
5. ``dom``       -- labelled value patterns: ``<dt>/<dd>`` definition lists,
   ``<th>/<td>`` table rows, ``<label>`` + associated input value.

HONEST SCOPE: deterministic best-effort from structured signals. Values are
returned as the page provides them (strings), with a LIGHT numeric coercion
only when a value is purely numeric -- no schema typing is inferred or enforced.
Malformed HTML never raises (we return whatever resolved). Bounds (field count,
per-value length, DOM pairs scanned) come from ``ExtractionConfig`` so a hostile
page cannot blow up the result.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Union

from bs4 import BeautifulSoup, Tag

# Fallback bounds used when no ExtractionConfig is threaded in (the resolver is
# pure and usable standalone). Recipes passes the configured caps.
_DEFAULT_MAX_FIELDS = 50
_DEFAULT_MAX_VALUE_CHARS = 4000
# Cap on labelled DOM (dt/dd, th/td, label) pairs scanned -- a hostile page
# with 100k table rows must not turn field resolution into an O(rows) walk.
_MAX_DOM_PAIRS = 2000

# Source tags, in the PRIORITY order they are consulted (first match wins).
SOURCE_JSON_LD = "json-ld"
SOURCE_OPENGRAPH = "opengraph"
SOURCE_META = "meta"
SOURCE_MICRODATA = "microdata"
SOURCE_DOM = "dom"

# Built-in alias table: maps a normalized requested-field token to the set of
# normalized signal-key tokens that should be treated as equivalent. Bi-
# directional matching is applied (a requested "cost" matches a signal "price"
# and vice versa), so each row is an equivalence class.
_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"price", "cost", "amount", "pricing"}),
    frozenset({"name", "title", "product", "headline", "productname"}),
    frozenset({"author", "by", "writer", "creator", "byline"}),
    frozenset({"date", "published", "publisheddate", "datepublished", "pubdate"}),
    frozenset({"description", "summary", "desc", "about", "abstract"}),
    frozenset({"sku", "id", "productid", "itemid", "mpn"}),
    frozenset({"rating", "stars", "score", "ratingvalue"}),
    frozenset({"brand", "manufacturer", "make", "vendor"}),
    frozenset({"image", "photo", "picture", "thumbnail", "img"}),
    frozenset({"currency", "pricecurrency"}),
)

# A purely numeric value (optionally signed / decimal / thousands-separated)
# that we coerce to int/float. Currency symbols / units are intentionally NOT
# stripped here -- we coerce only when the WHOLE value is a bare number.
_PURE_NUMBER = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^[+-]?\d+(?:\.\d+)?$")

SchemaLike = Union[dict[str, str], list[str]]


def _normalize(name: str) -> str:
    """Lowercase, strip, collapse ``_``/``-``/dots and whitespace to single spaces."""
    s = name.strip().lower()
    s = re.sub(r"[_\-.]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _tokens(normalized: str) -> set[str]:
    """Word tokens (length >= 1) of an already-normalized name."""
    return {t for t in normalized.split(" ") if t}


def _normalize_schema(schema: SchemaLike) -> dict[str, str]:
    """Normalize a list-of-names OR dict-of-name->hint into a name->hint dict.

    A ``list[str]`` becomes ``{name: ""}``. A dict is copied with non-string
    hints coerced to ``""``. Field order is preserved (dicts and lists keep
    insertion order) so the FIRST-declared field wins a same-source tie.
    """
    out: dict[str, str] = {}
    if isinstance(schema, dict):
        for name, hint in schema.items():
            if not isinstance(name, str) or not name.strip():
                continue
            out[name] = hint if isinstance(hint, str) else ""
    else:
        for name in schema:
            if isinstance(name, str) and name.strip():
                out.setdefault(name, "")
    return out


def _coerce_value(value: Any, *, max_chars: int) -> Any:
    """Return the page value as-is (string), with light numeric coercion + a length cap.

    - Non-strings (already-typed JSON-LD numbers/bools) pass through unchanged.
    - A string that is PURELY numeric becomes int/float (``"1,299.00"`` ->
      ``1299.0``); thousands separators are tolerated.
    - Any other string is stripped and truncated to ``max_chars``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        # list / dict that slipped through -- stringify defensively, capped.
        text = str(value)
        return text[:max_chars]
    text = value.strip()
    if _PURE_NUMBER.match(text):
        cleaned = text.replace(",", "")
        body = cleaned[1:] if cleaned[:1] in "+-" else cleaned
        digits = body.replace(".", "")
        # Conservative coercion: only coerce genuine QUANTITIES, never
        # IDENTIFIERS whose exact form is meaningful. A leading '+' marks an
        # (international) phone; a leading-zero integer is a SKU / ZIP / code
        # ("007", "02134"); a >15-digit run is a phone / UPC / EAN. Decimals
        # ("0.5", "1,299.00") are real quantities and coerce fine.
        is_phone_plus = text[:1] == "+"
        is_leading_zero_int = "." not in body and len(body) > 1 and body[:1] == "0"
        too_many_digits = len(digits) > 15
        if not (is_phone_plus or is_leading_zero_int or too_many_digits):
            try:
                return int(cleaned) if "." not in cleaned else float(cleaned)
            except ValueError:  # pragma: no cover -- regex already guards this
                pass
    return text[:max_chars]


def _flatten_json_ld(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten JSON-LD objects into a lowercased property->value map.

    Produces BOTH the leaf key and dotted paths so a requested ``price`` can
    match ``offers.price`` and a requested ``offers.price`` matches too:

    - scalar ``name`` -> ``{"name": ...}``
    - nested ``offers: {price: 9}`` -> ``{"offers.price": 9, "price": 9}``
    - list ``offers: [{price: 9}]`` -> uses the FIRST element, same as above
    - ``brand: {name: "X"}`` -> ``{"brand.name": "X", "brand": "X"}`` (an
      object carrying a single ``name``/``value`` leaf also collapses to its
      parent key so ``brand`` resolves directly).

    Earlier blocks win over later ones on key collision (the first JSON-LD
    object on the page is typically the primary entity). ``@``-prefixed keys
    (``@type`` / ``@context``) are skipped.
    """
    flat: dict[str, Any] = {}

    def _put(key: str, value: Any) -> None:
        k = key.lower()
        if k and k not in flat:
            flat[k] = value

    def _walk(obj: Any, prefix: str, depth: int) -> None:
        if depth > 6 or not isinstance(obj, dict):
            return
        for raw_key, value in obj.items():
            if not isinstance(raw_key, str) or raw_key.startswith("@"):
                continue
            key = raw_key.lower()
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                # Collapse a single-leaf wrapper ({name|value: X}) onto the
                # parent key so brand/author/etc. resolve without a dotted hint.
                for leaf in ("name", "value", "ratingvalue", "price", "url"):
                    if leaf in {k.lower() for k in value if isinstance(k, str)}:
                        leaf_val = next(
                            v for k, v in value.items() if isinstance(k, str) and k.lower() == leaf
                        )
                        if not isinstance(leaf_val, (dict, list)):
                            _put(dotted, leaf_val)
                            if not prefix:
                                _put(key, leaf_val)
                        break
                _walk(value, dotted, depth + 1)
            elif isinstance(value, list):
                if value and isinstance(value[0], dict):
                    _walk(value[0], dotted, depth + 1)
                elif value and not isinstance(value[0], (dict, list)):
                    _put(dotted, value[0])
                    if not prefix:
                        _put(key, value[0])
            else:
                _put(dotted, value)
                if not prefix:
                    _put(key, value)

    for block in blocks:
        if isinstance(block, dict):
            _walk(block, "", 0)
    return flat


def _meta_maps(soup: BeautifulSoup) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(opengraph_map, meta_name_map)`` from the page's ``<meta>`` tags.

    OpenGraph map keys are the property suffix after the namespace separator
    (``og:title`` -> ``title``, ``product:price:amount`` -> ``price amount`` /
    ``price``, ``article:published_time`` -> ``published time`` / ``published``).
    The ``<meta name=...>`` map is keyed by the normalized ``name``.
    """
    og: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    for tag in soup.find_all("meta"):
        if not isinstance(tag, Tag):
            continue
        content = tag.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        prop = tag.get("property")
        name = tag.get("name")
        if isinstance(prop, str) and ":" in prop:
            # og:title / product:price:amount / article:published_time
            parts = [p for p in re.split(r"[:_]", prop.lower()) if p]
            if len(parts) >= 2:
                tail = parts[1:]
                full = " ".join(tail)
                og.setdefault(full, content)
                # also index the trailing leaf so ``price`` hits
                # ``product:price:amount`` and ``published`` hits
                # ``article:published_time``.
                og.setdefault(tail[-1], content)
                og.setdefault(tail[0], content)
        elif isinstance(name, str) and name.strip():
            meta.setdefault(_normalize(name), content)
    return og, meta


def _microdata_map(soup: BeautifulSoup, *, limit: int) -> dict[str, Any]:
    """Map ``[itemprop]`` -> ``content`` attr (or text) for the first ``limit`` items."""
    out: dict[str, Any] = {}
    count = 0
    for tag in soup.find_all(attrs={"itemprop": True}):
        if not isinstance(tag, Tag):
            continue
        if count >= limit:
            break
        prop = tag.get("itemprop")
        names = prop if isinstance(prop, list) else [prop]
        value: Optional[str] = None
        content_attr = tag.get("content")
        if isinstance(content_attr, str) and content_attr.strip():
            value = content_attr.strip()
        else:
            text = tag.get_text(" ", strip=True)
            if text:
                value = text
        if value is None:
            continue
        for nm in names:
            if isinstance(nm, str) and nm.strip():
                out.setdefault(_normalize(nm), value)
                count += 1
    return out


def _dom_pairs(soup: BeautifulSoup, *, limit: int) -> dict[str, Any]:
    """Build a label->value map from labelled DOM patterns (bounded by ``limit``).

    Scans, in order: definition lists (``<dt>`` label / following ``<dd>``
    value), table rows (``<th>`` label / following ``<td>`` value), and
    ``<label>`` text paired with the value of its associated control (``for=``
    id or a wrapped input). The first value for a given normalized label wins.
    """
    out: dict[str, Any] = {}

    def _add(label: str, value: str) -> bool:
        norm = _normalize(label)
        if not norm or not value.strip():
            return False
        out.setdefault(norm, value.strip())
        return True

    scanned = 0
    # 1. <dt>/<dd> definition lists
    for dt in soup.find_all("dt"):
        if scanned >= limit:
            break
        if not isinstance(dt, Tag):
            continue
        dd = dt.find_next_sibling("dd")
        if isinstance(dd, Tag):
            _add(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))
            scanned += 1

    # 2. <th>/<td> table rows (header cell + first data cell in the same row)
    for th in soup.find_all("th"):
        if scanned >= limit:
            break
        if not isinstance(th, Tag):
            continue
        td = th.find_next_sibling("td")
        if isinstance(td, Tag):
            _add(th.get_text(" ", strip=True), td.get_text(" ", strip=True))
            scanned += 1

    # 3. <label> + associated input value
    for label in soup.find_all("label"):
        if scanned >= limit:
            break
        if not isinstance(label, Tag):
            continue
        target: Optional[Tag] = None
        for_id = label.get("for")
        if isinstance(for_id, str) and for_id:
            found = soup.find(id=for_id)
            if isinstance(found, Tag):
                target = found
        if target is None:
            wrapped = label.find(["input", "select", "textarea"])
            if isinstance(wrapped, Tag):
                target = wrapped
        if target is None:
            continue
        value = target.get("value")
        if isinstance(value, str) and value.strip():
            _add(label.get_text(" ", strip=True), value)
            scanned += 1
    return out


def _match_key(
    field_norm: str,
    field_tokens: set[str],
    hint_tokens: set[str],
    signal_map: dict[str, Any],
) -> Optional[str]:
    """Find the best signal key for one requested field within ONE source map.

    Match tiers (first hit wins):
      1. exact normalized-name equality
      2. alias-table equivalence (price<->cost<->amount, name<->title, ...)
      3. substring containment (field in key OR key in field)
      4. token overlap (shares at least one token), using the schema hint
         words too so a sparse field name can still match.
    Returns the matching signal key, or None.
    """
    if not signal_map:
        return None
    norm_keys = {_normalize(k): k for k in signal_map}

    # 1. exact
    if field_norm in norm_keys:
        return norm_keys[field_norm]

    # A single-token field must not bind to a long (>2-token) key: a generic
    # word inside a label sentence ("price" in "Price Match Guarantee", "name"
    # in a "Display Name Policy") is almost never the field's value. Short keys
    # and multi-token fields are specific enough to trust.
    field_multi = len(field_tokens) >= 2

    # 2. alias equivalence: expand the field tokens to their alias siblings,
    # then look for a key whose tokens intersect that expanded set on a
    # known alias axis.
    field_alias = set(field_tokens)
    for group in _ALIAS_GROUPS:
        if field_tokens & group:
            field_alias |= group
    for kn, original in norm_keys.items():
        ktoks = _tokens(kn)
        if not field_multi and len(ktoks) > 2:
            continue
        # Require the overlap to be on an alias axis OR a shared real token, so
        # a coincidental token (e.g. "the") cannot trigger an alias match.
        if (ktoks & field_alias) and (
            (ktoks & field_tokens)
            or any((field_tokens & g) and (ktoks & g) for g in _ALIAS_GROUPS)
        ):
            return original

    # 3. token-subset: every requested-field token appears as a key token.
    # For a SINGLE-token field, only accept a SHORT key (<= 2 tokens) so a
    # generic word buried in a long label ("price" in "Price Match Guarantee")
    # can't bind, while a wrapper path ("offers.price" -> "offers price",
    # "author.name" -> "author name") still matches. Camel-case keys
    # ("priceCurrency" -> one token "pricecurrency"; "authoredOn") are single
    # tokens, so a single-token field is correctly NOT bound to them.
    for kn, original in norm_keys.items():
        ktoks = _tokens(kn)
        if field_tokens and field_tokens <= ktoks and (field_multi or len(ktoks) <= 2):
            return original

    # 4. token overlap (include hint words to help a sparse field name). Skip
    # long (>2-token) keys for a single-token field, same anti-label guard.
    search_tokens = field_tokens | hint_tokens
    best: Optional[str] = None
    best_overlap = 0
    for kn, original in norm_keys.items():
        ktoks = _tokens(kn)
        if not field_multi and len(ktoks) > 2:
            continue
        overlap = len(ktoks & search_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best = original
    return best if best_overlap > 0 else None


def resolve_fields(
    schema: SchemaLike,
    *,
    json_ld: Optional[list[dict[str, Any]]] = None,
    html: Optional[str] = None,
    content: Optional[str] = None,
    max_fields: int = _DEFAULT_MAX_FIELDS,
    max_value_chars: int = _DEFAULT_MAX_VALUE_CHARS,
    max_dom_pairs: int = _MAX_DOM_PAIRS,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Resolve requested field names to values from the strongest page signal.

    Args:
        schema: ``dict[str, str]`` (field_name -> a human hint/description used
            for fuzzy matching) OR a ``list[str]`` of field names. Both are
            normalized to a dict internally.
        json_ld: Parsed JSON-LD objects (pass
            ``ExtractionResult.structured_data`` so the ``<script>`` blocks are
            not re-parsed). Highest-priority source.
        html: Raw page HTML, parsed ONCE with BeautifulSoup for the OpenGraph /
            meta / microdata / DOM sources. ``content`` is accepted for API
            symmetry / future use but the deterministic resolver works off the
            structured HTML signals, not the cleaned prose.
        content: Cleaned main-content text (currently unused by the
            deterministic path; reserved so a caller / LLM hook has the same
            cleaned text the extractor produced).
        max_fields: Cap on the number of resolved fields (excess requested
            fields are reported as unresolved, never silently dropped).
        max_value_chars: Cap on each resolved value's length.
        max_dom_pairs: Cap on labelled-DOM pairs scanned.

    Returns:
        ``(fields, field_sources, unresolved)`` where ``fields`` maps each
        resolved field NAME (verbatim, as the caller wrote it) to its value,
        ``field_sources`` maps the same names to the source tag that won
        (``json-ld`` | ``opengraph`` | ``meta`` | ``microdata`` | ``dom``),
        and ``unresolved`` lists the requested names no source could fill.

    Never raises on malformed HTML -- returns whatever resolved.
    """
    normalized_schema = _normalize_schema(schema)

    # Build each source map once (priority order: json-ld, og, meta, microdata, dom).
    sources: list[tuple[str, dict[str, Any]]] = []
    sources.append((SOURCE_JSON_LD, _flatten_json_ld(json_ld or [])))

    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:  # never raise on malformed html; degrade to empty
            soup = BeautifulSoup("", "html.parser")
        og_map, meta_map = _meta_maps(soup)
        sources.append((SOURCE_OPENGRAPH, og_map))
        sources.append((SOURCE_META, meta_map))
        sources.append((SOURCE_MICRODATA, _microdata_map(soup, limit=max_dom_pairs)))
        sources.append((SOURCE_DOM, _dom_pairs(soup, limit=max_dom_pairs)))

    fields: dict[str, Any] = {}
    field_sources: dict[str, str] = {}
    unresolved: list[str] = []

    for field_name, hint in normalized_schema.items():
        if len(fields) >= max_fields:
            unresolved.append(field_name)
            continue
        field_norm = _normalize(field_name)
        field_tokens = _tokens(field_norm)
        hint_tokens = _tokens(_normalize(hint)) if hint else set()

        resolved = False
        for source_tag, source_map in sources:
            key = _match_key(field_norm, field_tokens, hint_tokens, source_map)
            if key is not None:
                value = _coerce_value(source_map[key], max_chars=max_value_chars)
                if value is None or (isinstance(value, str) and not value):
                    continue
                fields[field_name] = value
                field_sources[field_name] = source_tag
                resolved = True
                break
        if not resolved:
            unresolved.append(field_name)

    return fields, field_sources, unresolved
