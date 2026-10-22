from odoo import fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    di_replenish_id = fields.Many2one(
        'di.stock.replenish', string="Replenishment", readonly=True,
        index=True, ondelete='set null',
        help="Replenishment document this transfer was generated from.")
