"""The decision core of the ``quant_company_v1`` checked projection (ADR 0009 §5, §6).

Free of every Odoo import. The model method in ``models/quant_company_projection.py`` gathers
facts under the calling identity; this module decides the answer and builds the one response.

## What this projection answers

``stock.quant.company_id`` is a **stored related** field on ``location_id.company_id``
(``addons/stock/models/stock_quant.py:56`` in the pinned image). A related field takes
``compute_sudo = related_sudo``, true by default (``odoo/orm/fields.py``), so Odoo resolved the hop
to ``stock.location`` **as superuser**; and the provenance registry could not resolve the related
field's comodel (``unresolved-relational-comodel``). Per ADR 0009 §6 that stored value is never an
answer here.

The answer is **re-derived** from ``stock.location.company_id`` -- an ordinary, optional, stored
``Many2one('res.company')`` (``addons/stock/models/stock_location.py:60``) with an explicit comodel,
so the returned id is typed by construction -- read as the caller, with the location's own record
rule in force, after proving the quant row, the quant's ``location_id`` field, the location row, the
location's ``company_id`` field and, when there is one, the company row readable.

``False`` is a real answer: a location with no company is shared between companies
(``stock.location.company_id``'s help text), and so is every quant in it.

## The stored copy is a tripwire, never an authority

The stored ``stock.quant.company_id`` is read, as the caller, for one purpose: if it disagrees with
the re-derived company, the answer is ``company-conflicting`` and no company is returned. Odoo
forbids changing a location's company through the ORM (``stock.location.write``,
``stock_location.py:222-225``), so a disagreement means an out-of-ORM write -- exactly the case a
stored elevated copy cannot be trusted in.

## What cannot be proven here

``stock.quant``'s own multi-company rule is ``[('company_id', 'in', company_ids + [False])]``
(``addons/stock/security/stock_security.xml:122``): it filters on the **elevated stored copy**. A
stale copy can therefore hide a quant from the company it really belongs to, or show a quant whose
location the caller cannot read. The first is invisible to every caller-rights read, this one
included: the quant is refused as ``quant-not-visible`` and nothing reveals why. The second is
refused as ``location-not-visible``. The integration suite pins both.
"""

from __future__ import annotations

from typing import Final, Literal, NamedTuple

SCHEMA_VERSION: Final = 1
PROJECTION_NAME: Final = "quant_company_v1"

MAX_QUANT_IDS: Final = 200
"""Hard bound on one call, the same as the other projections'."""

Status = Literal[
    "verified",
    "quant-not-visible",
    "field-not-readable",
    "location-missing",
    "location-not-visible",
    "company-not-visible",
    "company-conflicting",
    "company-not-allowed",
]

STATUSES: Final[tuple[Status, ...]] = (
    "verified",
    "quant-not-visible",
    "field-not-readable",
    "location-missing",
    "location-not-visible",
    "company-not-visible",
    "company-conflicting",
    "company-not-allowed",
)
"""The closed answer vocabulary, in check order.

``quant-not-visible`` conflates "no such quant" with "hidden from you", and ``field-not-readable``
does not name the field, so the projection is not an existence or schema oracle.
"""


class QuantFacts(NamedTuple):
    """Everything gathered about one quant, under the calling identity.

    ``None`` for a company means "no company" only when the field it came from was proven readable;
    otherwise the ``*_readable`` flag is false and the value is never consulted.
    """

    quant_id: int
    quant_visible: bool
    fields_readable: bool
    """``stock.quant.location_id``, ``stock.quant.company_id`` and ``stock.location.company_id``."""

    location_id: int | None
    location_visible: bool
    derived_company_id: int | None
    """``location_id.company_id`` re-read as the caller; ``None`` for a shared location."""

    stored_company_id: int | None
    """The elevated stored ``stock.quant.company_id``. Tripwire only."""

    company_visible: bool
    company_allowed: bool


class QuantCompanyResult(NamedTuple):
    quant_id: int
    status: Status
    company_id: int | None = None

    def as_payload(self) -> dict[str, object]:
        """``company_id`` is an integer, or ``False`` -- "shared" on a verified quant, nothing on a
        refused one. The status is always read first."""

        return {
            "quant_id": self.quant_id,
            "status": self.status,
            "company_id": self.company_id
            if self.status == "verified" and self.company_id is not None
            else False,
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


def decide(facts: QuantFacts) -> QuantCompanyResult:
    """One quant's answer. The least-disclosing refusal wins; the company only at the very end."""

    def refuse(status: Status) -> QuantCompanyResult:
        return QuantCompanyResult(facts.quant_id, status)

    if not facts.quant_visible:
        return refuse("quant-not-visible")
    if not facts.fields_readable:
        return refuse("field-not-readable")
    # `location_id` is required with ondelete restrict; absence is a broken row, not a quant.
    if facts.location_id is None:
        return refuse("location-missing")
    if not facts.location_visible:
        return refuse("location-not-visible")
    derived = facts.derived_company_id
    if derived is not None and not facts.company_visible:
        return refuse("company-not-visible")
    # The tripwire: the elevated stored copy must agree with the value proven under the caller,
    # including agreeing that a shared location has no company.
    if facts.stored_company_id != derived:
        return refuse("company-conflicting")
    if derived is not None and not facts.company_allowed:
        return refuse("company-not-allowed")
    return QuantCompanyResult(facts.quant_id, "verified", derived)


def build_response(results: tuple[QuantCompanyResult, ...]) -> dict[str, object]:
    """The fixed envelope. Same keys, same order, whatever the outcome."""

    return {
        "schema_version": SCHEMA_VERSION,
        "projection": PROJECTION_NAME,
        "results": [result.as_payload() for result in results],
    }
