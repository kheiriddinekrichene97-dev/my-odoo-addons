"""The decision core of the ``product_identity_v1`` checked projection (ADR 0009 §5, §6).

Free of every Odoo import. The model method in ``models/product_identity_projection.py`` gathers
facts under the calling identity; this module decides the answer and builds the one response.

## What is projected, and why these five are blocked

``product.product`` carries ``_inherits = {'product.template': 'product_tmpl_id'}``
(``addons/product/models/product_product.py:19`` in the pinned image). Its ``name``, ``type``,
``is_storable``, ``uom_id`` and ``company_id`` are **delegated**: non-stored related fields Odoo
builds with an explicit ``related_sudo=False`` (``odoo/orm/model_classes.py``), so they are not
elevated. They are blocked because ADR 0009 prohibits delegated fields by default, whatever their
elevation. The integration suite measures those effective attributes on real Odoo rather than
trusting this paragraph.

## What this projection proves before answering

The value of a delegated field lives on ``product.template``. A readable ``product.product`` row
is **not** evidence that the template, or each of the five template fields, is readable:

* ``ir.rule`` carries the template's *rules* down to the product (``_compute_domain`` adds
  ``product_tmpl_id any <template domain>``, ``odoo/addons/base/models/ir_rule.py:143-150``), but
  not the template's *ACL*, and not a field's ``groups``. A product row can be readable while its
  template, or one template field, is not.
* The ORM checks *record* access (ACL and rules) when it fetches, not when it serves a value
  already in the transaction's cache: a template value another environment loaded earlier is
  returned to a caller with no right to the template (measured). A field's ``groups`` is enforced
  on every read, cached or not (measured), so the explicit field check is defence in depth.

So each value is taken from the template, and only after the template row and all five template
fields are proven readable as the caller. It is never read through the product, and a readable
product alone forwards nothing.

## Per-field semantics at the pinned image, and what the core enforces

* ``name`` -- ``Char``, ``required``, ``translate=True``. The value is the translation for the
  caller's language, falling back to ``en_US``; the language comes from the call's context, which
  JSON-2 lets the caller set. It is returned with that language named in the envelope (``lang``). It
  is a display value: it cannot stand in for the product pack's server-side name *search*, which
  matches in the same language-dependent way and which this projection does not replace.
* ``type`` -- ``Selection`` of ``consu``, ``service``, ``combo`` in the pinned scope. Any other
  value (a module extending the selection) refuses the product: ``type-unrecognized``.
* ``is_storable`` -- a *stored* compute on the template, ``readonly=False``; its only compute,
  ``compute_is_storable`` (``@api.depends('type')``), clears it when ``type != 'consu'``. It reads
  nothing but its own record, so its default ``compute_sudo`` crosses no record boundary. The one
  invariant that compute guarantees is checked: ``is_storable`` with a ``type`` other than ``consu``
  is a stale stored value, refused as ``storability-conflicting``.
* ``uom_id`` -- ``Many2one('uom.uom')``, ``required``. The unit row must itself be readable as the
  caller, or the product is refused (``uom-missing`` / ``uom-not-visible``).
* ``company_id`` -- ``Many2one('res.company')``, optional: ``False`` means shared across companies.
  When set, the company row must be readable as the caller (``company-not-visible``). The template
  rule is ``company_id parent_of company_ids``, so a visible product may belong to a *parent* of an
  active company; the projection reports the company and leaves the comparison with a requested
  company to the consumer.
"""

from __future__ import annotations

from typing import Final, Literal, NamedTuple

SCHEMA_VERSION: Final = 1
PROJECTION_NAME: Final = "product_identity_v1"

MAX_PRODUCT_IDS: Final = 200
"""Hard bound on one call, the same as the picking projections'."""

PROJECTED_FIELDS: Final = ("name", "type", "is_storable", "uom_id", "company_id")
"""The five delegated ``product.product`` fields, each read from ``product.template``."""

PRODUCT_TYPES: Final = ("consu", "service", "combo")
"""``product.template.type``'s selection in the pinned scope. Anything else is refused."""

Status = Literal[
    "verified",
    "product-not-visible",
    "template-missing",
    "template-not-visible",
    "field-not-readable",
    "type-unrecognized",
    "storability-conflicting",
    "uom-missing",
    "uom-not-visible",
    "company-not-visible",
]

