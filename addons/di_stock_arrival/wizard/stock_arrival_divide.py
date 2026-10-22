from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class StockArrivalDivide(models.TransientModel):
    _name = 'di.stock.arrival.divide'
    _description = "Divide an Arrival Across Warehouses"

    arrival_id = fields.Many2one(
        'di.stock.arrival', string="Arrival", required=True,
        ondelete='cascade')
    company_id = fields.Many2one(related='arrival_id.company_id')
    arrival_warehouse_id = fields.Many2one(
        related='arrival_id.warehouse_id', string="Arrival Warehouse")
    warehouse_id = fields.Many2one(
        'stock.warehouse', string="Destination", required=True,
        check_company=True,
        domain="[('id', '!=', arrival_warehouse_id)]",
        help="Warehouse to send this share to.")
    method = fields.Selection(
        [('percent', "Percentage of what is left"),
         ('fixed', "Fixed quantity per line")],
        string="Distribute", default='percent', required=True)
    percentage = fields.Float(string="Percentage", default=100.0)
    quantity = fields.Float(
        string="Quantity per Line", default=1.0,
        digits='Product Unit')
    rounding = fields.Selection(
        [('down', "Round down"), ('nearest', "Round to nearest")],
        string="Rounding", default='down', required=True,
        help="Applied when a percentage does not divide evenly. Rounding "
             "down never distributes more than arrived.")
    only_undistributed = fields.Boolean(
        string="Skip Fully Distributed Lines", default=True)

    @api.constrains('percentage', 'method')
    def _check_percentage(self):
        for wizard in self:
            if wizard.method == 'percent' and not 0 < wizard.percentage <= 100:
                raise ValidationError(_(
                    "Percentage must be greater than 0 and at most 100."))

    def action_apply(self):
        self.ensure_one()
        arrival = self.arrival_id
        if arrival.state not in ('draft', 'to_divide'):
            raise UserError(_(
                "This arrival can no longer be divided."))
        if self.warehouse_id == arrival.warehouse_id:
            raise UserError(_(
                "Stock staying at %s needs no transfer - simply leave it "
                "undistributed.", arrival.warehouse_id.display_name))

        Allocation = self.env['di.stock.arrival.allocation']
        precision = self.env['decimal.precision'].precision_get(
            'Product Unit')
        touched = 0

        for line in arrival.line_ids:
            remaining = line.qty_remaining
            if self.only_undistributed and remaining <= 0:
                continue

            if self.method == 'percent':
                share = remaining * self.percentage / 100.0
                share = (int(share) if self.rounding == 'down'
                         else round(share, precision))
            else:
                share = min(self.quantity, remaining)

            if share <= 0:
                continue

            existing = line.allocation_ids.filtered(
                lambda a: a.warehouse_id == self.warehouse_id)
            if existing:
                existing.quantity += share
            else:
                Allocation.create({
                    'line_id': line.id,
                    'warehouse_id': self.warehouse_id.id,
                    'quantity': share,
                })
            touched += 1

        if not touched:
            raise UserError(_(
                "Nothing left to distribute on this arrival."))

        arrival.message_post(body=_(
            "Distributed %(count)s line(s) to %(warehouse)s.",
            count=touched, warehouse=self.warehouse_id.display_name))

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'di.stock.arrival',
            'res_id': arrival.id,
            'view_mode': 'form',
            'target': 'current',
        }
