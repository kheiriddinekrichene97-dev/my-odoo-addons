from odoo import fields, models


class BonusPointMovement(models.Model):
    """Full audit ledger: every earn, spend, expire and refund-related
    event is recorded here, one row per batch touched."""
    _name = 'bonus.point.movement'
    _description = 'Bonus Points - Movement Ledger'
    _order = 'date desc, id desc'

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)
    batch_id = fields.Many2one(
        'bonus.point.batch', required=True, ondelete='cascade', index=True)
    partner_id = fields.Many2one(
        'res.partner', related='batch_id.partner_id', store=True, index=True)
    movement_type = fields.Selection([
        ('earn', 'Earn'),
        ('spend', 'Spend'),
        ('expire', 'Expire'),
        ('refund_return', 'Refund - Points Returned'),
        ('refund_reverse_earn', 'Refund - Earned Points Reversed'),
    ], required=True)
    points = fields.Integer(
        required=True,
        help="Always a positive count; the movement_type gives the direction.")
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    pos_order_id = fields.Many2one('pos.order', string='POS Order')
    pos_order_line_id = fields.Many2one('pos.order.line', string='POS Order Line')
