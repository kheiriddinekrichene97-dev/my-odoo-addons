"""The decision core of the ``picking_window_v1`` bounded search projection (ADR 0009 §5, §6).

Free of every Odoo import. The model method in ``models/picking_window_projection.py`` discovers
candidates and runs the three checked picking projections under the caller; this module validates
the request, classifies each candidate from those three answers, and builds the one response.

## The exact question this answers -- and the one it does not

It **does not** answer "which pickings match, according to Odoo's own recomputation over every
move". That question cannot be answered with the caller's rights. The counterexample is concrete
and is pinned by a real-Odoo test: a picking whose stored ``scheduled_date`` was made stale *outside
the ORM* and lies outside the window, and whose in-window determining move is *hidden* from the
caller. Odoo's fresh recomputation puts it in the window; nothing the caller may read does. No
search the caller can run finds it, and nothing the caller can read reveals that it was missed.

It **does** answer, with a proof, this narrower question -- named in every response as
:data:`COVERAGE`:

    Among the pickings the caller can read whose operation type is requested, every picking that
    has **a stored scheduled date inside the window**, or **a caller-visible open, non-draft move
    dated inside the window**, is examined; each one is listed as ``included`` (proven to satisfy
    the request under the caller) or ``refused`` (not provable either way), or counted as
    ``excluded`` (proven not to).

Two consequences are stated in every response, not only here:

* ``status`` is ``covered``, never "complete". The stored-field domain the picking pack sends today
  is a subset of what is covered (it adds a company and a state clause to the stored-date window),
  so the guarantee is **at least** as strong as today's -- and it additionally examines pickings a
  stale stored value would have hidden, as long as a visible move witnesses them.
* ``absence_proven`` is ``False``, always. An empty ``covered`` answer means "no picking the caller
  can read has a stored date or a visible open move in this window", which is a statement about
  stored and visible data. It is **not** an all-clear about the business, and a consumer must not
  treat it as one.

## Candidate discovery, and why it covers exactly the named question

Two searches, both **as the caller**, both bounded with a ``bound + 1`` probe:

* **A, witnessed by visible moves.** ``stock.move`` in ``waiting``, ``confirmed``,
  ``partially_available`` or ``assigned`` whose plain ``date`` lies in the window and whose
  picking's plain ``picking_type_id`` is requested. No company clause: the requested types are
  proven to belong to the requested company up front, and a move of another company is either
  visible (then it is a witness) or hidden by ``stock.move``'s own rule.
* **B, witnessed by the stored date.** ``stock.picking`` of a requested type whose stored
  ``scheduled_date`` lies in the window. The elevated value is used only to *find* candidates, and
  is never returned. No clause on the stored ``state`` (stale by Odoo's own design) or
  ``company_id`` (implied by the validated types).

A picking in the named question is, by definition, in A (its visible open non-draft move) or in B
(its stored date). And a picking that is **not** in the named question cannot be ``included``:
inclusion needs a verified open state, so no visible move is a draft, and a verified date ``d`` in
the window, which ``picking_scheduled_date_v1`` verifies only as the min or max over visible moves
that are not ``done``/``cancel`` -- a visible, open, non-draft move dated ``d``, i.e. A. So nothing
provable is lost by covering exactly this set, and nothing outside it is claimed.

If either search, or their union, exceeds its bound, the **whole request is refused**. A partial
candidate set would make even the narrow guarantee false.

## One call, one snapshot -- checked, not assumed

Candidate discovery is two searches and verification is three more projections: five reads at
least. Under PostgreSQL's ``READ COMMITTED`` each *statement* takes a fresh snapshot, so a change
committed by another connection between search A, search B and the verification would be seen by
some reads and not others. The answer would then combine two database states, and could miss a
covered picking outright (both measured on real Odoo 19 in the integration suite). "One call" and
"one transaction" are therefore **not** by themselves a single snapshot.

What makes it one is the isolation level, and the method checks it instead of inferring it:

* **Before any business read**, the transaction's isolation level must be one of
  :data:`SNAPSHOT_ISOLATION_LEVELS`. Under ``REPEATABLE READ`` (what every Odoo 19 cursor sets,
  ``odoo/sql_db.py:373``) and ``SERIALIZABLE`` all statements of a transaction see the snapshot
  taken at its first statement. Anything else refuses the whole request with
  ``snapshot-not-established`` before a single row is read.
* **After the last read**, ``pg_current_snapshot()`` must equal its value before the first. A
  commit or rollback anywhere inside the call starts a new transaction with a new snapshot even at
  the right level; that refuses the whole request with ``snapshot-changed``.

Both are whole-request refusals like every other: no coverage, no totals, no rows.

Rows (``included`` and ``refused``) are ordered by picking id. The caller states ``max_rows``, the
most rows it will accept; if more would be listed, the **whole request is refused** with no rows.
There is no cursor and no continuation: independent calls are independent snapshots, and nothing
in the contract invites concatenating them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final, Literal, NamedTuple

SCHEMA_VERSION: Final = 1
PROJECTION_NAME: Final = "picking_window_v1"

COVERAGE: Final = "stored-scheduled-date-or-visible-open-move-in-window"
"""The name of the one question every ``covered`` response answers. See the module docstring."""

OPEN_PICKING_STATES: Final = ("waiting", "confirmed", "assigned")
"""The only states a request may ask for. A picking with no open move has no recomputable
scheduled date (``picking_scheduled_date_v1`` answers ``no-open-moves``), so ``draft``, ``done``
and ``cancel`` are outside what this projection can answer."""

WITNESS_MOVE_STATES: Final = ("waiting", "confirmed", "partially_available", "assigned")
"""Set A's move states: open and not draft."""