STATUSES: Final[tuple[Status, ...]] = (
    "verified",
    "product-not-visible",
    "template-missing",
    "template-not-visible",
    "field-not-readable",
    "type-unrecognized",
    "storability-conflicting",
    "uom-missing",
    "uom-not-visible",
    "company-not-visible",
)
"""The closed answer vocabulary, in the order the checks run.

``product-not-visible`` conflates "no such product" with "hidden from you", so the projection is
not an existence oracle. ``field-not-readable`` does not say which field, for the same reason.
"""


class ProductFacts(NamedTuple):
    """Everything gathered about one product, under the calling identity.

    The five values are the **template's**, and are present only when the template row and every
    projected template field were proven readable; otherwise they are ``None`` and never consulted.
    """

    product_id: int
    product_visible: bool
    link_readable: bool
    """Whether the caller may read ``product.product.product_tmpl_id`` itself (its ``groups``)."""

    template_id: int | None
    template_visible: bool
    fields_readable: bool
    name: str | None
    type: str | None
    is_storable: bool | None
    uom_id: int | None
    uom_visible: bool
    company_id: int | None
    company_visible: bool


class ProductIdentityResult(NamedTuple):
    product_id: int
    status: Status
    name: str | None = None
    type: str | None = None
    is_storable: bool | None = None
    uom_id: int | None = None
    company_id: int | None = None

    def as_payload(self) -> dict[str, object]:
        """Fixed keys. A refused product carries ``False`` for every value.

        On a verified product ``company_id: False`` means "shared"; on a refused one it means
        nothing, which is why the status is always read first.
        """

        verified = self.status == "verified"
        return {
            "product_id": self.product_id,
            "status": self.status,
            "name": self.name if verified else False,
            "type": self.type if verified else False,
            "is_storable": self.is_storable if verified else False,
            "uom_id": self.uom_id if verified else False,
            "company_id": (self.company_id or False) if verified else False,
        }


class ProjectionInputError(ValueError):
    """The request is malformed or out of bounds. Carries a closed code, never an input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_product_ids(raw: object) -> tuple[int, ...]:
    """An explicit, bounded, de-duplicated list of positive integers; ``True`` is not an id."""

    if not isinstance(raw, list | tuple):
        raise ProjectionInputError("product-ids-not-a-list")
    if not raw:
        raise ProjectionInputError("product-ids-empty")
    if len(raw) > MAX_PRODUCT_IDS:
        raise ProjectionInputError("product-ids-over-bound")
    seen: dict[int, None] = {}
    for value in raw:
        if type(value) is not int or value <= 0:
            raise ProjectionInputError("product-id-not-a-positive-integer")
        seen.setdefault(value, None)
    return tuple(seen)


def decide(facts: ProductFacts) -> ProductIdentityResult:
    """One product's answer. The least-disclosing refusal wins; values only at the very end."""

    def refuse(status: Status) -> ProductIdentityResult:
        return ProductIdentityResult(facts.product_id, status)

    if not facts.product_visible:
        return refuse("product-not-visible")
    if not facts.link_readable:
        return refuse("field-not-readable")
    # `product_tmpl_id` is required with ondelete cascade; absence is a broken row, not a product.
    if facts.template_id is None:
        return refuse("template-missing")
    if not facts.template_visible:
        return refuse("template-not-visible")
    if not facts.fields_readable:
        return refuse("field-not-readable")
    if type(facts.name) is not str or not facts.name:
        return refuse("field-not-readable")
    if type(facts.type) is not str or facts.type not in PRODUCT_TYPES:
        return refuse("type-unrecognized")
    if type(facts.is_storable) is not bool:
        return refuse("field-not-readable")
    # compute_is_storable's one guarantee: a product that is not goods is never storable.
    if facts.is_storable and facts.type != "consu":
        return refuse("storability-conflicting")
    if facts.uom_id is None:
        return refuse("uom-missing")
    if not facts.uom_visible:
        return refuse("uom-not-visible")
    if facts.company_id is not None and not facts.company_visible:
        return refuse("company-not-visible")
    return ProductIdentityResult(
        facts.product_id,
        "verified",
        name=facts.name,
        type=facts.type,
        is_storable=facts.is_storable,
        uom_id=facts.uom_id,
        company_id=facts.company_id,
    )


def build_response(lang: str, results: tuple[ProductIdentityResult, ...]) -> dict[str, object]:
    """The fixed envelope. ``lang`` names the language every returned ``name`` was read in."""

    return {
        "schema_version": SCHEMA_VERSION,
        "projection": PROJECTION_NAME,
        "lang": lang,
        "results": [result.as_payload() for result in results],
    }
