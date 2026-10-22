"""The decision core of the ``picking_scheduled_date_v1`` checked projection (ADR 0009 §5, §6).

Free of every Odoo import, for the same reasons as ``picking_company``: the rules below are the
security argument, reviewable and executable without an Odoo installation, and a rule that lives
here cannot reach back into the ORM, widen a record set or call ``sudo``.

## What Odoo computes, at the pinned commit

``stock.picking.scheduled_date`` (``addons/stock/models/stock_picking.py:595``) is a **stored
compute** with no explicit ``compute_sudo``, so it takes ``compute_sudo = store``, i.e. true
(``odoo/orm/fields.py:448``). Its method, ``_compute_scheduled_date`` (``:865``), is::

    @api.depends('move_ids.state', 'move_ids.date', 'move_type')
    def _compute_scheduled_date(self):
        for picking in self:
            if not picking.id:
                continue
            moves_dates = picking.move_ids.filtered(
                lambda move: move.state not in ('done', 'cancel')).mapped('date')
            if picking.move_type == 'direct':
                picking.scheduled_date = min(moves_dates, default=picking.scheduled_date or now())
            else:
                picking.scheduled_date = max(moves_dates, default=picking.scheduled_date or now())

So under superuser Odoo aggregates over **every** move of the picking, including moves the reading
identity's record rules hide, and when no open move exists it keeps **its own previous value**.

## What this module answers instead

The same min/max, over the ``stock.move.state`` and ``stock.move.date`` of the moves the **caller**
can read -- both plain stored columns (``stock_move.py:28,107``) -- selected by the picking's
``move_type``. It refuses, with a closed status and no date, whenever that recomputation cannot be
trusted to be the value Odoo would compute:

* **No open move is visible.** Odoo would fall back to the stored value itself. Forwarding it is
  exactly what ADR 0009 §6 forbids, so there is nothing to recompute from and the answer is
  ``no-open-moves`` rather than a date.
* **An input is unreadable, unrecognised or incomplete** -- the move set could not be read, it
  exceeded the per-picking bound, a move's state is outside Odoo's vocabulary, or an open move has
  no date. Each fails closed with its own status.
* **The recomputation diverges from the stored value.** The stored value is read for exactly one
  purpose: a tripwire. It is never returned, never a fallback, never allowed to decide.

## What the tripwire does and does not prove -- stated exactly

A restricted identity cannot count rows its record rules hide; only an elevated read could, and this
projection makes none. The completeness argument is therefore about *contribution*, not presence:

* ``min`` (or ``max``) over a subset can only be later (or earlier) than over the whole set. If the
  value over the visible moves **equals** the stored full-set value, the extremum is attained by a
  move the caller can read. The returned date is then witnessed by a visible input and is the value
  Odoo computed.
* If a hidden move **is** the extremum, the two values differ and the answer is
  ``scheduled-date-conflicting``. A hidden move that would change the answer therefore always fails
  closed.
* A hidden move that is **not** the extremum is undetectable without elevation, and by construction
  does not change the answer. That is a statement of the limit, not a claim that it cannot occur.
* The residual risk is a stale stored value that coincides with the visible recomputation while a
  hidden extremal move exists. That requires both a stale column and a coincidence of timestamps
  to the second; it is recorded as a known limit rather than argued away.

``move_type`` is an input, never an output. It is itself a stored compute whose only dependency is
``picking_type_id.move_type`` (``stock_picking.py:571``, ``_compute_move_type`` at ``:720``), and it
is ``readonly=False``, so a user may legitimately set it per picking and it cannot be re-derived
from the operation type. The model method instead proves the operation type is readable by the
caller, so the one hop the elevated compute takes reaches nothing the caller cannot see, and this
module accepts only the two values Odoo declares.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal, NamedTuple

SCHEMA_VERSION: Final = 1
PROJECTION_NAME: Final = "picking_scheduled_date_v1"

MAX_MOVES_PER_PICKING: Final = 500
"""Hard bound on the moves read for one picking. Exceeding it refuses that picking.

A truncated move set cannot be min/max'd honestly: the missing moves might hold the extremum. The
model method reads ``bound + 1`` so it can tell "exactly at the bound" from "more than the bound".
"""

MOVE_TYPES: Final = frozenset({"direct", "one"})
"""``stock.picking.move_type``'s declared selection (``stock_picking.py:571``)."""

MOVE_STATES: Final = frozenset(
    {"draft", "waiting", "confirmed", "partially_available", "assigned", "done", "cancel"}
)
"""``stock.move.state``'s declared selection (``stock_move.py:107``).

Odoo's compute treats *anything* outside ``{done, cancel}`` as open, so a state added by a module
outside the reviewed scope would silently join the aggregation. Refusing an unrecognised state keeps
that a visible deployment divergence rather than an unreviewed input.
"""

CLOSED_MOVE_STATES: Final = frozenset({"done", "cancel"})

ODOO_DATETIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
"""Odoo's wire format for a naive-UTC ``Datetime`` (``DEFAULT_SERVER_DATETIME_FORMAT``).

Rendered here rather than left to the transport's JSON encoder, so the response is plain JSON types
only and its format is pinned by this module's tests.
"""

