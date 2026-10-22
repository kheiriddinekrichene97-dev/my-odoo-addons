from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    di_voucher_validity_months = fields.Integer(
        related='company_id.di_voucher_validity_months', readonly=False)
