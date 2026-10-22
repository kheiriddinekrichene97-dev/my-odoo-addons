"""The ``product_identity_v1`` checked projection, as Odoo exposes it (ADR 0009 §5, §6; ADR 0002).

One new RPC-callable method. It reads; it cannot write. What it answers, and the pinned semantics of
each field, are in ``projection/product_identity.py``; this module does the ORM work.

## The whole method, in order

1. Refuse ``env.su``; refuse any caller without :data:`SERVICE_GROUP`; validate the id list.
2. ``_filtered_access('read')`` the products, as the caller. This already includes the template's
   record rules, which ``ir.rule`` propagates through ``_inherits``.
3. For each visible product, read its plain ``product_tmpl_id`` (``required``,
   ``ondelete='cascade'``, ``addons/product/models/product_product.py:42``) after proving that
   field readable, and re-read that **template** as the caller: its ACL and rules decide, not the
   product's.
4. Prove each of the five template fields readable as the caller (``_has_field_access``: a field's
   ``groups``), and only then read the five values **from the template**. Never through the
   product: a delegated read is exactly what this projection replaces, and a readable product row
   is no evidence about the template.
5. Prove the unit row, and the company row when there is one, readable as the caller.
6. Hand the facts to the pure core and return its fixed answer.

The explicit record checks in steps 3 and 5 matter because the ORM checks record access on fetch,
not on a cache hit: without them a template value another environment loaded into this transaction
is returned to a caller with no right to the template (measured). Field ``groups`` are enforced by
Odoo on every read (measured); step 4 checks them explicitly anyway, and the values are read inside
a guard that turns an ``AccessError`` into a refusal.

No ``sudo``, ``with_user``, ``with_company`` or ``with_context``. No search and no SQL. The only
argument is a list of integers. ``@api.readonly``.
"""

from odoo import api, models  # type: ignore[import-not-found]
from odoo.exceptions import AccessError, UserError  # type: ignore[import-not-found]

from ..projection.product_identity import (
    PROJECTED_FIELDS,
    ProductFacts,
    ProjectionInputError,
    build_response,
    decide,
    validate_product_ids,
)
from .picking_company_projection import SERVICE_GROUP

PRODUCT_MODEL = "product.product"
TEMPLATE_MODEL = "product.template"
TEMPLATE_FIELD = "product_tmpl_id"
UOM_MODEL = "uom.uom"
COMPANY_MODEL = "res.company"


class ProductIdentityProjection(models.AbstractModel):
    """Hosts the projection on its own abstract model, like the picking projections."""

    _name = "odoo.agent.product.identity.projection"
    _description = "Checked projection: product.product identity, re-read from its template"

    @api.model
    @api.readonly
    def agent_projected_product_identity_v1(self, product_ids):
        """Return each product's five identity values, proven under the caller, or a refusal."""

        if self.env.su:
            raise UserError("odoo-agent-projection-refuses-superuser-mode")
        if not self.env.user._has_group(SERVICE_GROUP):
            raise AccessError("odoo-agent-projection-requires-the-service-group")
        try:
            ids = validate_product_ids(product_ids)
        except ProjectionInputError as error:
            raise UserError(f"odoo-agent-projection-{error.code}") from None

        lang = self.env.lang or "en_US"
        visible = set(self.env[PRODUCT_MODEL].browse(ids).exists()._filtered_access("read").ids)
        results = tuple(
            decide(self._gather(product_id, product_id in visible)) for product_id in ids
        )
        return build_response(lang, results)

    def _gather(self, product_id, product_visible):
        """One product's facts under the caller. Every read here can legitimately fail."""

        facts = dict(
            product_id=product_id,
            product_visible=product_visible,
            link_readable=False,
            template_id=None,
            template_visible=False,
            fields_readable=False,
            name=None,
            type=None,
            is_storable=None,
            uom_id=None,
            uom_visible=False,
            company_id=None,
            company_visible=False,
        )
        if not product_visible:
            return ProductFacts(**facts)

        products = self.env[PRODUCT_MODEL]
        if not products._has_field_access(products._fields[TEMPLATE_FIELD], "read"):
            return ProductFacts(**facts)
        facts["link_readable"] = True
        template_id = self._reference(products.browse(product_id), TEMPLATE_FIELD)
        facts["template_id"] = template_id
        if template_id is None:
            return ProductFacts(**facts)

        template = self.env[TEMPLATE_MODEL].browse(template_id)
        facts["template_visible"] = bool(template.exists()._filtered_access("read"))
        if not facts["template_visible"]:
            return ProductFacts(**facts)
        if not all(
            template._has_field_access(template._fields[name], "read") for name in PROJECTED_FIELDS
        ):
            return ProductFacts(**facts)

        try:
            values = {name: template[name] for name in PROJECTED_FIELDS}
        except AccessError:
            return ProductFacts(**facts)
        uom_id = values["uom_id"].id or None
        company_id = values["company_id"].id or None
        facts.update(
            name=values["name"],
            type=values["type"],
            is_storable=values["is_storable"],
            fields_readable=True,
            uom_id=uom_id,
            uom_visible=uom_id is not None and self._visible(UOM_MODEL, uom_id),
            company_id=company_id,
            company_visible=company_id is not None and self._visible(COMPANY_MODEL, company_id),
        )
        return ProductFacts(**facts)

    def _visible(self, model, record_id):
        return bool(self.env[model].browse(record_id).exists()._filtered_access("read"))

    @staticmethod
    def _reference(record, field_name):
        """One Many2one's raw id; an access failure is an answer, and its message stays here."""

        try:
            value = record[field_name]
        except AccessError:
            return None
        return value.id if value else None
