from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    di_voucher_validity_months = fields.Integer(
        string="Refund Voucher Validity (months)", default=24,
        help="How long a gift card issued for a refund stays spendable. "
             "Set to 0 for no expiry.")
