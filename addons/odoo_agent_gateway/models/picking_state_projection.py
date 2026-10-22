"""The ``picking_state_v1`` checked projection, as Odoo exposes it (ADR 0009 §5/§6, 0002).

One new RPC-callable method. It reads; it cannot write. The full trace of Odoo's computation and the
reasoning behind every refusal live in ``projection/picking_state.py``; this module gathers facts
under the calling identity and nothing else.

## The whole method, in order

1. Refuse ``env.su``; refuse any caller without :data:`SERVICE_GROUP`; validate the id list with
   the grammar every picking projection shares.
2. ``_filtered_access('read')`` the pickings, collapsing absent and hidden into one answer.
3. For each visible picking: read the plain ``picking_type_id`` and prove that operation type
   readable (the hop behind ``move_type``); read ``move_type`` and the stored ``state`` (a
   tripwire).
4. Search ``stock.move`` **as the caller** -- one literal domain, order ``id``, ``bound + 1`` limit,
   exactly as ``picking_scheduled_date_v1`` does and for the same reasons -- and read each move's
   plain ``state``, ``product_uom_qty`` and ``procure_method``.
5. Hand the facts to the pure decision core.

**No location is read.** ``stock.picking.location_id`` and ``stock.move.location_dest_id`` are
elevated stored computes whose choice of location depends on partner properties, ``ir.default``
and route logic; a readable target row proves nothing about how the id was chosen. The core answers
only where every possible usage of both locations gives the same state, and refuses elsewhere -- see
the proof in ``projection/picking_state.py``.

No ``sudo``, ``with_user``, ``with_company`` or ``with_context``. No caller-supplied model, field
list, domain, context or ordering. Exactly one search. ``@api.readonly``.
"""

from odoo import api, models  # type: ignore[import-not-found]
from odoo.exceptions import AccessError, UserError  # type: ignore[import-not-found]

from ..projection.picking_company import ProjectionInputError, validate_picking_ids
from ..projection.picking_scheduled_date import MAX_MOVES_PER_PICKING
from ..projection.picking_state import MoveStateFacts, StateFacts, build_response, decide
from .picking_company_projection import SERVICE_GROUP

PICKING_MODEL = "stock.picking"
PICKING_TYPE_MODEL = "stock.picking.type"
MOVE_MODEL = "stock.move"


class StockPickingStateProjection(models.AbstractModel):
    """Hosts the projection on its own abstract model, like the other two picking projections."""

    _name = "odoo.agent.picking.state.projection"
    _description = "Checked projection: stock.picking state, recomputed under the caller"

    @api.model
    @api.readonly
    def agent_projected_picking_state_v1(self, picking_ids):
        """Return each picking's state, recomputed from inputs the caller can read.

        The single argument is a list of positive integers, and there is deliberately no way to
        influence which model, fields, domain, context or order are used.
        """

        if self.env.su:
            raise UserError("odoo-agent-projection-refuses-superuser-mode")
        if not self.env.user._has_group(SERVICE_GROUP):
            raise AccessError("odoo-agent-projection-requires-the-service-group")
        try:
            ids = validate_picking_ids(picking_ids)
        except ProjectionInputError as error:
            raise UserError(f"odoo-agent-projection-{error.code}") from None

        pickings = self.env[PICKING_MODEL].browse(ids)
        visible = set(pickings.exists()._filtered_access("read").ids)

        return build_response(
            tuple(
                decide(self._gather_facts(picking_id=picking_id, visible=picking_id in visible))
                for picking_id in ids
            )
        )

    def _gather_facts(self, *, picking_id, visible):
        """Collect one picking's facts under the caller. Every read here can legitimately fail."""

        facts = StateFacts(
            picking_id=picking_id,
            picking_visible=visible,
            operation_type_id=None,
            operation_type_visible=False,
            move_type=None,
            stored_state=None,
            moves_readable=False,
            moves_truncated=False,
            moves=(),
        )
        if not visible:
            return facts

        picking = self.env[PICKING_MODEL].browse(picking_id)
        operation_type_id = self._readable_reference(picking, "picking_type_id")
        if operation_type_id is None:
            return facts
        operation_type = self.env[PICKING_TYPE_MODEL].browse(operation_type_id)
        facts = facts._replace(
            operation_type_id=operation_type_id,
            operation_type_visible=bool(operation_type.exists()._filtered_access("read")),
            move_type=self._readable_value(picking, "move_type"),
            stored_state=self._readable_value(picking, "state"),
        )
        if not facts.operation_type_visible:
            return facts

        try:
            moves = self.env[MOVE_MODEL].search(
                [("picking_id", "=", picking_id)], order="id", limit=MAX_MOVES_PER_PICKING + 1
            )
            if len(moves) > MAX_MOVES_PER_PICKING:
                return facts._replace(moves_readable=True, moves_truncated=True)
            rows = tuple(
                MoveStateFacts(
                    state=move["state"],
                    product_uom_qty=move["product_uom_qty"],
                    procure_method=move["procure_method"],
                )
                for move in moves
            )
        except AccessError:
            # No read right on `stock.move`, or a field-level restriction on one of its inputs.
            return facts
        return facts._replace(moves_readable=True, moves=rows)

    @staticmethod
    def _readable_reference(record, field_name):
        """Read one Many2one's raw id, mapping every access failure to "not visible"."""

        try:
            value = record[field_name]
        except AccessError:
            return None
        return value.id if value else None

    @staticmethod
    def _readable_value(record, field_name):
        """Read one scalar column, mapping every access failure to ``None``."""

        try:
            return record[field_name]
        except AccessError:
            return None
