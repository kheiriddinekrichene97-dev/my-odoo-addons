from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StockReplenishLine(models.Model):
    _name = 'di.stock.replenish.line'
    _description = "Replenishment Line"
    _order = 'warehouse_id, id'
    _rec_name = 'product_id'
    _rec_names_search = ['product_id']

    replenish_id = fields.Many2one(
        'di.stock.replenish', string="Replenishment", required=True,
        ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='replenish_id.company_id', store=True, index=True)
    state = fields.Selection(related='replenish_id.state', store=True)

    product_id = fields.Many2one(
        'product.product', string="Product", required=True,
        check_company=True, domain="[('is_storable', '=', True)]")
    default_code = fields.Char(
        related='product_id.default_code', string="Internal Reference")
    barcode = fields.Char(related='product_id.barcode', string="Barcode")
    warehouse_id = fields.Many2one(
        'stock.warehouse', string="Destination", required=True,
        check_company=True,
        help="Shop or stockroom this quantity is sent to.")
    quantity = fields.Float(
        string="Quantity", required=True, default=0.0,
        digits='Product Unit')

    _product_warehouse_uniq = models.Constraint(
        'unique(replenish_id, product_id, warehouse_id)',
        "This product is already going to that destination on this "
        "document. Edit the existing row instead of adding a second one.",
    )
    _quantity_positive = models.Constraint(
        'CHECK(quantity > 0)',
        "A quantity to send must be greater than zero.",
    )

    @api.constrains('warehouse_id')
    def _check_destination_differs(self):
        for line in self:
            if line.warehouse_id in line.replenish_id.source_warehouse_ids:
                raise ValidationError(_(
                    "The destination cannot be the source warehouse itself "
                    "(%s).", line.warehouse_id.display_name))
