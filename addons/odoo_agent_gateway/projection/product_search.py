"""The decision core of the ``product_search_v1`` bounded search projection (ADR 0009 §5, §6).

Free of every Odoo import. The model method in ``models/product_search_projection.py`` runs the
searches and the identity projection under the caller; this module validates the request, turns the
search term into the exact pattern the product pack sends today, classifies each candidate, and
builds the one response.

## What it replaces

``ProductStockReader.search_products`` sends one ``search_read`` on ``product.product``::

    active = True, type = 'consu', is_storable = True, company_id in [company, False],
    name | default_code | barcode  ilike  %<term, LIKE-escaped>%

ordered by ``id``, with a ``limit + 1`` probe for ``truncated``. Four of those clauses name
``_inherits``-delegated fields (``name``, ``type``, ``is_storable``, ``company_id``). Odoo evaluates
a condition on a delegated field through ``product_tmpl_id``, which carries
``bypass_search_access=True`` (``addons/product/models/product_product.py:42-44`` in the pinned
image), so the template subquery runs **without the template's ACL or field access**: today's
search filters on template values the caller may not be allowed to read. The integration suite
measures that.

## How this answers the same question under the caller

* **Name branch.** ``product.template`` is searched **as the caller** -- its ACL, its rules and its
  fields' ``groups`` in force -- for ``type``, ``is_storable``, the company clause and the name
  pattern, with ``active in (True, False)`` so an archived template is matched exactly as today's
  search matches it; then the active ``product.product`` variants of those templates, as the
  caller.
* **Code branch.** ``product.product`` is searched as the caller on its own plain columns only:
  ``active`` and ``default_code | barcode`` against the same pattern.
* **Verification.** Every candidate goes through ``product_identity_v1`` in the same transaction,
  and is ``included`` only when its identity is verified, it is goods (``consu``), storable, and
  its company is the requested one or none. A verified non-member is ``excluded`` (counted, not
  listed). A candidate whose identity cannot be proven -- a code match whose template the caller
  cannot read, for instance -- is ``refused``: listed with its identity status and no values,
  because it might match.

The name is matched in the call's language with Odoo's own ``en_US`` fallback, exactly as today;
``lang`` names that language. The pattern is the pack's own: the term stripped, ``\\``, ``%`` and
``_`` escaped, wrapped in ``%``.

## One deliberate difference, and the bounds

A product whose template the caller cannot read is **not** found by its name, where today's search
would find it through the bypassed subquery. That is the point: a name the caller may not read is
not evidence. Such a product can still be found by its own code or barcode, and is then ``refused``.

Candidate discovery is bounded -- 200 matching templates, 200 name-branch products, 200 code-branch
products, 200 in the union (the identity projection's bound) -- and any overflow refuses the whole
request; nothing is sliced. The code branch cannot filter on storability without reading the
template, so a short code-like term can overflow where today's search would not; the caller
narrows the term. Only the *listed* rows are cut at ``max_rows``, after every candidate has been
classified, so the prefix returned is exactly the first rows by id and ``truncated`` says whether
more exist.
"""

from __future__ import annotations

from typing import Final, Literal, NamedTuple

SCHEMA_VERSION: Final = 1
PROJECTION_NAME: Final = "product_search_v1"

MAX_SEARCH_TERM_LENGTH: Final = 64
"""The product pack's own bound (``odoo_agent_contracts.v1.product_stock``)."""

MAX_ROWS: Final = 50
"""The pack's ``MAX_PRODUCT_SEARCH_RESULTS``: the most rows one answer lists."""

MAX_TEMPLATE_MATCHES: Final = 200
MAX_BRANCH_PRODUCTS: Final = 200
MAX_CANDIDATES: Final = 200
"""Equal to ``product_identity_v1``'s id bound, so one call verifies every candidate."""

STORABLE_PRODUCT_TYPE: Final = "consu"
LIKE_ESCAPES: Final = (("\\", "\\\\"), ("%", "\\%"), ("_", "\\_"))
"""The pack's escapes, in the pack's order: a term is matched literally, never as a pattern."""

TEMPLATE_SEARCH_FIELDS: Final = ("active", "type", "is_storable", "company_id", "name")
PRODUCT_SEARCH_FIELDS: Final = ("active", "product_tmpl_id", "default_code", "barcode")
"""Every field a search filters on. The caller must be allowed to read each, or a filter would
disclose what it cannot read."""

