from odoo import api, fields, models


class PosOrderLine(models.Model):
    _inherit = 'pos.order.line'

    salesperson_id = fields.Many2one(
        'hr.employee', string='Salesperson', index='btree_not_null',
        help="Employee who assisted the customer on THIS product - separate "
             "from the cashier who rang the sale up. One receipt can carry "
             "several, one per line. Optional: a line may stay untagged.")

    @api.model
    def _load_pos_data_fields(self, config):
        """pos.order.line - unlike pos.order - declares an explicit field
        list, so a new field reaches the till only by being appended here."""
        return super()._load_pos_data_fields(config) + ['salesperson_id']

    @api.model_create_multi
    def create(self, vals_list):
        """A returned line is credited back to whoever sold it.

        Done server-side rather than in the browser because the till builds
        a refund line by copying the original record directly, bypassing
        the path that applies the current salesperson - and because a refund
        created from the back office has to behave the same way.
        """
        for vals in vals_list:
            origin_id = vals.get('refunded_orderline_id')
            if origin_id and not vals.get('salesperson_id'):
                origin = self.browse(origin_id)
                if origin.exists() and origin.salesperson_id:
                    vals['salesperson_id'] = origin.salesperson_id.id
        return super().create(vals_list)
