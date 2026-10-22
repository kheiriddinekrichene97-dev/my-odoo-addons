"""The decision core of the ``quant_unit_v1`` checked projection (ADR 0009 §5, §6).

Free of every Odoo import. The model method in ``models/quant_unit_projection.py`` gathers facts
under the calling identity; this module decides the answer and builds the one response.

## The dependency path, at the pinned image

``stock.quant.product_uom_id`` is ``Many2one('uom.uom', related='product_id.uom_id')``, **not
stored** (``addons/stock/models/stock_quant.py:52``). A related field takes ``compute_sudo =
related_sudo``, true by default, so every read of it walks the path **as superuser**:

1. ``stock.quant.product_id`` -- plain stored ``Many2one('product.product')``, ``required``,
   ``ondelete='restrict'``, ``check_company=True`` (``stock_quant.py:44``);
2. ``product.product.uom_id`` -- the ``_inherits`` delegation of ``product_tmpl_id.uom_id``, built
   with ``related_sudo=False``;
3. ``product.product.product_tmpl_id`` -- plain stored, ``required``, ``ondelete='cascade'``;
4. ``product.template.uom_id`` -- plain stored ``Many2one('uom.uom')``, ``required``
   (``addons/product/models/product_template.py:118``);
5. the ``uom.uom`` row.

The integration suite asserts this path on the running registry before trusting it.

## What this projection proves, and what it never reads

Each hop is proven as the caller: the quant row; the fields ``stock.quant.product_id``,
``product.product.product_tmpl_id``, ``product.product.uom_id`` (the delegated field the related
traverses) and ``product.template.uom_id``; the product row; the template row; and the unit row.
The unit returned is ``template.uom_id`` read after all of that. Neither the elevated
``stock.quant.product_uom_id`` nor the delegated ``product.product.uom_id`` is ever read: the first
is the value being replaced, and there is no stored copy to compare against, so a tripwire would
only re-run the same elevated walk.

## What cannot be derived under caller rights -- or at all

* **The unit is the product's current unit, not the unit the quantity was recorded in.** Odoo 19
  lets a template's ``uom_id`` change after stock exists (``product_template.py:586-588``): moves
  and move lines are relabelled with ``skip_uom_conversion=True`` (``stock/models/product.py``
  ``_update_uom``), and quant quantities are not converted. Nothing on the quant records the unit
  its quantity was entered in, so no read -- elevated or not -- can detect that a quantity
  predates a change. The integration suite pins the behaviour.
* **Nothing is stale in the usual sense.** The field is not stored, so there is no copy to go stale;
  a unit changed in SQL is simply read fresh.
"""

from __future__ import annotations

from typing import Final, Literal, NamedTuple

SCHEMA_VERSION: Final = 1
PROJECTION_NAME: Final = "quant_unit_v1"

MAX_QUANT_IDS: Final = 200
"""Hard bound on one call, the same as the other projections'."""

GUARDED_FIELDS: Final = (
    ("stock.quant", "product_id"),
    ("product.product", "product_tmpl_id"),
    ("product.product", "uom_id"),
    ("product.template", "uom_id"),
)
"""Every field on the path the caller must be allowed to read, whether or not it is read."""

Status = Literal[
    "verified",
    "quant-not-visible",
    "field-not-readable",
    "product-missing",
    "product-not-visible",
    "template-missing",
    "template-not-visible",
    "uom-missing",
    "uom-not-visible",
]

STATUSES: Final[tuple[Status, ...]] = (
    "verified",
    "quant-not-visible",
    "field-not-readable",
    "product-missing",
    "product-not-visible",
    "template-missing",
    "template-not-visible",
    "uom-missing",
    "uom-not-visible",
)
"""The closed answer vocabulary, in check order. ``quant-not-visible`` conflates absent and
hidden; ``field-not-readable`` does not name the field."""


class QuantUnitFacts(NamedTuple):
    """One quant's path, gathered under the calling identity. An id is ``None`` when the link was
    empty *or* was not reached; the preceding flag says which."""

    quant_id: int
    quant_visible: bool
    fields_readable: bool
    product_id: int | None
    product_visible: bool
    template_id: int | None
    template_visible: bool
    uom_id: int | None
    uom_visible: bool


class QuantUnitResult(NamedTuple):
    quant_id: int
    status: Status
    uom_id: int | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "quant_id": self.quant_id,
            "status": self.status,
            "uom_id": self.uom_id if self.status == "verified" else False,
        }


class ProjectionInputError(ValueError):
    """The request is malformed or out of bounds. Carries a closed code, never an input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_quant_ids(raw: object) -> tuple[int, ...]:
    """An explicit, bounded, de-duplicated list of positive integers; ``True`` is not an id."""

    if not isinstance(raw, list | tuple):
        raise ProjectionInputError("quant-ids-not-a-list")
    if not raw:
        raise ProjectionInputError("quant-ids-empty")
    if len(raw) > MAX_QUANT_IDS:
        raise ProjectionInputError("quant-ids-over-bound")
    seen: dict[int, None] = {}
    for value in raw:
        if type(value) is not int or value <= 0:
            raise ProjectionInputError("quant-id-not-a-positive-integer")
        seen.setdefault(value, None)
    return tuple(seen)


def decide(facts: QuantUnitFacts) -> QuantUnitResult:
    """One quant's answer, hop by hop. The unit only at the very end."""

    def refuse(status: Status) -> QuantUnitResult:
        return QuantUnitResult(facts.quant_id, status)

    if not facts.quant_visible:
        return refuse("quant-not-visible")
    if not facts.fields_readable:
        return refuse("field-not-readable")
    # Every link below is `required` upstream; an empty one is a broken row, not an answer.
    if facts.product_id is None:
        return refuse("product-missing")
    if not facts.product_visible:
        return refuse("product-not-visible")
    if facts.template_id is None:
        return refuse("template-missing")
    if not facts.template_visible:
        return refuse("template-not-visible")
    if facts.uom_id is None:
        return refuse("uom-missing")
    if not facts.uom_visible:
        return refuse("uom-not-visible")
    return QuantUnitResult(facts.quant_id, "verified", facts.uom_id)


def build_response(results: tuple[QuantUnitResult, ...]) -> dict[str, object]:
    """The fixed envelope. Same keys, same order, whatever the outcome."""

    return {
        "schema_version": SCHEMA_VERSION,
        "projection": PROJECTION_NAME,
        "results": [result.as_payload() for result in results],
    }