MAX_PICKING_TYPE_IDS: Final = 50
"""The existing trusted-scope ceiling (``MAX_REQUESTED_PICKING_TYPE_IDS`` in the control plane)."""

MAX_WINDOW: Final = timedelta(days=366)
MAX_WITNESS_MOVES: Final = 2000
"""Set A's work bound, in moves. Exceeding it refuses the whole request."""

MAX_STORED_CANDIDATES: Final = 200
"""Set B's work bound, in pickings. Exceeding it refuses the whole request."""

MAX_CANDIDATES: Final = 200
"""The union's bound. Equal to the checked projections' own id bound, so one call of each verifies
every candidate in the same transaction."""

MAX_ROWS: Final = MAX_CANDIDATES
"""The response bound a caller may ask for. Listed rows never exceed candidates, so the largest
answer this projection can produce fits in one response."""

ODOO_DATETIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

SNAPSHOT_ISOLATION_LEVELS: Final = ("repeatable read", "serializable")
"""PostgreSQL ``transaction_isolation`` values under which every statement of a transaction reads
the one snapshot taken at its first statement. ``read committed`` (a snapshot per statement) and
``read uncommitted`` (the same as ``read committed`` in PostgreSQL) are refused."""

RequestStatus = Literal[
    "covered",
    "snapshot-not-established",
    "snapshot-changed",
    "company-not-allowed",
    "operation-type-scope-invalid",
    "witness-moves-not-readable",
    "witness-moves-over-bound",
    "stored-candidates-over-bound",
    "candidates-over-bound",
    "rows-over-limit",
]

REQUEST_STATUSES: Final[tuple[RequestStatus, ...]] = (
    "covered",
    "snapshot-not-established",
    "snapshot-changed",
    "company-not-allowed",
    "operation-type-scope-invalid",
    "witness-moves-not-readable",
    "witness-moves-over-bound",
    "stored-candidates-over-bound",
    "candidates-over-bound",
    "rows-over-limit",
)
"""Whole-request outcomes. Only ``covered`` carries rows and totals; every other value carries
neither, so a refusal can never be read as an empty answer."""

Disposition = Literal["included", "excluded", "refused"]


class WindowInputError(ValueError):
    """The request is malformed. Carries a closed code, never an input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WindowRequest(NamedTuple):
    company_id: int
    picking_type_ids: tuple[int, ...]
    states: tuple[str, ...]
    scheduled_from: datetime
    scheduled_to: datetime
    max_rows: int

    @property
    def window_strings(self) -> tuple[str, str]:
        return (
            self.scheduled_from.strftime(ODOO_DATETIME_FORMAT),
            self.scheduled_to.strftime(ODOO_DATETIME_FORMAT),
        )


def _positive_int(value: object, code: str) -> int:
    if type(value) is not int or value < 1:
        raise WindowInputError(code)
    return value


def _odoo_datetime(value: object, code: str) -> datetime:
    """Exactly Odoo's naive-UTC wire format, and nothing looser."""

    if type(value) is not str or len(value) != 19:
        raise WindowInputError(code)
    try:
        parsed = datetime.strptime(value, ODOO_DATETIME_FORMAT)
    except ValueError:
        raise WindowInputError(code) from None
    if parsed.strftime(ODOO_DATETIME_FORMAT) != value:
        raise WindowInputError(code)
    return parsed


