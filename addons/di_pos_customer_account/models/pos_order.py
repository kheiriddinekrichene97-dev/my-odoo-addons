from odoo import fields, models


class PosOrder(models.Model):
    _inherit = "pos.order"

    di_settle_kind = fields.Selection(
        [("debt", "Settle Debt"), ("invoices", "Settle Invoices"), ("deposit", "Deposit")],
        string="Customer Account Operation", readonly=True, copy=False,
        help="Set on a settlement order: the money taken in is credited to the "
             "customer's account (negative Customer Account payment).")
    # Plain ids: account.move is not a model the till loads, so a relational
    # field could not be synced from it.
    di_settle_invoice_ids = fields.Char(
        string="Invoices to Settle", readonly=True, copy=False,
        help="Comma-separated ids of the invoices this settlement pays.")

    def _di_settle_invoices(self):
        self.ensure_one()
        ids = [int(i) for i in (self.di_settle_invoice_ids or "").split(",") if i.strip().isdigit()]
        return self.env["account.move"].browse(ids).exists()
