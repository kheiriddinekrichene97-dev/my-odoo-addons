from dateutil.relativedelta import relativedelta

from odoo import _, fields, models
from odoo.exceptions import UserError


class PosOrder(models.Model):
    _inherit = 'pos.order'

    di_refund_voucher_id = fields.Many2one(
        'loyalty.card', string="Refund Voucher", readonly=True, copy=False,
        help="The gift card issued to settle this refund.")
    # Copied onto the order rather than read through the relation, because
    # the receipt is rendered in the browser from the order record the sync
    # sends back, and it cannot follow a many2one into loyalty.card.
    di_refund_voucher_code = fields.Char(
        string="Refund Voucher Code", readonly=True, copy=False,
        help="Printed on the receipt so the customer leaves holding the "
             "number. Odoo deliberately keeps gift card codes off receipts "
             "- a physical card already carries its number, and a receipt "
             "that showed it would be spendable by whoever picked it up. "
             "A refund voucher has no physical card, so the paper is the "
             "only thing the customer gets.")
    di_refund_voucher_expiry = fields.Date(
        string="Refund Voucher Expiry", readonly=True, copy=False)
    # Plain integers rather than relations: the till reads these straight off
    # the synced order, and a many2one would need loyalty.card and
    # ir.actions.report loaded into the point of sale to resolve.
    di_refund_voucher_card_id = fields.Integer(
        string="Refund Voucher Card ID", readonly=True, copy=False)
    di_refund_voucher_report_id = fields.Integer(
        string="Refund Voucher Report ID", readonly=True, copy=False,
        help="The gift card PDF report to print, taken from the loyalty "
             "programme's own communication plan - the same report Odoo "
             "prints when a card is sold.")

    def _process_saved_order(self, draft):
        result = super()._process_saved_order(draft)
        if not draft and self.state != 'cancel' and not self.di_refund_voucher_id:
            self._di_issue_refund_voucher()
        return result

    # ------------------------------------------------------------------
    def _di_voucher_amount(self):
        """What the card should be worth: whatever the order owes.

        Not the voucher payment line's own figure. On an exchange those
        differ, and taking the line at face value overpays the customer:
        returning a 14,95 item for a 9,95 one leaves the shop owing 5,00,
        but the voucher line can read -14,95 with a matching 9,95 taken in
        - a card worth three times the difference, and 9,95 of cash the
        till never saw. The order total is the one figure that cannot be
        wrong, because Odoo derives it from the lines.

        A voucher payment still has to be present: that is what says the
        customer chose a card rather than a refund, and it keeps an
        ordinary negative order from silently minting one.
        """
        self.ensure_one()
        used = self.payment_ids.filtered(
            lambda p: p.payment_method_id.di_issues_gift_card and p.amount < 0)
        if not used or self.amount_total >= 0:
            return 0.0
        return -self.amount_total

    def _di_issue_refund_voucher(self):
        """Issue the gift card that settles this refund.

        Guarded by di_refund_voucher_id rather than run blindly:
        _process_saved_order runs again on every later resync of an
        already-paid order - invoice generation, for one - and a customer
        must not collect a second voucher because the order was touched
        again.
        """
        self.ensure_one()
        amount = self._di_voucher_amount()
        if amount <= 0:
            return

        program = self.env['loyalty.program'].sudo().search([
            ('program_type', '=', 'gift_card'),
            ('active', '=', True),
        ], limit=1)
        if not program:
            # Refusing here would strand a paid order mid-sync with the
            # customer already gone. The order stands; the configuration
            # is what needs fixing, and the amount is recoverable from the
            # payment line.
            raise UserError(_(
                "This refund was settled with a voucher, but no active gift "
                "card programme is configured, so no card could be issued.\n\n"
                "Set one up under Point of Sale, then re-issue the card for "
                "%(amount)s manually.", amount=amount))

        months = (self.company_id or self.env.company).di_voucher_validity_months
        expiry = fields.Date.today() + relativedelta(months=months) if months else False

        # `code` fills itself: loyalty.card defaults it to _generate_code(),
        # so the number is in Odoo's own format rather than a second one
        # invented here.
        card = self.env['loyalty.card'].sudo().create({
            'program_id': program.id,
            'partner_id': self.partner_id.id or False,
            'points': amount,
            'expiration_date': expiry,
        })
        # The programme already says which PDF to print when a card is
        # created - reuse it, so a refund voucher comes out on exactly the
        # same paper as one that was sold.
        report = program.communication_plan_ids.filtered(
            lambda c: c.trigger == 'create').pos_report_print_id[:1]

        self.write({
            'di_refund_voucher_id': card.id,
            'di_refund_voucher_code': card.code,
            'di_refund_voucher_expiry': card.expiration_date,
            'di_refund_voucher_card_id': card.id,
            'di_refund_voucher_report_id': report.id if report else 0,
        })