def validate_request(
    *,
    company_id: object,
    picking_type_ids: object,
    states: object,
    scheduled_from: object,
    scheduled_to: object,
    max_rows: object,
) -> WindowRequest:
    """The whole argument grammar. Types are checked exactly, so ``True`` is not an id."""

    company = _positive_int(company_id, "company-id-not-a-positive-integer")

    if not isinstance(picking_type_ids, list | tuple) or not picking_type_ids:
        raise WindowInputError("picking-type-ids-not-a-non-empty-list")
    if len(picking_type_ids) > MAX_PICKING_TYPE_IDS:
        raise WindowInputError("picking-type-ids-over-bound")
    types: dict[int, None] = {}
    for value in picking_type_ids:
        types.setdefault(_positive_int(value, "picking-type-id-not-a-positive-integer"), None)

    if not isinstance(states, list | tuple) or not states:
        raise WindowInputError("states-not-a-non-empty-list")
    chosen: dict[str, None] = {}
    for value in states:
        if type(value) is not str or value not in OPEN_PICKING_STATES:
            raise WindowInputError("state-not-an-open-picking-state")
        chosen.setdefault(value, None)

    start = _odoo_datetime(scheduled_from, "scheduled-from-not-an-odoo-datetime")
    end = _odoo_datetime(scheduled_to, "scheduled-to-not-an-odoo-datetime")
    if end < start:
        raise WindowInputError("window-reversed")
    if end - start > MAX_WINDOW:
        raise WindowInputError("window-over-bound")

    rows = _positive_int(max_rows, "max-rows-not-a-positive-integer")
    if rows > MAX_ROWS:
        raise WindowInputError("max-rows-over-bound")

    return WindowRequest(
        company_id=company,
        picking_type_ids=tuple(sorted(types)),
        states=tuple(state for state in OPEN_PICKING_STATES if state in chosen),
        scheduled_from=start,
        scheduled_to=end,
        max_rows=rows,
    )


def snapshot_established(isolation: object) -> bool:
    """Whether a transaction at this isolation level reads one snapshot for all its statements."""

    return type(isolation) is str and isolation in SNAPSHOT_ISOLATION_LEVELS


class CandidateAnswers(NamedTuple):
    """The three checked projections' rows for one candidate, as they returned them."""

    picking_id: int
    company_status: str
    company_id: object
    state_status: str
    state: object
    date_status: str
    scheduled_date: object


class Classified(NamedTuple):
    picking_id: int
    disposition: Disposition
    answers: CandidateAnswers


def classify(answers: CandidateAnswers, request: WindowRequest) -> Classified:
    """One candidate's disposition. A proven mismatch outranks a refusal; a refusal outranks
    inclusion; inclusion needs all three verified and all three satisfied."""

    def verdict(disposition: Disposition) -> Classified:
        return Classified(answers.picking_id, disposition, answers)

    checks = []
    if answers.company_status == "verified":
        checks.append(answers.company_id == request.company_id)
    if answers.state_status == "verified":
        checks.append(answers.state in request.states)
    if answers.date_status == "verified":
        checks.append(_in_window(answers.scheduled_date, request))
    if not all(checks):
        return verdict("excluded")
    if len(checks) < 3:
        return verdict("refused")
    return verdict("included")


def _in_window(value: object, request: WindowRequest) -> bool:
    if type(value) is not str:
        return False
    try:
        moment = datetime.strptime(value, ODOO_DATETIME_FORMAT)
    except ValueError:
        return False
    return request.scheduled_from <= moment <= request.scheduled_to


def _row(item: Classified) -> dict[str, object]:
    answers = item.answers
    if item.disposition == "included":
        return {
            "picking_id": item.picking_id,
            "disposition": "included",
            "company_id": answers.company_id,
            "state": answers.state,
            "scheduled_date": answers.scheduled_date,
            "statuses": {"company": "verified", "state": "verified", "scheduled_date": "verified"},
        }
    return {
        "picking_id": item.picking_id,
        "disposition": "refused",
        "company_id": False,
        "state": False,
        "scheduled_date": False,
        "statuses": {
            "company": answers.company_status,
            "state": answers.state_status,
            "scheduled_date": answers.date_status,
        },
    }


def _envelope(
    status: RequestStatus, totals: dict[str, int] | bool, rows: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "projection": PROJECTION_NAME,
        "status": status,
        "coverage": COVERAGE if status == "covered" else False,
        "absence_proven": False,
        "totals": totals,
        "rows": rows,
    }


def refuse_request(status: RequestStatus) -> dict[str, object]:
    """A whole-request refusal: no coverage, no totals, no rows. Nothing reads as an answer."""

    assert status != "covered"
    return _envelope(status, False, [])


def build_response(classified: tuple[Classified, ...], request: WindowRequest) -> dict[str, object]:
    """The whole covered answer, or ``rows-over-limit`` if it would not fit ``max_rows``."""

    listed = sorted(
        (item for item in classified if item.disposition != "excluded"),
        key=lambda item: item.picking_id,
    )
    if len(listed) > request.max_rows:
        return refuse_request("rows-over-limit")
    totals = {
        "candidates": len(classified),
        "included": sum(1 for item in classified if item.disposition == "included"),
        "refused": sum(1 for item in classified if item.disposition == "refused"),
        "excluded": sum(1 for item in classified if item.disposition == "excluded"),
    }
    return _envelope("covered", totals, [_row(item) for item in listed])
