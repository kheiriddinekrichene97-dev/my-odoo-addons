"""The ``quant_company_v1`` checked projection, as Odoo exposes it (ADR 0009 §5, §6; ADR 0002).

One new RPC-callable method. It reads; it cannot write. What it answers, and what it cannot prove,
are in ``projection/quant_company.py``; this module does the ORM work.

## The whole method, in order

1. Refuse ``env.su``; refuse any caller without :data:`SERVICE_GROUP`; validate the id list.
2. ``_filtered_access('read')`` the quants, as the caller.
3. For each visible quant, prove ``stock.quant.location_id``, ``stock.quant.company_id`` and
   ``stock.location.company_id`` readable as the caller (``_has_field_access``: a field's
   ``groups``), then read the quant's plain ``location_id`` (``required``, ``ondelete='restrict'``,
   ``stock_quant.py:57``) and its stored ``company_id`` -- the latter only as a tripwire.
4. Re-read that **location** as the caller: its ACL and its own multi-company rule decide.
5. Read the location's plain ``company_id``, and when it is set prove the company row readable and
   in ``env.companies``.
6. Hand the facts to the pure core and return its fixed answer.

The explicit record checks matter because the ORM checks record access on fetch, not on a cache hit
(measured for ``product_identity_v1`` on the same image): without them a location value another
environment loaded into this transaction would be returned to a caller with no right to it. Every
read is inside a guard that turns an ``AccessError`` into a refusal.

No ``sudo``, ``with_user``, ``with_company`` or ``with_context``. No search and no SQL. The only
argument is a list of integers. ``@api.readonly``.
"""

from odoo import api, models  # type: ignore[import-not-found]
from odoo.exceptions import AccessError, UserError  # type: ignore[import-not-found]

from ..projection.quant_company import (
    ProjectionInputError,
    QuantFacts,
    build_response,
    decide,
    validate_quant_ids,
)
from .picking_company_projection import SERVICE_GROUP

QUANT_MODEL = "stock.quant"
LOCATION_MODEL = "stock.location"
COMPANY_MODEL = "res.company"


class QuantCompanyProjection(models.AbstractModel):
    """Hosts the projection on its own abstract model, like the other projections."""

    _name = "odoo.agent.quant.company.projection"
    _description = "Checked projection: stock.quant company, re-read from its location"

    @api.model
    @api.readonly
    def agent_projected_quant_company_v1(self, quant_ids):
        """Return each quant's company, proven under the caller, or a closed refusal."""

        if self.env.su:
            raise UserError("odoo-agent-projection-refuses-superuser-mode")
        if not self.env.user._has_group(SERVICE_GROUP):
            raise AccessError("odoo-agent-projection-requires-the-service-group")
        try:
            ids = validate_quant_ids(quant_ids)
        except ProjectionInputError as error:
            raise UserError(f"odoo-agent-projection-{error.code}") from None

        visible = set(self.env[QUANT_MODEL].browse(ids).exists()._filtered_access("read").ids)
        allowed = set(self.env.companies.ids)
        return build_response(
            tuple(decide(self._gather(quant_id, quant_id in visible, allowed)) for quant_id in ids)
        )

    def _gather(self, quant_id, quant_visible, allowed):
        """One quant's facts under the caller. Every read here can legitimately fail."""

        facts = dict(
            quant_id=quant_id,
            quant_visible=quant_visible,
            fields_readable=False,
            location_id=None,
            location_visible=False,
            derived_company_id=None,
            stored_company_id=None,
            company_visible=False,
            company_allowed=False,
        )
        if not quant_visible:
            return QuantFacts(**facts)

        quants = self.env[QUANT_MODEL]
        locations = self.env[LOCATION_MODEL]
        if not (
            quants._has_field_access(quants._fields["location_id"], "read")
            and quants._has_field_access(quants._fields["company_id"], "read")
            and locations._has_field_access(locations._fields["company_id"], "read")
        ):
            return QuantFacts(**facts)
        quant = quants.browse(quant_id)
        try:
            location_id = quant["location_id"].id or None
            stored_company_id = quant["company_id"].id or None
        except AccessError:
            return QuantFacts(**facts)
        facts.update(
            fields_readable=True, location_id=location_id, stored_company_id=stored_company_id
        )
        if location_id is None:
            return QuantFacts(**facts)

        location = locations.browse(location_id)
        if not location.exists()._filtered_access("read"):
            return QuantFacts(**facts)
        try:
            derived_company_id = location["company_id"].id or None
        except AccessError:
            return QuantFacts(**facts)
        facts.update(location_visible=True, derived_company_id=derived_company_id)
        if derived_company_id is not None:
            company = self.env[COMPANY_MODEL].browse(derived_company_id)
            facts.update(
                company_visible=bool(company.exists()._filtered_access("read")),
                company_allowed=derived_company_id in allowed,
            )
        return QuantFacts(**facts)