Status = Literal[
    "verified",
    "picking-not-visible",
    "operation-type-missing",
    "operation-type-not-visible",
    "move-type-unresolved",
    "moves-not-readable",
    "moves-over-bound",
    "move-state-unrecognised",
    "move-date-missing",
    "no-open-moves",
    "scheduled-date-conflicting",
]

STATUSES: Final[tuple[Status, ...]] = (
    "verified",
    "picking-not-visible",
    "operation-type-missing",
    "operation-type-not-visible",
    "move-type-unresolved",
    "moves-not-readable",
    "moves-over-bound",
    "move-state-unrecognised",
    "move-date-missing",
    "no-open-moves",
    "scheduled-date-conflicting",
)
"""The closed answer vocabulary. No free text, no Odoo message, no exception string ever joins it.

As in ``picking_company``, ``picking-not-visible`` deliberately conflates "no such picking" with
"hidden from you", so the projection is not an existence oracle.
"""


class MoveFacts(NamedTuple):
    """One visible move's two inputs, exactly as read under the caller.

    Typed ``object`` on purpose: this module validates them rather than trusting the ORM's types.
    """

    state: object
    date: object


class ScheduledDateFacts(NamedTuple):
    """Everything gathered about one picking, already read under the calling identity.

    ``None`` consistently means "the caller could not read it", never "absent from the database".
    """

    picking_id: int
    picking_visible: bool
    operation_type_id: int | None
    operation_type_visible: bool
    move_type: object
    """The stored ``move_type``. An input selecting min or max; never returned."""

    stored_scheduled_date: object
    """The elevated stored ``scheduled_date``. Tripwire only; never an answer."""

    moves_readable: bool
    moves_truncated: bool
    moves: tuple[MoveFacts, ...]
    """The caller-visible moves of this picking, in ``id`` order."""


class ScheduledDateResult(NamedTuple):
    picking_id: int
    status: Status
    scheduled_date: datetime | None

    def as_payload(self) -> dict[str, object]:
        """The wire form. ``False`` rather than ``None`` for an absent date, as Odoo renders."""

        return {
            "picking_id": self.picking_id,
            "status": self.status,
            "scheduled_date": (
                self.scheduled_date.strftime(ODOO_DATETIME_FORMAT)
                if self.scheduled_date is not None
                else False
            ),
        }


def _is_naive_datetime(value: object) -> bool:
    """Odoo stores ``Datetime`` as naive UTC. Anything else is not a value this module compares."""

    return isinstance(value, datetime) and value.tzinfo is None


def decide(facts: ScheduledDateFacts) -> ScheduledDateResult:
    """Recompute one picking's scheduled date from visible inputs, or give the closed reason not.

    Ordered so the least-disclosing refusal wins: visibility of the picking first, then the
    operation type that ``move_type`` was elevated through, then the moves, and the date only at
    the very end, after the tripwire.
    """

    def refuse(status: Status) -> ScheduledDateResult:
        return ScheduledDateResult(facts.picking_id, status, None)

    if not facts.picking_visible:
        return refuse("picking-not-visible")
    if facts.operation_type_id is None:
        return refuse("operation-type-missing")
    if not facts.operation_type_visible:
        return refuse("operation-type-not-visible")
    if not isinstance(facts.move_type, str) or facts.move_type not in MOVE_TYPES:
        return refuse("move-type-unresolved")
    if not facts.moves_readable:
        return refuse("moves-not-readable")
    if facts.moves_truncated:
        return refuse("moves-over-bound")
    if any(
        not isinstance(move.state, str) or move.state not in MOVE_STATES for move in facts.moves
    ):
        return refuse("move-state-unrecognised")

    open_dates = []
    for move in facts.moves:
        if move.state in CLOSED_MOVE_STATES:
            continue
        # `stock.move.date` is `required=True` upstream, so an open move without one is a broken
        # row, not a move to skip: skipping it could drop the extremum.
        if not _is_naive_datetime(move.date):
            return refuse("move-date-missing")
        open_dates.append(move.date)

    # Odoo's fallback here is the stored value itself. That is the one thing §6 forbids forwarding.
    if not open_dates:
        return refuse("no-open-moves")

    recomputed = min(open_dates) if facts.move_type == "direct" else max(open_dates)

    # The tripwire. Elevated, so it confirms nothing on its own -- but disagreement means either a
    # hidden move holds the extremum or the stored column is stale, and neither is reconcilable.
    if (
        not _is_naive_datetime(facts.stored_scheduled_date)
        or facts.stored_scheduled_date != recomputed
    ):
        return refuse("scheduled-date-conflicting")
    return ScheduledDateResult(facts.picking_id, "verified", recomputed)


def build_response(results: tuple[ScheduledDateResult, ...]) -> dict[str, object]:
    """The fixed response envelope. Same keys, same order, whatever the outcome."""

    return {
        "schema_version": SCHEMA_VERSION,
        "projection": PROJECTION_NAME,
        "results": [result.as_payload() for result in results],
    }
