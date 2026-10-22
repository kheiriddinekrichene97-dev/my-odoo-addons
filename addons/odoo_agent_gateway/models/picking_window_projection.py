"""The ``picking_window_v1`` bounded search projection, as Odoo exposes it (ADR 0009 §5/§6, 0002).

One new RPC-callable method. It reads; it cannot write. The exact question it proves, the
dispositions and the one-call response contract live in ``projection/picking_window.py``; this
module does the ORM work.

## The whole method, in order

1. Refuse ``env.su``; refuse any caller without :data:`SERVICE_GROUP`; validate the arguments.
   None of this reads business data.
2. **Snapshot, checked.** Read the transaction's isolation level and ``pg_current_snapshot()``.
   Unless the level gives every statement one snapshot (``REPEATABLE READ`` or ``SERIALIZABLE``),
   refuse the whole request before the first business read.
3. **Scope, up front.** The requested company must be in ``env.companies``, and every requested
   operation type must be readable by the caller and carry that company in its plain
   ``company_id`` (``addons/stock/models/stock_picking.py:130``). Otherwise the whole request is
   refused: a type the caller cannot see, or of another company, would make "every picking of these
   types" a different question from the one asked.
4. **Set A**, as the caller: open, non-draft ``stock.move`` rows dated in the window whose
   picking's plain ``picking_type_id`` is requested -- the ``picking_id.picking_type_id`` path is
   searched through ``stock.picking``'s own record rules (``odoo/orm/fields_relational.py:485``,
   ``picking_id`` has no ``bypass_search_access``). ``bound + 1``; overflow refuses the request.
5. **Set B**, as the caller: ``stock.picking`` of a requested type whose stored
   ``scheduled_date`` is in the window. ``bound + 1``; overflow refuses the request.
6. The union, bounded; overflow refuses the request.
7. Run ``picking_company_v1``, ``picking_state_v1`` and ``picking_scheduled_date_v1`` on the union
   **in this same transaction**.
8. **Snapshot, rechecked.** ``pg_current_snapshot()`` must still equal step 2's value, so no commit
   or rollback happened inside the call; otherwise the whole request is refused.
9. Classify in the pure core and return the **whole** answer, or refuse if it exceeds
   ``max_rows``. There are no pages: one call is one snapshot.

Steps 4, 5 and 7 are private methods only so the integration suite can commit a change from a
second PostgreSQL connection between them. ``get_public_method`` refuses underscore names, so they
are not reachable over RPC.

## Isolation, as measured on the pinned Odoo 19.0-20260908

Every ``odoo.sql_db.Cursor`` sets ``REPEATABLE READ`` (``odoo/sql_db.py:373``). A JSON-2 call to
this ``@api.readonly`` method is served by ``Request._serve_db`` (``odoo/http.py:2267-2329``). With
``db_replica_host`` set, it runs on the replica cursor in the same transaction as
``check_signaling``. Without one, ``registry.cursor(readonly=True)`` returns a primary read/write
cursor, and ``_serve_db`` rolls back after ``check_signaling`` and serves the method in a fresh
``REPEATABLE READ READ WRITE`` transaction -- measured with PostgreSQL statement logging: every
statement of the call, both snapshot reads included, in one virtual transaction, all of them
``SELECT``. A retry or a read-only fallback reruns the whole method in a new transaction, so each
attempt is checked on its own. Steps 2 and 8 do not
trust any of this; they read PostgreSQL.

Exactly two searches, both literal apart from the validated request values. The two snapshot reads
are constant SQL with no parameters.

The response answers the narrow question named by ``COVERAGE`` and never claims absence; see the
core module for why a complete recomputed business answer is not provable under the caller.

No ``sudo``,
``with_user``, ``with_company`` or ``with_context``. ``@api.readonly``.
"""

from odoo import api, models  # type: ignore[import-not-found]
from odoo.exceptions import AccessError, UserError  # type: ignore[import-not-found]

from ..projection.picking_window import (
    MAX_CANDIDATES,
    MAX_STORED_CANDIDATES,
    MAX_WITNESS_MOVES,
    WITNESS_MOVE_STATES,
    CandidateAnswers,
    WindowInputError,
    build_response,
    classify,
    refuse_request,
    snapshot_established,
    validate_request,
)
from .picking_company_projection import SERVICE_GROUP

PICKING_MODEL = "stock.picking"
PICKING_TYPE_MODEL = "stock.picking.type"
MOVE_MODEL = "stock.move"
COMPANY_PROJECTION = "odoo.agent.picking.company.projection"
STATE_PROJECTION = "odoo.agent.picking.state.projection"
DATE_PROJECTION = "odoo.agent.picking.scheduled.date.projection"


