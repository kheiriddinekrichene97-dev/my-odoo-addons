from odoo import api, fields, models


class PosOrderLine(models.Model):
    _inherit = 'pos.order.line'

    # Snapshotted BEFORE any bonus points are applied - this is what the
    # eligibility gate and bracket lookup are evaluated against, so that
    # applying points can never retroactively change the item's own
    # eligibility (see module spec, Q2/Q3 clarification).
    bonus_campaign_discount_pct = fields.Float(
        string='Campaign Discount %',
        help="Discount % vs. list price at the Dettaglio price, snapshotted "
             "before bonus points are applied.")
    bonus_eligible = fields.Boolean(
        string='Eligible for Bonus Points',
        help="Whether this line's campaign discount is within its "
             "category's max discount rate.")
    bonus_max_points_per_unit = fields.Integer(string='Max Bonus Points / Unit')
    bonus_points_applied = fields.Integer(
        string='Bonus Points Applied',
        help="Total points spent on this line, across all units.")
    bonus_reduction_amount = fields.Float(
        string='Bonus Points Reduction (€)',
        help="Monetary amount this line's price was reduced by via spent "
             "bonus points. Applied directly to this line's price; the "
             "Dettaglio pricelist itself is never modified.")
    bonus_movement_ids = fields.One2many(
        'bonus.point.movement', 'pos_order_line_id', string='Bonus Point Movements')
    bonus_points_ledger_consumed = fields.Integer(
        string='Bonus Points Ledger Consumed', copy=False,
        help="How many points have actually been drawn from the customer's "
             "real batches for this line so far. Backend-only bookkeeping, "
             "never sent to the POS frontend: the frontend owns "
             "bonus_points_applied/price_unit (it must work offline), this "
             "field is what the backend reconciles it against at order "
             "validation time so points are never double-spent.")

    @api.model
    def _load_pos_data_fields(self, config):
        fields_list = super()._load_pos_data_fields(config)
        return fields_list + [
            'bonus_campaign_discount_pct', 'bonus_eligible',
            'bonus_max_points_per_unit', 'bonus_points_applied',
            'bonus_reduction_amount',
        ]
