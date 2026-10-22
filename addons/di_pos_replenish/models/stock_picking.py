from odoo import fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    di_pos_replenish_id = fields.Many2one(
        'di.pos.replenish', string="Sales Replenishment", readonly=True,
        index=True, ondelete='set null',
        help="Sales replenishment run this transfer was generated from.")
