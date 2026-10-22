from odoo import fields, models


class StockMove(models.Model):
    _inherit = "stock.move"

    barcode_qty_scanned = fields.Float(
        string="Scanned Quantity",
        digits="Product Unit",
        copy=False,
        help="Quantity scanned with the Barcode Scanner app. It is applied to the "
             "move quantity when the transfer is validated from the scanner.",
    )
