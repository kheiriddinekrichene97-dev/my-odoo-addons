from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pos_di_allow_price_change = fields.Boolean(
        related='pos_config_id.di_allow_price_change', readonly=False)
