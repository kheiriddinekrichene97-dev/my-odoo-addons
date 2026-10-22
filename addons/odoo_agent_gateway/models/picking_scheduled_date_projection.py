"""The ``picking_scheduled_date_v1`` checked projection, as Odoo exposes it (ADR 0009 §5/§6, 0002).

One new RPC-callable method. It reads; it cannot write. The decision core and the reasoning behind
every refusal live in ``projection/picking_scheduled_date.py``; this module gathers facts under the
calling identity and nothing else.

## The whole method, in order

1. Refuse outright if ``env.su`` is set: every access check short-circuits there
   (``odoo/orm/models.py:4120``), so a recomputation run under it would prove nothing.
2. Refuse unless the caller holds :data:`SERVICE_GROUP` -- the same unassigned group, checked in the
   method body for the same reason as ``picking_company_v1``: Odoo's RPC dispatch performs no access
   check before calling a public method.
3. Validate the id list with the grammar ``picking_company_v1`` already uses.
4. ``_filtered_access('read')`` the pickings, collapsing absent and hidden into one answer.
5. For each visible picking, read its plain ``picking_type_id`` and prove that operation type
   readable as the caller -- the one hop the elevated ``move_type`` compute takes.
6. Read ``move_type`` (an input choosing min or max) and the stored ``scheduled_date`` (a tripwire).
7. Search ``stock.move`` **as the caller** with a fixed domain, order and ``bound + 1`` limit, so
   ``stock.move``'s own multi-company rule (``stock/security/stock_security.xml:108``) and any
   deployment rule decide which moves take part, and read each one's plain ``state`` and ``date``.
8. Hand the facts to the pure decision core and return its fixed-schema answer.

## Why an explicit search and not ``picking.move_ids``

``One2many.read`` (``odoo/orm/fields_relational.py:940``) fetches with ``comodel.search_fetch`` in
the *reading* environment, so as the caller ``move_ids`` silently omits rule-hidden moves -- and a
value already in the transaction's field cache from an elevated read can make it return them after
all. Either way the set would depend on what ran earlier. A search written here has one fixed
domain, applies the caller's rules every time, and is pinned by a source-level test.

## What this method never does

No ``sudo``, ``with_user``, ``with_company`` or ``with_context``. No caller-supplied model, field
list, domain, context or ordering: the only argument is a list of integers. Exactly one search, on
``stock.move``, with a literal domain. No Odoo exception text reaches the response.
``@api.readonly``, so an RPC call runs on a readonly cursor (``odoo/orm/decorators.py:345``).
"""

from odoo import api, models  # type: ignore[import-not-found]
from odoo.exceptions import AccessError, UserError  # type: ignore[import-not-found]

from ..projection.picking_company import ProjectionInputError, validate_picking_ids
from ..projection.picking_scheduled_date import (
    MAX_MOVES_PER_PICKING,
    MoveFacts,
    ScheduledDateFacts,
    build_response,
    decide,
)
from .picking_company_projection import SERVICE_GROUP

PICKING_MODEL = "stock.picking"
PICKING_TYPE_MODEL = "stock.picking.type"
MOVE_MODEL = "stock.move"


class StockPickingScheduledDateProjection(models.AbstractModel):
    """Hosts the projection on its own abstract model, like ``picking_company_v1``.

    Nothing that already holds a picking record set can reach it, and no standard model gains a
    field, column or override.
    """

    _name = "odoo.agent.picking.scheduled.date.projection"
    _description = "Checked projection: stock.picking scheduled date, recomputed under the caller"

    @api.model
    @api.readonly
    def agent_projected_picking_scheduled_date_v1(self, picking_ids):
        """Return each picking's scheduled date, recomputed from moves the caller can read.

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

        facts = ScheduledDateFacts(
            picking_id=picking_id,
            picking_visible=visible,
            operation_type_id=None,
            operation_type_visible=False,
            move_type=None,
            stored_scheduled_date=None,
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
            stored_scheduled_date=self._readable_value(picking, "scheduled_date"),
        )
        if not facts.operation_type_visible:
            return facts

        try:
            moves = self.env[MOVE_MODEL].search(
                [("picking_id", "=", picking_id)], order="id", limit=MAX_MOVES_PER_PICKING + 1
            )
            if len(moves) > MAX_MOVES_PER_PICKING:
                return facts._replace(moves_readable=True, moves_truncated=True)
            rows = tuple(MoveFacts(state=move["state"], date=move["date"]) for move in moves)
        except AccessError:
            # No read right on `stock.move`, or a field-level restriction on one of the three
            # columns. Either way the move set is unknown, and an unknown set has no extremum.
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
        """Read one scalar column, mapping every access failure to ``None``.

        A falsy value is returned as read (``False`` for an empty ``Datetime``); the decision core
        treats anything but a recognised value as unusable, so no normalisation is needed here.
        """

        try:
            return record[field_name]
        except AccessError:
            return None