RequestStatus = Literal[
    "answered",
    "company-not-allowed",
    "model-not-readable",
    "field-not-readable",
    "snapshot-not-established",
    "snapshot-changed",
    "template-matches-over-bound",
    "branch-products-over-bound",
    "candidates-over-bound",
]

REQUEST_STATUSES: Final[tuple[RequestStatus, ...]] = (
    "answered",
    "company-not-allowed",
    "model-not-readable",
    "field-not-readable",
    "snapshot-not-established",
    "snapshot-changed",
    "template-matches-over-bound",
    "branch-products-over-bound",
    "candidates-over-bound",
)
"""Only ``answered`` carries rows and totals; every other status is a whole-request refusal."""

Disposition = Literal["included", "excluded", "refused"]


class SearchInputError(ValueError):
    """The request is malformed. Carries a closed code, never an input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SearchRequest(NamedTuple):
    company_id: int
    term: str
    max_rows: int

    @property
    def pattern(self) -> str:
        """Exactly the value the product pack sends today."""

        escaped = self.term
        for character, replacement in LIKE_ESCAPES:
            escaped = escaped.replace(character, replacement)
        return f"%{escaped}%"


def validate_request(*, company_id: object, search_term: object, max_rows: object) -> SearchRequest:
    """The whole grammar. Types are checked exactly, so ``True`` is not an id."""

    if type(company_id) is not int or company_id < 1:
        raise SearchInputError("company-id-not-a-positive-integer")
    if type(search_term) is not str or not 1 <= len(search_term) <= MAX_SEARCH_TERM_LENGTH:
        raise SearchInputError("search-term-not-a-bounded-string")
    term = search_term.strip()
    if not term:
        raise SearchInputError("search-term-empty")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        raise SearchInputError("max-rows-out-of-bounds")
    return SearchRequest(company_id, term, max_rows)


class Classified(NamedTuple):
    product_id: int
    disposition: Disposition
    identity: dict[str, object]


def classify(identity: dict[str, object], request: SearchRequest) -> Classified:
    """One candidate, from its ``product_identity_v1`` row."""

    product_id = identity["product_id"]
    assert type(product_id) is int
    if identity["status"] != "verified":
        return Classified(product_id, "refused", identity)
    member = (
        identity["type"] == STORABLE_PRODUCT_TYPE
        and identity["is_storable"] is True
        and identity["company_id"] in (request.company_id, False)
    )
    return Classified(product_id, "included" if member else "excluded", identity)


def _row(item: Classified) -> dict[str, object]:
    identity = item.identity
    included = item.disposition == "included"
    return {
        "product_id": item.product_id,
        "disposition": item.disposition,
        "identity_status": identity["status"],
        "name": identity["name"] if included else False,
        "type": identity["type"] if included else False,
        "is_storable": identity["is_storable"] if included else False,
        "uom_id": identity["uom_id"] if included else False,
        "company_id": identity["company_id"] if included else False,
    }


def _envelope(
    status: RequestStatus,
    lang: str | bool,
    request_company: int | bool,
    truncated: bool,
    totals: dict[str, int] | bool,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "projection": PROJECTION_NAME,
        "status": status,
        "lang": lang,
        "company_id": request_company,
        "truncated": truncated,
        "totals": totals,
        "rows": rows,
    }


def refuse_request(status: RequestStatus) -> dict[str, object]:
    """A whole-request refusal: nothing in it can be read as an answer or as an empty result."""

    assert status != "answered"
    return _envelope(status, False, False, False, False, [])


def build_response(
    classified: tuple[Classified, ...], request: SearchRequest, lang: str
) -> dict[str, object]:
    """Every candidate classified; the listed rows (included and refused) in id order, cut at
    ``max_rows`` with ``truncated`` saying whether more exist."""

    listed = sorted(
        (item for item in classified if item.disposition != "excluded"),
        key=lambda item: item.product_id,
    )
    totals = {
        "candidates": len(classified),
        "included": sum(1 for item in classified if item.disposition == "included"),
        "refused": sum(1 for item in classified if item.disposition == "refused"),
        "excluded": sum(1 for item in classified if item.disposition == "excluded"),
    }
    return _envelope(
        "answered",
        lang,
        request.company_id,
        len(listed) > request.max_rows,
        totals,
        [_row(item) for item in listed[: request.max_rows]],
    )
