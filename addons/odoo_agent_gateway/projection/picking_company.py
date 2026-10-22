"""The decision core of the ``picking_company_v1`` checked projection (ADR 0009 §5, §6).

Deliberately free of every Odoo import. This module decides *what the answer is* given facts that
have already been gathered under the calling identity; the model method in
``models/picking_company_projection.py`` does the ORM work and nothing else. Splitting them is not
decoration:

* the rules below are the security argument, and they are reviewable and executable without an Odoo
  installation, which is what lets the adversarial suite run them in CI;
* a rule that lives here cannot accidentally reach back into the ORM, pick up an environment, widen
  a record set or call ``sudo``, because there is nothing here to call.

## What this projection answers, and what it refuses to answer

``stock.picking.company_id`` is a **stored related** field on ``picking_type_id.company_id``
(``addons/stock/models/stock_picking.py:634`` at the pinned commit). A related field takes
``compute_sudo = related_sudo``, which defaults to true
(``odoo/orm/fields.py:456``), so Odoo resolved that hop to ``stock.picking.type`` **as superuser**.
The reading identity is never proven to see the operation type the company was taken from.

Per ADR 0009 §6 a projection may not forward that stored value. So the answer this module produces
is **re-derived** from ``stock.picking.type.company_id``, which at the same commit
(``addons/stock/models/stock_picking.py:130``) is an ordinary required stored column and not a
compute, read under the caller's own rights with the operation type's own record rule in force.

The stored value is still read, and used for exactly one thing: a tripwire. If the stored company
and the re-derived company disagree, this module answers ``company-conflicting`` and returns no
company at all. It is never returned, never used as a fallback, and never allowed to decide.

## Why divergence is a real condition rather than a theoretical one

``stock.picking``'s own multi-company record rule is
``[('company_id', 'in', company_ids)]`` (``addons/stock/security/stock_security.xml:73``) — it
filters on the elevated stored copy. If that copy ever goes stale against its operation type, the
rule itself is being enforced against a superuser-derived value. Re-deriving under the caller and
refusing on divergence is what turns that from an invisible assumption into a checked one.
"""

from __future__ import annotations

from typing import Final, Literal, NamedTuple

SCHEMA_VERSION: Final = 1
PROJECTION_NAME: Final = "picking_company_v1"

MAX_PICKING_IDS: Final = 200
"""Hard bound on one call. Not configurable, and not caller-controlled beyond staying under it.

A projection exists to answer a narrow question about records the caller already identified, so an
unbounded id list would turn it into an enumeration oracle with extra steps.
"""

Status = Literal[
    "verified",
    "picking-not-visible",
    "operation-type-missing",
    "operation-type-not-visible",
    "company-not-visible",
    "company-conflicting",
    "company-not-allowed",
]

STATUSES: Final[tuple[Status, ...]] = (
    "verified",
    "picking-not-visible",
    "operation-type-missing",
    "operation-type-not-visible",
    "company-not-visible",
    "company-conflicting",
    "company-not-allowed",
)
"""The closed answer vocabulary. No free text, no Odoo message, no exception string ever joins it.

``picking-not-visible`` deliberately conflates "no such picking" with "hidden from you". Separating
them would make the projection an existence oracle for rows the caller's record rules exclude, which
is a disclosure the elevated field it replaces does not make either.
"""


class PickingFacts(NamedTuple):
    """Everything gathered about one picking, already read under the calling identity.

    Every field is a plain Python value. ``None`` consistently means "the caller could not see it",
    never "it is absent from the database": the two are indistinguishable to a restricted reader by
    design, and both fail closed here.
    """

    picking_id: int
    picking_visible: bool
    operation_type_id: int | None
    """``None`` when the picking carries no operation type at all."""

    operation_type_visible: bool
    derived_company_id: int | None
    """``picking_type_id.company_id``, re-read as the caller. ``None`` when unreadable."""

    stored_company_id: int | None
    """The elevated stored ``stock.picking.company_id``. Tripwire only; never an answer."""

    company_visible: bool
    company_allowed: bool
    """Whether ``derived_company_id`` is in the caller's own allowed-company set."""


class PickingCompanyResult(NamedTuple):
    picking_id: int
    status: Status
    company_id: int | None

    def as_payload(self) -> dict[str, object]:
        """The wire form. ``False`` rather than ``None`` for an absent company, as Odoo renders."""

        return {
            "picking_id": self.picking_id,
            "status": self.status,
            "company_id": self.company_id if self.company_id is not None else False,
        }


class ProjectionInputError(ValueError):
    """The request itself is malformed or out of bounds. Carries a closed code, never an input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_picking_ids(raw: object) -> tuple[int, ...]:
    """Accept only an explicit, bounded, de-duplicated list of positive integers.

    Strict about types on purpose. ``True`` is an ``int`` in Python and would otherwise browse
    record 1, so booleans are rejected by exact type rather than by ``isinstance``. Order is
    preserved and duplicates collapse, so the response has exactly one entry per distinct id and a
    caller cannot inflate the bound by repeating one.
    """

    if not isinstance(raw, list | tuple):
        raise ProjectionInputError("picking-ids-not-a-list")
    if not raw:
        raise ProjectionInputError("picking-ids-empty")
    if len(raw) > MAX_PICKING_IDS:
        raise ProjectionInputError("picking-ids-over-bound")
    seen: dict[int, None] = {}
    for value in raw:
        if type(value) is not int or value <= 0:
            raise ProjectionInputError("picking-id-not-a-positive-integer")
        seen.setdefault(value, None)
    return tuple(seen)


def decide(facts: PickingFacts) -> PickingCompanyResult:
    """Resolve one picking to a company, or to the closed reason it could not be resolved.

    Ordered so that the least-disclosing refusal wins. Visibility of the picking is settled before
    anything about its operation type is considered, and the company is only ever returned at the
    very end, after every dependency has been proven under the caller's own rights.
    """

    def refuse(status: Status) -> PickingCompanyResult:
        return PickingCompanyResult(facts.picking_id, status, None)

    if not facts.picking_visible:
        return refuse("picking-not-visible")
    # `picking_type_id` is `required=True` upstream, so absence means a row that predates the
    # constraint, a broken migration or a deliberately crafted record. None of those is a company.
    if facts.operation_type_id is None:
        return refuse("operation-type-missing")
    if not facts.operation_type_visible:
        return refuse("operation-type-not-visible")
    if facts.derived_company_id is None or not facts.company_visible:
        return refuse("company-not-visible")
    # The tripwire. The stored value is elevated, so it cannot confirm anything on its own -- but a
    # disagreement between it and the value proven under the caller is not something to reconcile.
    if facts.stored_company_id is None or facts.stored_company_id != facts.derived_company_id:
        return refuse("company-conflicting")
    if not facts.company_allowed:
        return refuse("company-not-allowed")
    return PickingCompanyResult(facts.picking_id, "verified", facts.derived_company_id)


def build_response(results: tuple[PickingCompanyResult, ...]) -> dict[str, object]:
    """The fixed response envelope. Same keys, same order, whatever the outcome."""

    return {
        "schema_version": SCHEMA_VERSION,
        "projection": PROJECTION_NAME,
        "results": [result.as_payload() for result in results],
    }
