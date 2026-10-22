"""The decision core of the ``picking_state_v1`` checked projection (ADR 0009 §5, §6).

Free of every Odoo import, for the same reasons as ``picking_company`` and
``picking_scheduled_date``: the rules below are the security argument, and a rule that lives here
cannot reach back into the ORM, widen a record set or call ``sudo``.

## What Odoo computes, at the pinned commit -- the whole trace

``stock.picking.state`` (``addons/stock/models/stock_picking.py:575``) is a **stored compute** with
no explicit ``compute_sudo``, so it takes ``compute_sudo = store``, i.e. true
(``odoo/orm/fields.py:448``). ``_compute_state`` (``:816``), declared
``@api.depends('move_type', 'move_ids.state', 'move_ids.picking_id')``, does, per picking:

1. Search **every** ``stock.move`` of the picking (as superuser) and fold them into five flags:
   ``any_draft``, ``all_cancel``, ``all_cancel_done``, ``all_done_are_scrapped`` (every ``done``
   move's ``location_dest_usage`` is ``inventory``) and ``any_cancel_and_not_scrapped`` (some
   ``cancel`` move's ``location_dest_usage`` is not ``inventory``).
2. No move, or any draft move -> ``draft``.
3. Else every move cancelled -> ``cancel``.
4. Else every move cancelled or done -> ``cancel`` if all done moves are scrapped *and* some
   cancelled move is not, otherwise ``done``.
5. Else, if ``picking.location_id.should_bypass_reservation()`` -- the source location's ``usage``
   is one of ``supplier``, ``customer``, ``inventory``, ``production``
   (``stock_location.py:411``) -- **and** every move is ``make_to_stock``, -> ``assigned``.
6. Else ``stock.move._get_relevant_state_among_moves`` (``stock_move.py:1409``) over all the moves,
   with ``partially_available`` mapped to ``assigned``. That method:

   * keeps moves not ``cancel``/``done`` and not (``assigned`` with a zero demand);
   * sorts them by ``(rank, product_uom_qty)`` with ranks ``assigned`` 4, ``waiting`` 3,
     ``partially_available`` 2, ``confirmed`` 1, anything else 0;
   * none left -> ``assigned``;
   * policy ``one``: every demand zero -> ``assigned``; else the *first* move's state, with
     ``confirmed`` and ``partially_available`` both answering ``confirmed``;
   * otherwise: the first move is not ``assigned`` and some move is ``assigned`` or
     ``partially_available`` -> ``partially_available``; else the *last* move's state, except a
     ``confirmed`` last move with zero demand -> ``assigned``.

The provenance resolver could not follow ``_get_relevant_state_among_moves``, so the registry
records this field ``method_trace_complete=false``. The trace above completes it by hand at the
pinned commit, and the real-Odoo golden suite checks the port against Odoo's own method. Within the
reviewed module scope nothing overrides either method or ``should_bypass_reservation``. Across every
upstream addon's ``models/stock*.py`` and ``models/mrp*.py`` at the same commit, only ``mrp``
overrides ``_get_relevant_state_among_moves`` (for raw-material moves of a manufacturing order);
other files were not searched. A deployment with such an override can make Odoo's stored state
differ from this port, which the tripwire turns into a refusal, never into a wrong answer.

**Order does not matter.** Odoo sorts a record set built from a Python ``set`` of ids, so ties are
ordered arbitrarily. Every rank step 6 can see belongs to exactly one state (drafts were settled in
step 2, unrecognised states are refused), so tied moves share state and demand and are
indistinguishable to every read that follows. A test pins this by exhaustive permutation.

## The two location inputs are never read -- and why

Steps 4 and 5 consult a location, and **both location ids are elevated**:

* step 5 uses ``stock.picking.location_id`` (``stock_picking.py:609``), a stored compute whose
  ``_compute_location_id`` chooses between the operation type's default and the partner's
  company-dependent ``property_stock_supplier`` / ``property_stock_customer``, compared against
  ``ir.default.sudo()._get`` under ``with_company`` -- the ``#1`` removal, a derived channel for
  partner configuration. It is also ``readonly=False``, so a user may have set it by hand;
* step 4 uses ``stock.move.location_dest_usage``, a non-stored related over the stored
  ``stock.move.location_dest_id`` (``stock_move.py:81``), whose ``_compute_location_dest_id`` takes
  the picking's elevated destination, a rule's, or the operation type's, then substitutes
  ``location_final_id`` under warehouse and inter-company logic -- the ``#6`` removal, trace
  incomplete.

Proving the *target row* readable says nothing about how the elevated computation *chose* it, and
comparing with the stored state cannot help either, because Odoo computed the stored state from the
same elevated id. No approved caller-visible input determines either id: both are user-editable,
and every other column that names a location (``stock.move.location_id``, the operation type's
defaults) is itself an elevated compute. So neither is re-derived, neither is read, and the
projection answers **only when every possible usage of both locations yields the same state**.

### The proof, branch by branch

Steps 1-3 and 6 read no location. Only steps 4 and 5 do, and each reads a location through exactly
one predicate:

* **Step 4** reads each closed move's destination only through ``usage == 'inventory'``. It is
  reached only when every move is ``done`` or ``cancel`` and at least one is ``done`` (step 3 took
  the all-``cancel`` case).

  - *No ``cancel`` move.* ``any_cancel_and_not_scrapped`` is an ``any`` over nothing, so false, so
    the answer is ``done`` **for every destination**. Answered.
  - *At least one ``done`` and one ``cancel`` move.* Scrapping every ``done`` destination and no
    ``cancel`` destination gives ``cancel``; leaving any ``done`` destination unscrapped gives
    ``done``. Both are realisable, because each move's destination is a separate elevated id with
    no visible constraint on its usage. **Refused: ``destination-location-dependent``.**

* **Step 5** reads the source only through membership of its usage in the bypass set, which has
  members (``supplier``) and non-members (``internal``) among the declared usages. Let ``r`` be step
  6's answer, computed without any location.

  - *Some move is ``make_to_order``.* The bypass condition is false **for every source**, so the
    answer is ``r``. Answered.
  - *Every move is ``make_to_stock`` and ``r`` is ``assigned``.* Bypass gives ``assigned`` and no
    bypass gives ``r`` = ``assigned``. Answered, **for every source**.
  - *Every move is ``make_to_stock`` and ``r`` is not ``assigned``.* A bypassing source gives
    ``assigned``, a non-bypassing one gives ``r``. **Refused: ``source-location-dependent``.**

Every ``verified`` answer is therefore the value Odoo computes for *every* assignment of usages to
the two elevated locations, and every location-dependent refusal is a case where two assignments
disagree. The unit suite checks this exhaustively against a transliteration of Odoo's full method
that *does* take the usages as parameters; the real-Odoo suite checks it with identical visible
move inputs under different source and destination provenance -- partner properties, a hidden
``ir.default``, and a route's final location.

**The cost is real.** An open, all-``make_to_stock`` picking whose moves are not yet reserved --
most ``waiting`` and ``confirmed`` deliveries -- is refused, because whether Odoo calls it
``assigned`` depends on its source. Both possible answers are open states, and both refused step 4
answers are closed states; a coarser "open / closed" answer would therefore be location-independent
in every case, but that is a different contract and is not implemented here.

## The tripwire, and exactly what it proves

The same argument as ``picking_scheduled_date``, generalised. A restricted identity cannot count
the moves its record rules hide. The projection recomputes the state from the moves it *can* read
and compares the result with the stored value, which Odoo computed over every move:

* if a hidden move would change the state, the two differ and the answer is ``state-conflicting``;
* if they agree, the returned state is both recomputed from visible inputs and equal to Odoo's
  value, so any hidden move did not affect it. Such a move is undetectable and changes nothing.

The tripwire proves nothing about the locations, which is exactly why they are excluded above
rather than left to it. ``_compute_state`` also reads ``product_uom_qty`` and ``procure_method``,
neither of which is in its ``@api.depends``, so a change to one without a move state change leaves
the stored state stale **by Odoo's own design**; the projection then refuses with
``state-conflicting`` rather than choose. The frequency is unmeasured.
"""