class StockPickingWindowProjection(models.AbstractModel):
    """Hosts the search projection on its own abstract model, like the three it composes."""

    _name = "odoo.agent.picking.window.projection"
    _description = "Checked projection: bounded picking window search, verified under the caller"

    @api.model
    @api.readonly
    def agent_projected_picking_window_v1(
        self,
        company_id,
        picking_type_ids,
        states,
        scheduled_from,
        scheduled_to,
        max_rows,
    ):
        """Return the whole covered answer in one snapshot, or a whole-request refusal.

        Every argument is data with a fixed grammar; none is a model, field, domain, operator,
        context or ordering.
        """

        if self.env.su:
            raise UserError("odoo-agent-projection-refuses-superuser-mode")
        if not self.env.user._has_group(SERVICE_GROUP):
            raise AccessError("odoo-agent-projection-requires-the-service-group")
        try:
            request = validate_request(
                company_id=company_id,
                picking_type_ids=picking_type_ids,
                states=states,
                scheduled_from=scheduled_from,
                scheduled_to=scheduled_to,
                max_rows=max_rows,
            )
        except WindowInputError as error:
            raise UserError(f"odoo-agent-projection-{error.code}") from None

        isolation, snapshot = self._transaction_snapshot()
        if not snapshot_established(isolation):
            return refuse_request("snapshot-not-established")

        if request.company_id not in set(self.env.companies.ids):
            return refuse_request("company-not-allowed")
        if not self._types_belong_to_company(request):
            return refuse_request("operation-type-scope-invalid")

        try:
            witnesses = self._witness_moves(request)
            if len(witnesses) > MAX_WITNESS_MOVES:
                return refuse_request("witness-moves-over-bound")
            witnessed = {move["picking_id"].id for move in witnesses}
        except AccessError:
            return refuse_request("witness-moves-not-readable")

        stored = self._stored_candidates(request)
        if len(stored) > MAX_STORED_CANDIDATES:
            return refuse_request("stored-candidates-over-bound")

        candidates = sorted(witnessed | set(stored.ids))
        if len(candidates) > MAX_CANDIDATES:
            return refuse_request("candidates-over-bound")
        classified = (
            tuple(classify(answers, request) for answers in self._verify(candidates))
            if candidates
            else ()
        )

        if self._transaction_snapshot()[1] != snapshot:
            return refuse_request("snapshot-changed")
        return build_response(classified, request)

    def _transaction_snapshot(self):
        """This transaction's isolation level and current snapshot, read from PostgreSQL."""

        self.env.cr.execute(
            "SELECT current_setting('transaction_isolation'), pg_current_snapshot()::text"
        )
        return self.env.cr.fetchone()

    def _witness_moves(self, request):
        """Set A, as the caller: open non-draft moves dated in the window, of a requested type."""

        window_from, window_to = request.window_strings
        return self.env[MOVE_MODEL].search(
            [
                ("state", "in", list(WITNESS_MOVE_STATES)),
                ("date", ">=", window_from),
                ("date", "<=", window_to),
                ("picking_id.picking_type_id", "in", list(request.picking_type_ids)),
            ],
            order="id",
            limit=MAX_WITNESS_MOVES + 1,
        )

    def _stored_candidates(self, request):
        """Set B, as the caller: pickings of a requested type whose stored date is in the window."""

        window_from, window_to = request.window_strings
        return self.env[PICKING_MODEL].search(
            [
                ("picking_type_id", "in", list(request.picking_type_ids)),
                ("scheduled_date", ">=", window_from),
                ("scheduled_date", "<=", window_to),
            ],
            order="id",
            limit=MAX_STORED_CANDIDATES + 1,
        )

    def _types_belong_to_company(self, request):
        """Every requested type is readable by the caller and belongs to the requested company."""

        types = self.env[PICKING_TYPE_MODEL].browse(request.picking_type_ids)
        visible = types.exists()._filtered_access("read")
        if len(visible) != len(request.picking_type_ids):
            return False
        try:
            return all(
                picking_type["company_id"].id == request.company_id for picking_type in visible
            )
        except AccessError:
            return False

    def _verify(self, candidates):
        """The three checked projections, on the whole candidate set, in this transaction."""

        company = self.env[COMPANY_PROJECTION].agent_projected_picking_company_v1(candidates)
        state = self.env[STATE_PROJECTION].agent_projected_picking_state_v1(candidates)
        date = self.env[DATE_PROJECTION].agent_projected_picking_scheduled_date_v1(candidates)
        by_id = {}
        for name, response in (("company", company), ("state", state), ("date", date)):
            for row in response["results"]:
                by_id.setdefault(row["picking_id"], {})[name] = row
        answers = []
        for picking_id in candidates:
            rows = by_id[picking_id]
            answers.append(
                CandidateAnswers(
                    picking_id=picking_id,
                    company_status=rows["company"]["status"],
                    company_id=rows["company"]["company_id"],
                    state_status=rows["state"]["status"],
                    state=rows["state"]["state"],
                    date_status=rows["date"]["status"],
                    scheduled_date=rows["date"]["scheduled_date"],
                )
            )
        return answers
