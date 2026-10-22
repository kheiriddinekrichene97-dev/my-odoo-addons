from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    di_arrival_warehouse_id = fields.Many2one(
        related='company_id.di_arrival_warehouse_id',
        string="Arrival Warehouse",
        readonly=False,
    )