from __future__ import annotations

import math
from typing import Final, Literal, NamedTuple

from .picking_scheduled_date import CLOSED_MOVE_STATES, MOVE_STATES, MOVE_TYPES

SCHEMA_VERSION: Final = 1
PROJECTION_NAME: Final = "picking_state_v1"

PICKING_STATES: Final = frozenset({"draft", "waiting", "confirmed", "assigned", "done", "cancel"})
"""``stock.picking.state``'s declared selection (``stock_picking.py:575``)."""

PROCURE_METHODS: Final = frozenset({"make_to_stock", "make_to_order"})
"""``stock.move.procure_method``'s declared selection (``stock_move.py:131``)."""

LOCATION_USAGES: Final = frozenset(
    {"supplier", "view", "internal", "customer", "inventory", "production", "transit"}
)
"""``stock.location.usage``'s declared selection (``stock_location.py:32``).

Never read. It is the domain the proof above ranges over, and the exhaustive unit test enumerates
it.
"""

BYPASS_RESERVATION_USAGES: Final = frozenset({"supplier", "customer", "inventory", "production"})
"""``stock.location.should_bypass_reservation`` (``stock_location.py:411``). Never read."""

SCRAP_USAGE: Final = "inventory"
"""The one usage step 4 tests for. Never read."""

RELEVANCE_RANK: Final = {"assigned": 4, "waiting": 3, "partially_available": 2, "confirmed": 1}
"""``_get_relevant_state_among_moves``'s ``sort_map`` (``stock_move.py:1420``); others rank 0."""

Status = Literal[
    "verified",
    "picking-not-visible",
    "operation-type-missing",
    "operation-type-not-visible",
    "move-type-unresolved",
    "moves-not-readable",
    "moves-over-bound",
    "move-state-unrecognised",
    "move-input-unrecognised",
    "source-location-dependent",
    "destination-location-dependent",
    "state-conflicting",
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
    "move-input-unrecognised",
    "source-location-dependent",
    "destination-location-dependent",
    "state-conflicting",
)
"""The closed answer vocabulary. ``picking-not-visible`` conflates absent and hidden."""


