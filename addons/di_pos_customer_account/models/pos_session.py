from odoo import models


class PosSession(models.Model):
    _inherit = "pos.session"

    def _create_pay_later_receivable_lines(self, data):
        """Remember which receivable line each settlement payment became.

        Odoo creates the combined lines first, then one line per "split"
        (identified customer) payment, in that order.
        """
        data = super()._create_pay_later_receivable_lines(data)
        combined = len(data.get("combine_receivables_pay_later") or {})
        split_payments = list((data.get("split_receivables_pay_later") or {}).keys())
        split_lines = data["pay_later_move_lines"][combined:]
        data["di_settlement_lines"] = {
            payment: line for payment, line in zip(split_payments, split_lines)
            if payment.pos_order_id.di_settle_kind and payment.amount < 0
        }
        return data

    def _reconcile_account_move_lines(self, data):
        """Match each settlement's credit with what it pays.

        Settle Debt: the customer's oldest open receivables that are not
        invoices (i.e. earlier Customer Account sales). Settle Invoices: the
        invoices chosen at the till. A deposit stays open as a credit.
        """
        data = super()._reconcile_account_move_lines(data)
        MoveLine = self.env["account.move.line"]
        for payment, credit_line in (data.get("di_settlement_lines") or {}).items():
            order = payment.pos_order_id
            account = credit_line.account_id
            if credit_line.reconciled or not account.reconcile or credit_line.move_id.state != "posted":
                continue
            if order.di_settle_kind == "invoices":
                debits = order._di_settle_invoices().line_ids.filtered(
                    lambda line: line.account_id == account and not line.reconciled and line.balance > 0)
            elif order.di_settle_kind == "debt":
                debits = MoveLine.search([
                    ("partner_id", "=", credit_line.partner_id.id),
                    ("account_id", "=", account.id),
                    ("parent_state", "=", "posted"),
                    ("reconciled", "=", False),
                    ("balance", ">", 0),
                    ("move_id.move_type", "not in", ("out_invoice", "out_refund")),
                ], order="date, id")
            else:
                continue
            if debits:
                (credit_line | debits).reconcile()
        return data
