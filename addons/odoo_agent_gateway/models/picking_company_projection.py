"""The ``picking_company_v1`` checked projection, as Odoo exposes it (ADR 0009 §5, §6; ADR 0002).

One new RPC-callable method, and nothing else in this addon is callable. It reads; it cannot write.

## The whole method, in order

1. Refuse outright if the environment is in superuser mode. Under ``env.su`` every access check in
   Odoo short-circuits to "allowed" (``odoo/orm/models.py:4120``), so a projection that ran there
   would return values it had proven nothing about while looking exactly like one that had.
2. Refuse unless the caller holds :data:`SERVICE_GROUP`. This is *the* authorization gate and it
   must live here, because Odoo's RPC dispatch performs no access check before calling a public
   method. Being absent from this repository's own transport allowlist constrains our client and
   nobody else's.
3. Validate the id list: explicit, positive, de-duplicated, at most ``MAX_PICKING_IDS``.
4. ``_filtered_access('read')`` the pickings. Existence and visibility collapse into one answer on
   purpose -- see ``STATUSES`` in the decision core.
5. For each visible picking, read its ``picking_type_id`` -- a plain stored column
   (``addons/stock/models/stock_picking.py:620``) -- and re-read that operation type **as the
   caller**, so ``stock.picking.type``'s own multi-company rule
   (``addons/stock/security/stock_security.xml:78``) decides whether this identity may see it.
6. Take the company from ``stock.picking.type.company_id``, an ordinary required stored column
   (``addons/stock/models/stock_picking.py:130``) -- *not* from ``stock.picking.company_id``, which
   is the elevated related copy this projection exists to replace.
7. Prove the company record itself is readable, and that it is in ``env.companies``.
8. Hand the gathered facts to the pure decision core and return its fixed-schema answer.

## What this method never does

No ``sudo``. No ``with_user``, ``with_company`` or ``with_context``. No ``search``. No caller-
supplied model, field list, domain, context or ordering -- the only argument is a list of integers.
No Odoo exception text, field name, record name or message reaches the response: every outcome is
one member of a closed vocabulary this repository publishes.

``@api.readonly`` is load bearing rather than documentation: on an RPC call Odoo gives the method a
readonly cursor (``odoo/orm/decorators.py:345``), so a write introduced here later fails at the
database rather than silently succeeding.
"""

from odoo import api, models  # type: ignore[import-not-found]
from odoo.exceptions import AccessError, UserError  # type: ignore[import-not-found]

from ..projection.picking_company import (
    PickingFacts,
    ProjectionInputError,
    build_response,
    decide,
    validate_picking_ids,
)

PICKING_MODEL = "stock.picking"
PICKING_TYPE_MODEL = "stock.picking.type"
COMPANY_MODEL = "res.company"

SERVICE_GROUP = "odoo_agent_gateway.group_agent_projection_service"
"""The only identity permitted to invoke this projection.

**An ``ir.model.access`` entry cannot do this job.** Odoo 19 dispatches
``/json/2/<model>/<method>`` and ``call_kw`` through ``get_public_method``
(``odoo/service/model.py:45``), which refuses private, ``@api.private``, class- and static-methods
and then **calls the function**. There is no ``check_access`` anywhere on that path. An ACL bounds
what the ORM does *inside* a method; it does not decide whether the method runs.

Measured, not assumed: against Odoo Server 19.0-20260908, with this model's ACL granted to
``base.group_user``, an ordinary internal user called this method over ``/json/2/`` and received
HTTP 200 and a well-formed response. Authorization therefore has to be an explicit statement in the
method body, and the integration suite reproduces the original gap as a regression test.

The group is unassigned on install (``security/agent_gateway_groups.xml``), so the default answer is
"no" for every identity including ``base.user_admin`` and ``base.user_root``.
"""