class MoveStateFacts(NamedTuple):
    """One visible move's inputs, exactly as read under the caller. No location, by design."""

    state: object
    product_uom_qty: object
    procure_method: object


class StateFacts(NamedTuple):
    """Everything gathered about one picking, already read under the calling identity."""

    picking_id: int
    picking_visible: bool
    operation_type_id: int | None
    operation_type_visible: bool
    move_type: object
    stored_state: object
    """The elevated stored ``state``. Tripwire only; never an answer."""

    moves_readable: bool
    moves_truncated: bool
    moves: tuple[MoveStateFacts, ...]


class StateResult(NamedTuple):
    picking_id: int
    status: Status
    state: str | None

    def as_payload(self) -> dict[str, object]:
        return {
            "picking_id": self.picking_id,
            "status": self.status,
            "state": self.state if self.state is not None else False,
        }


class LocationDependent(Exception):
    """The state depends on a location this projection may not trust. Carries the closed status."""

    def __init__(self, status: Status) -> None:
        super().__init__(status)
        self.status = status


def _is_quantity(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]


def relevant_state_among_moves(moves: tuple[MoveStateFacts, ...], move_type: str) -> str:
    """A port of ``stock.move._get_relevant_state_among_moves`` (``stock_move.py:1409``).

    Called only with recognised states and numeric demands, and only for one picking, so Odoo's
    ``moves_todo[:1].picking_id`` guard is always true and its ``.move_type`` is ``move_type``.
    """

    todo = sorted(
        (
            move
            for move in moves
            if move.state not in CLOSED_MOVE_STATES
            and not (move.state == "assigned" and not move.product_uom_qty)
        ),
        key=lambda move: (RELEVANCE_RANK.get(move.state, 0), move.product_uom_qty),  # type: ignore[arg-type]
    )
    if not todo:
        return "assigned"
    if move_type == "one":
        if all(not move.product_uom_qty for move in todo):
            return "assigned"
        most_important = todo[0]
        if most_important.state in ("confirmed", "partially_available"):
            return "confirmed"
        return str(most_important.state) or "draft"
    if todo[0].state != "assigned" and any(
        move.state in ("assigned", "partially_available") for move in todo
    ):
        return "partially_available"
    least_important = todo[-1]
    if least_important.state == "confirmed" and least_important.product_uom_qty == 0:
        return "assigned"
    return str(least_important.state) or "draft"


def compute_state(moves: tuple[MoveStateFacts, ...], move_type: str) -> str:
    """``stock.picking._compute_state`` (``stock_picking.py:816``) over visible moves, answered
    only where no location can change it.

    Raises :class:`LocationDependent` exactly where the proof in the module docstring says two
    location usages give different answers. Takes no location argument, so it cannot read one.
    """

    states = [move.state for move in moves]
    if not moves or "draft" in states:
        return "draft"
    if all(state == "cancel" for state in states):
        return "cancel"
    if all(state in CLOSED_MOVE_STATES for state in states):
        # At least one move is done here. With no cancel move, `any_cancel_and_not_scrapped` is
        # false whatever the destinations are; with one, the destinations decide.
        if "cancel" not in states:
            return "done"
        raise LocationDependent("destination-location-dependent")
    relevant = relevant_state_among_moves(moves, move_type)
    without_bypass = "assigned" if relevant == "partially_available" else relevant
    if any(move.procure_method != "make_to_stock" for move in moves):
        return without_bypass
    if without_bypass == "assigned":
        return "assigned"
    raise LocationDependent("source-location-dependent")


def decide(facts: StateFacts) -> StateResult:
    """Recompute one picking's state from visible inputs, or give the closed reason it cannot."""

    def refuse(status: Status) -> StateResult:
        return StateResult(facts.picking_id, status, None)

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
    if any(
        not _is_quantity(move.product_uom_qty)
        or not isinstance(move.procure_method, str)
        or move.procure_method not in PROCURE_METHODS
        for move in facts.moves
    ):
        return refuse("move-input-unrecognised")

    try:
        recomputed = compute_state(facts.moves, facts.move_type)
    except LocationDependent as dependency:
        return refuse(dependency.status)

    # The tripwire. A disagreement means a hidden move changes the answer, the stored value is
    # stale, or the deployment computes the state differently. None of those is reconcilable here.
    if not isinstance(facts.stored_state, str) or facts.stored_state != recomputed:
        return refuse("state-conflicting")
    return StateResult(facts.picking_id, "verified", recomputed)


def build_response(results: tuple[StateResult, ...]) -> dict[str, object]:
    """The fixed response envelope. Same keys, same order, whatever the outcome."""

    return {
        "schema_version": SCHEMA_VERSION,
        "projection": PROJECTION_NAME,
        "results": [result.as_payload() for result in results],
    }
