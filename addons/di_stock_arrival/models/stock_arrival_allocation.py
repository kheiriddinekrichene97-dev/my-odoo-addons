from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StockArrivalAllocation(models.Model):
    _name = 'di.stock.arrival.allocation'
    _description = "Stock Arrival Allocation"
    _order = 'arrival_id, warehouse_id, id'

    line_id = fields.Many2one(
        'di.stock.arrival.line', string="Arrival Line", required=True,
        ondelete='cascade', index=True)
    arrival_id = fields.Many2one(
        related='line_id.arrival_id', store=True, index=True, string="Arrival")
    company_id = fields.Many2one(
        related='line_id.company_id', store=True, index=True)
    state = fields.Selection(related='arrival_id.state', store=True)

    product_id = fields.Many2one(
        related='line_id.product_id', store=True, string="Product")
    warehouse_id = fields.Many2one(
        'stock.warehouse', string="Destination", required=True,
        check_company=True,
        help="Warehouse this share of the line is transferred to. Shops "
             "(PV) and stockrooms (MAG) are both valid destinations.")
    quantity = fields.Float(
        string="Quantity", required=True, default=0.0,
        digits='Product Unit')

    # Mirrored from the line so the worker sees what arrived and what is
    # still unassigned on the row being filled in, rather than having to
    # switch back to the Products Received tab and add it up.
    line_quantity = fields.Float(
        related='line_id.quantity', string="Received",
        digits='Product Unit')
    line_qty_remaining = fields.Float(
        related='line_id.qty_remaining', string="Still Unassigned",
        digits='Product Unit',
        help="Of what arrived, how much is not yet assigned to any "
             "destination. Distributing everything is not required: what is "
             "left simply stays at the arrival warehouse.")

    _line_warehouse_uniq = models.Constraint(
        'unique(line_id, warehouse_id)',
        "This product is already distributed to that warehouse on this "
        "arrival. Edit the existing row instead of adding a second one.",
    )
    _quantity_positive = models.Constraint(
        'CHECK(quantity >= 0)',
        "A distributed quantity cannot be negative.",
    )

    @api.constrains('quantity', 'line_id')
    def _check_line_not_over_allocated(self):
        """Total up the line and refuse to send out more than arrived.

        The line carries the same check, but a constraint declared on its
        ``allocation_ids`` only runs when that one2many is written. Rows added
        or edited in the Distribution tab write this model instead, so without
        this the check never ran for them.
        """
        self.line_id._check_not_over_allocated()

    @api.onchange('quantity')
    def _onchange_quantity(self):
        """Warn as soon as one row alone exceeds the whole line.

        Deliberately narrow: the unsaved sibling rows of the tab are not
        readable from here, so a total across destinations cannot be trusted
        yet - that is what the constraint above is for. A single row larger
        than the line, though, is always wrong.
        """
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        line = self.line_id
        if not line or not self.quantity:
            return
        if round(self.quantity - line.quantity, precision) > 0:
            return {'warning': {
                'title': _("More than arrived"),
                'message': _(
                    "Only %(received)s of %(product)s arrived, but this row "
                    "sends out %(quantity)s.",
                    received=line.quantity,
                    product=line.product_id.display_name,
                    quantity=self.quantity),
            }}

    @api.constrains('warehouse_id')
    def _check_destination_differs(self):
        for allocation in self:
            if allocation.warehouse_id == allocation.arrival_id.warehouse_id:
                raise ValidationError(_(
                    "The destination cannot be the arrival warehouse itself. "
                    "Stock that stays at %s needs no transfer - just leave it "
                    "undistributed.",
                    allocation.arrival_id.warehouse_id.display_name))
