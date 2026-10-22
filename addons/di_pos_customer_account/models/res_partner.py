from collections import defaultdict

from odoo import api, models

INVOICE_TYPES = ("out_invoice", "out_refund")


class ResPartner(models.Model):
    _inherit = "res.partner"

    @api.model
    def _di_pending_settlement_orders(self, commercial_ids, company):
        """Orders with Customer Account payments not yet in the accounting
        (session not closed, order not invoiced)."""
        # pos.payment.method.type is computed, not stored: pay later is a method
        # without journal, narrowed down in Python (online payments have none either).
        orders = self.env["pos.order"].sudo().search([
            ("partner_id.commercial_partner_id", "in", commercial_ids),
            ("company_id", "=", company.id),
            ("session_id.state", "!=", "closed"),
            ("account_move", "=", False),
            ("payment_ids.payment_method_id.journal_id", "=", False),
        ])
        return orders.filtered(lambda order: any(
            payment.payment_method_id.type == "pay_later" for payment in order.payment_ids))

    @api.model
    def di_get_customer_balances(self, partner_ids, config_id=False):
        """What each customer owes (positive) or has in credit (negative).

        Returns {partner_id: {pos_due, invoices_due, total_due}}:
        - posted, unreconciled receivables, split between invoices and the rest
          (Customer Account sales, settlements, deposits);
        - plus the Customer Account payments of sessions still open.
        Read as superuser: a cashier has no access to the accounting, and this
        returns nothing but the totals.
        """
        config = self.env["pos.config"].browse(config_id).exists()
        company = config.company_id or self.env.company
        partners = self.browse(partner_ids).exists()
        commercial_ids = partners.commercial_partner_id.ids
        totals = defaultdict(lambda: {"pos_due": 0.0, "invoices_due": 0.0})

        lines = self.env["account.move.line"].sudo().search([
            ("partner_id", "in", commercial_ids),
            ("company_id", "=", company.id),
            ("account_id.account_type", "=", "asset_receivable"),
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
        ])
        for line in lines:
            key = "invoices_due" if line.move_id.move_type in INVOICE_TYPES else "pos_due"
            totals[line.partner_id.id][key] += line.amount_residual

        for order in self._di_pending_settlement_orders(commercial_ids, company):
            key = "invoices_due" if order.di_settle_kind == "invoices" else "pos_due"
            amount = sum(order.payment_ids.filtered(
                lambda p: p.payment_method_id.type == "pay_later").mapped("amount"))
            totals[order.partner_id.commercial_partner_id.id][key] += amount

        round_ = company.currency_id.round
        result = {}
        for partner in partners:
            due = totals[partner.commercial_partner_id.id]
            pos_due, invoices_due = round_(due["pos_due"]), round_(due["invoices_due"])
            result[partner.id] = {
                "pos_due": pos_due,
                "invoices_due": invoices_due,
                "total_due": round_(pos_due + invoices_due),
            }
        return result

    @api.model
    def di_get_open_invoices(self, partner_id, config_id=False):
        """The customer's unpaid invoices, minus those already being settled
        in a session that is still open."""
        config = self.env["pos.config"].browse(config_id).exists()
        company = config.company_id or self.env.company
        commercial = self.browse(partner_id).commercial_partner_id
        pending = set()
        for order in self._di_pending_settlement_orders(commercial.ids, company):
            if order.di_settle_kind == "invoices":
                pending |= set(order._di_settle_invoices().ids)
        invoices = self.env["account.move"].sudo().search([
            ("commercial_partner_id", "=", commercial.id),
            ("company_id", "=", company.id),
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial")),
            ("id", "not in", list(pending)),
        ], order="invoice_date_due, id")
        return [{
            "id": invoice.id,
            "name": invoice.name,
            "date": invoice.invoice_date and invoice.invoice_date.isoformat(),
            "date_due": invoice.invoice_date_due and invoice.invoice_date_due.isoformat(),
            "amount_total": invoice.amount_total,
            "amount_residual": invoice.amount_residual,
        } for invoice in invoices]
