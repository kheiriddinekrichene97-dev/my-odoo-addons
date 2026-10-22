from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class PosReplenishAllocation(models.Model):
    _name = 'di.pos.replenish.allocation'
    _description = "Sales Replenishment Allocation"
    _order = 'line_id, warehouse_id, id'

    line_id = fields.Many2one(
        'di.pos.replenish.line', string="Line", required=True,
        ondelete='cascade', index=True)
    replenish_id = fields.Many2one(
        related='line_id.replenish_id', store=True, index=True,
        string="Replenishment")
    company_id = fields.Many2one(
        related='line_id.company_id', store=True, index=True)
    product_id = fields.Many2one(
        related='line_id.product_id', store=True, string="Product")

    warehouse_id = fields.Many2one(
        'stock.warehouse', string="Source", required=True,
        check_company=True,
        help="Which source warehouse this share of the row comes out of.")
    quantity = fields.Float(
        string="Quantity", required=True, default=0.0, digits='Product Unit')

    _line_warehouse_uniq = models.Constraint(
        'unique(line_id, warehouse_id)',
        "That source is already on this row. Edit the quantity instead of "
        "adding a second one.",
    )
    _quantity_positive = models.Constraint(
        'CHECK(quantity >= 0)',
        "A quantity to send cannot be negative.",
    )

    @api.constrains('quantity', 'line_id')
    def _check_not_over_sold(self):
        """Never send a shop more than it sold.

        The point of the run is to put back what went out. Sending more is
        not replenishment, it is a distribution decision, and di_stock_arrival
        is where that is made.
        """
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        for allocation in self:
            line = allocation.line_id
            total = sum(line.allocation_ids.mapped('quantity'))
            if round(total - line.net_sold_qty, precision) > 0:
                raise ValidationError(_(
                    "%(product)s at %(config)s: %(total)s to send but only "
                    "%(sold)s sold. A run puts back what went out; to send "
                    "more, use an arrival distribution.",
                    product=line.product_id.display_name,
                    config=line.config_id.name,
                    total=total,
                    sold=line.net_sold_qty))
