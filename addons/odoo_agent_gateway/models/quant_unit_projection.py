"""The ``quant_unit_v1`` checked projection, as Odoo exposes it (ADR 0009 §5, §6; ADR 0002).

One new RPC-callable method. It reads; it cannot write. The dependency path, what the answer means
and what cannot be derived are in ``projection/quant_unit.py``; this module does the ORM work.

## The whole method, in order

1. Refuse ``env.su``; refuse any caller without :data:`SERVICE_GROUP`; validate the id list.
2. ``_filtered_access('read')`` the quants, as the caller.
3. Prove every field on the path readable as the caller (``_has_field_access``, a field's
   ``groups``): ``stock.quant.product_id``, ``product.product.product_tmpl_id``,
   ``product.product.uom_id`` and ``product.template.uom_id``.
4. Read the quant's plain ``product_id``; prove the **product** row readable.
5. Read the product's plain ``product_tmpl_id``; prove the **template** row readable. The template's
   rules reach the product through ``_inherits``, but its ACL does not, so this is a separate proof.
6. Read the template's plain ``uom_id``; prove the **unit** row readable.
7. Hand the facts to the pure core and return its fixed answer.

Each hop is proven before the value behind it is read, because the ORM checks record access on
fetch, not on a cache hit (measured on this image for ``product_identity_v1``); every read is inside
a guard that turns an ``AccessError`` into a refusal. Neither ``stock.quant.product_uom_id`` nor the
delegated ``product.product.uom_id`` is ever read.

No ``sudo``, ``with_user``, ``with_company`` or ``with_context``. No search and no SQL. The only
argument is a list of integers. ``@api.readonly``.
"""

from odoo import api, models  # type: ignore[import-not-found]
from odoo.exceptions import AccessError, UserError  # type: ignore[import-not-found]

from ..projection.quant_unit import (
    GUARDED_FIELDS,
    ProjectionInputError,
    QuantUnitFacts,
    build_response,
    decide,
    validate_quant_ids,
)
from .picking_company_projection import SERVICE_GROUP

QUANT_MODEL = "stock.quant"
PRODUCT_MODEL = "product.product"
TEMPLATE_MODEL = "product.template"
UOM_MODEL = "uom.uom"


class QuantUnitProjection(models.AbstractModel):
    """Hosts the projection on its own abstract model, like the other projections."""

    _name = "odoo.agent.quant.unit.projection"
    _description = "Checked projection: stock.quant unit, re-derived hop by hop under the caller"

    @api.model
    @api.readonly
    def agent_projected_quant_unit_v1(self, quant_ids):
        """Return each quant's unit, proven under the caller, or a closed refusal."""

        if self.env.su:
            raise UserError("odoo-agent-projection-refuses-superuser-mode")
        if not self.env.user._has_group(SERVICE_GROUP):
            raise AccessError("odoo-agent-projection-requires-the-service-group")
        try:
            ids = validate_quant_ids(quant_ids)
        except ProjectionInputError as error:
            raise UserError(f"odoo-agent-projection-{error.code}") from None

        visible = set(self.env[QUANT_MODEL].browse(ids).exists()._filtered_access("read").ids)
        fields_readable = all(
            self.env[model]._has_field_access(self.env[model]._fields[name], "read")
            for model, name in GUARDED_FIELDS
        )
        return build_response(
            tuple(
                decide(self._gather(quant_id, quant_id in visible, fields_readable))
                for quant_id in ids
            )
        )

    def _gather(self, quant_id, quant_visible, fields_readable):
        """One quant's path under the caller. Every read here can legitimately fail."""

        facts = dict(
            quant_id=quant_id,
            quant_visible=quant_visible,
            fields_readable=fields_readable,
            product_id=None,
            product_visible=False,
            template_id=None,
            template_visible=False,
            uom_id=None,
            uom_visible=False,
        )
        if not (quant_visible and fields_readable):
            return QuantUnitFacts(**facts)
        try:
            quant = self.env[QUANT_MODEL].browse(quant_id)
            facts["product_id"] = quant["product_id"].id or None
            if facts["product_id"] is None:
                return QuantUnitFacts(**facts)
            product = self.env[PRODUCT_MODEL].browse(facts["product_id"])
            facts["product_visible"] = self._visible(product)
            if not facts["product_visible"]:
                return QuantUnitFacts(**facts)

            facts["template_id"] = product["product_tmpl_id"].id or None
            if facts["template_id"] is None:
                return QuantUnitFacts(**facts)
            template = self.env[TEMPLATE_MODEL].browse(facts["template_id"])
            facts["template_visible"] = self._visible(template)
            if not facts["template_visible"]:
                return QuantUnitFacts(**facts)

            facts["uom_id"] = template["uom_id"].id or None
            if facts["uom_id"] is None:
                return QuantUnitFacts(**facts)
            facts["uom_visible"] = self._visible(self.env[UOM_MODEL].browse(facts["uom_id"]))
        except AccessError:
            # Something between the proofs and the read refused: nothing past it is an answer.
            facts.update(fields_readable=False)
        return QuantUnitFacts(**facts)

    @staticmethod
    def _visible(record):
        return bool(record.exists()._filtered_access("read"))