class StockPickingCompanyProjection(models.AbstractModel):
    """Hosts the projection on its own abstract model rather than patching ``stock.picking``.

    Keeping it off ``stock.picking`` means the new callable cannot be reached by anything that
    already holds a picking record set, and the addon adds no field, no column and no override to a
    standard model. The ACL for this model is the only thing that decides who may call it.
    """

    _name = "odoo.agent.picking.company.projection"
    _description = "Checked projection: stock.picking company, proven under the caller"

    @api.model
    @api.readonly
    def agent_projected_picking_company_v1(self, picking_ids):
        """Return each picking's company, proven under the calling identity, or a closed refusal.

        The single argument is a list of positive integers. There is no second argument, and there
        is deliberately no way to influence which model, fields, domain, context or order are used.
        """

        if self.env.su:
            # Not an access failure -- an argument that this answer would be worthless.
            raise UserError("odoo-agent-projection-refuses-superuser-mode")
        # Authorization, before the arguments are even parsed: an unauthorized caller learns
        # nothing about the argument grammar and no work is done on its behalf.
        if not self.env.user._has_group(SERVICE_GROUP):
            raise AccessError("odoo-agent-projection-requires-the-service-group")
        try:
            ids = validate_picking_ids(picking_ids)
        except ProjectionInputError as error:
            raise UserError(f"odoo-agent-projection-{error.code}") from None

        pickings = self.env[PICKING_MODEL].browse(ids)
        visible = set(pickings.exists()._filtered_access("read").ids)

        allowed_companies = set(self.env.companies.ids)
        results = []
        for picking_id in ids:
            results.append(
                decide(
                    self._gather_picking_facts(
                        picking_id=picking_id,
                        visible=picking_id in visible,
                        allowed_companies=allowed_companies,
                    )
                )
            )
        return build_response(tuple(results))

    def _gather_picking_facts(self, *, picking_id, visible, allowed_companies):
        """Collect one picking's facts under the caller. Every read here can legitimately fail."""

        if not visible:
            return PickingFacts(
                picking_id=picking_id,
                picking_visible=False,
                operation_type_id=None,
                operation_type_visible=False,
                derived_company_id=None,
                stored_company_id=None,
                company_visible=False,
                company_allowed=False,
            )

        picking = self.env[PICKING_MODEL].browse(picking_id)
        # Reading the raw id avoids materialising the related record set as a side effect, so the
        # operation type's visibility is decided below by an explicit check rather than implicitly.
        operation_type_id = self._readable_reference(picking, "picking_type_id")
        stored_company_id = self._readable_reference(picking, "company_id")

        if operation_type_id is None:
            return PickingFacts(
                picking_id=picking_id,
                picking_visible=True,
                operation_type_id=None,
                operation_type_visible=False,
                derived_company_id=None,
                stored_company_id=stored_company_id,
                company_visible=False,
                company_allowed=False,
            )

        operation_type = self.env[PICKING_TYPE_MODEL].browse(operation_type_id)
        operation_type_visible = bool(operation_type.exists()._filtered_access("read"))
        derived_company_id = (
            self._readable_reference(operation_type, "company_id")
            if operation_type_visible
            else None
        )

        company_visible = False
        if derived_company_id is not None:
            company = self.env[COMPANY_MODEL].browse(derived_company_id)
            company_visible = bool(company.exists()._filtered_access("read"))

        return PickingFacts(
            picking_id=picking_id,
            picking_visible=True,
            operation_type_id=operation_type_id,
            operation_type_visible=operation_type_visible,
            derived_company_id=derived_company_id,
            stored_company_id=stored_company_id,
            company_visible=company_visible,
            company_allowed=derived_company_id in allowed_companies,
        )

    @staticmethod
    def _readable_reference(record, field_name):
        """Read one Many2one's raw id, mapping every access failure to "not visible".

        Field-level access can be refused independently of the record, so this must not assume that
        a readable row implies a readable column. An ``AccessError`` here is an answer, not an
        incident, and its message never leaves this frame.
        """

        try:
            value = record[field_name]
        except AccessError:
            return None
        return value.id if value else None
