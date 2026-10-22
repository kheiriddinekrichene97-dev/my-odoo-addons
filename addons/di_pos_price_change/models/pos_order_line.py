from odoo import api, fields, models


class PosOrderLine(models.Model):
    _inherit = 'pos.order.line'

    di_price_note = fields.Text(
        string="Price Change Reason", readonly=True,
        help="Why the cashier changed this product's price or discount. "
             "Several changes on the same line are kept one per line.")
    di_original_price_unit = fields.Float(
        string="Original Price", digits='Product Price', readonly=True,
        help="The price before the first change made by hand.")
    di_original_discount = fields.Float(
        string="Original Discount (%)", digits='Discount', readonly=True)
    di_price_changed_by_id = fields.Many2one(
        'hr.employee', string="Changed By", readonly=True)
    # Stored so the Price Changes list can be filtered and grouped by shop.
    di_config_id = fields.Many2one(
        related='order_id.config_id', store=True, string="Point of Sale")

    @api.model
    def _load_pos_data_fields(self, config):
        """pos.order.line declares an explicit field list - a field the till
        does not load is one it will not send back either, so a reason typed
        at the till would be silently dropped on sync."""
        return super()._load_pos_data_fields(config) + [
            'di_price_note', 'di_original_price_unit',
            'di_original_discount', 'di_price_changed_by_id',
        ]
