from odoo import api, fields, models


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    di_issues_gift_card = fields.Boolean(
        string="Issues a Gift Card",
        help="Settling an order with this method issues a gift card for the "
             "amount instead of moving money.\n\n"
             "Meant for refunds: the negative amount is booked against this "
             "method's journal, which is where the liability for outstanding "
             "vouchers accumulates, and the customer leaves with a card "
             "rather than cash.\n\n"
             "On a refund, methods WITHOUT this flag are refused at the "
             "till, so a return cannot be paid out.")

    @api.model
    def _load_pos_data_fields(self, config):
        """The till needs the flag to know which method may settle a refund."""
        return super()._load_pos_data_fields(config) + ['di_issues_gift_card']
