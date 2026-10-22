"""The ``product_search_v1`` bounded search projection, as Odoo exposes it (ADR 0009 §5, §6; 0002).

One new RPC-callable method. It reads; it cannot write. What it answers, the one deliberate
difference from today's search, and the bounds are in ``projection/product_search.py``; this module
does the ORM work.

## The whole method, in order

1. Refuse ``env.su``; refuse any caller without :data:`SERVICE_GROUP`; validate the arguments.
2. **Snapshot, checked** -- the reviewed guard from ``picking_window_v1``: unless the transaction
   reads one snapshot for all its statements, refuse before the first business read.
3. **Scope.** The requested company must be in ``env.companies``; the caller must hold read access
   to ``product.template`` and ``product.product``; and every field any search filters on must be
   readable by the caller (a filter on an unreadable field would disclose it).
4. **Name branch**, as the caller: ``product.template`` by type, storability, company and the name
   pattern (``active in (True, False)``, as today's search ignores the template's ``active``); then
   the active ``product.product`` variants of those templates.
5. **Code branch**, as the caller: ``product.product`` by ``active`` and ``default_code | barcode``.
6. The union, bounded; every bound is probed with ``bound + 1`` and any overflow refuses the whole
   request.
7. ``product_identity_v1`` on the union, in this transaction; classify in the pure core.
8. **Snapshot, rechecked**, then the answer.

Exactly three searches, each literal apart from the validated request values. No ``sudo``,
``with_user``, ``with_company`` or ``with_context``. ``@api.readonly``.
"""

from odoo import api, models  # type: ignore[import-not-found]
from odoo.exceptions import AccessError, UserError  # type: ignore[import-not-found]

from ..projection.picking_window import snapshot_established
from ..projection.product_search import (
    MAX_BRANCH_PRODUCTS,
    MAX_CANDIDATES,
    MAX_TEMPLATE_MATCHES,
    PRODUCT_SEARCH_FIELDS,
    STORABLE_PRODUCT_TYPE,
    TEMPLATE_SEARCH_FIELDS,
    SearchInputError,
    build_response,
    classify,
    refuse_request,
    validate_request,
)
from .picking_company_projection import SERVICE_GROUP

PRODUCT_MODEL = "product.product"
TEMPLATE_MODEL = "product.template"
IDENTITY_PROJECTION = "odoo.agent.product.identity.projection"


class ProductSearchProjection(models.AbstractModel):
    """Hosts the search projection on its own abstract model, like the others."""

    _name = "odoo.agent.product.search.projection"
    _description = (
        "Checked projection: bounded product search, matched and verified under the caller"
    )

    @api.model
    @api.readonly
    def agent_projected_product_search_v1(self, company_id, search_term, max_rows):
        """Return the covered answer in one checked snapshot, or a whole-request refusal.

        Every argument is data with a fixed grammar; none is a model, field, domain, operator,
        context or ordering.
        """

        if self.env.su:
            raise UserError("odoo-agent-projection-refuses-superuser-mode")
        if not self.env.user._has_group(SERVICE_GROUP):
            raise AccessError("odoo-agent-projection-requires-the-service-group")
        try:
            request = validate_request(
                company_id=company_id, search_term=search_term, max_rows=max_rows
            )
        except SearchInputError as error:
            raise UserError(f"odoo-agent-projection-{error.code}") from None

        isolation, snapshot = self._transaction_snapshot()
        if not snapshot_established(isolation):
            return refuse_request("snapshot-not-established")

        if request.company_id not in set(self.env.companies.ids):
            return refuse_request("company-not-allowed")
        products = self.env[PRODUCT_MODEL]
        templates = self.env[TEMPLATE_MODEL]
        if not (products.browse().has_access("read") and templates.browse().has_access("read")):
            return refuse_request("model-not-readable")
        if not all(
            model._has_field_access(model._fields[name], "read")
            for model, names in (
                (templates, TEMPLATE_SEARCH_FIELDS),
                (products, PRODUCT_SEARCH_FIELDS),
            )
            for name in names
        ):
            return refuse_request("field-not-readable")

        pattern = request.pattern
        try:
            matched_templates = self._name_matches(request, pattern)
            if len(matched_templates) > MAX_TEMPLATE_MATCHES:
                return refuse_request("template-matches-over-bound")
            by_name = self._variants(matched_templates)
            by_code = self._code_matches(pattern)
        except AccessError:
            return refuse_request("model-not-readable")
        if len(by_name) > MAX_BRANCH_PRODUCTS or len(by_code) > MAX_BRANCH_PRODUCTS:
            return refuse_request("branch-products-over-bound")

        candidates = sorted(set(by_name.ids) | set(by_code.ids))
        if len(candidates) > MAX_CANDIDATES:
            return refuse_request("candidates-over-bound")
        classified = ()
        if candidates:
            identities = self.env[IDENTITY_PROJECTION].agent_projected_product_identity_v1(
                candidates
            )
            classified = tuple(classify(row, request) for row in identities["results"])
        lang = self.env.lang or "en_US"

        if self._transaction_snapshot()[1] != snapshot:
            return refuse_request("snapshot-changed")
        return build_response(classified, request, lang)

    def _transaction_snapshot(self):
        """This transaction's isolation level and current snapshot, read from PostgreSQL."""

        self.env.cr.execute(
            "SELECT current_setting('transaction_isolation'), pg_current_snapshot()::text"
        )
        return self.env.cr.fetchone()

    def _name_matches(self, request, pattern):
        """Templates the caller can read that match today's type, storability, company and name
        clauses. ``active in (True, False)`` keeps an archived template, as today's search does."""

        return self.env[TEMPLATE_MODEL].search(
            [
                ("active", "in", [True, False]),
                ("type", "=", STORABLE_PRODUCT_TYPE),
                ("is_storable", "=", True),
                ("company_id", "in", [request.company_id, False]),
                ("name", "ilike", pattern),
            ],
            order="id",
            limit=MAX_TEMPLATE_MATCHES + 1,
        )

    def _variants(self, matched_templates):
        """The active variants, readable by the caller, of the matched templates."""

        return self.env[PRODUCT_MODEL].search(
            [("product_tmpl_id", "in", matched_templates.ids), ("active", "=", True)],
            order="id",
            limit=MAX_BRANCH_PRODUCTS + 1,
        )

    def _code_matches(self, pattern):
        """Active products the caller can read whose own code or barcode matches."""

        return self.env[PRODUCT_MODEL].search(
            [
                ("active", "=", True),
                "|",
                ("default_code", "ilike", pattern),
                ("barcode", "ilike", pattern),
            ],
            order="id",
            limit=MAX_BRANCH_PRODUCTS + 1,
        )
