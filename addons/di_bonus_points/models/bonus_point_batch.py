from datetime import timedelta

from odoo import api, fields, models


class BonusPointBatch(models.Model):
    """One earning event = one batch. Batches never merge: a partner can
    hold several batches at once, each with its own expiry date."""
    _name = 'bonus.point.batch'
    _description = 'Bonus Points - Earned Batch'
    _order = 'expiry_date'

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)
    partner_id = fields.Many2one('res.partner', required=True, index=True)
    points_earned = fields.Integer(required=True)
    points_remaining = fields.Integer(required=True)
    earn_date = fields.Datetime(required=True, default=fields.Datetime.now)
    expiry_date = fields.Datetime(required=True, index=True)
    source_pos_order_id = fields.Many2one('pos.order', string='Source POS Order')
    movement_ids = fields.One2many('bonus.point.movement', 'batch_id')
    state = fields.Selection([
        ('active', 'Active'),
        ('depleted', 'Depleted'),
        ('expired', 'Expired'),
    ], compute='_compute_state', store=True)

    @api.depends('points_remaining', 'expiry_date')
    def _compute_state(self):
        now = fields.Datetime.now()
        for rec in self:
            if rec.points_remaining <= 0:
                rec.state = 'depleted'
            elif rec.expiry_date and rec.expiry_date < now:
                rec.state = 'expired'
            else:
                rec.state = 'active'

    @api.model
    def create_batch(self, partner, points, source_order=None, company=None):
        """Create a new earn batch + its 'earn' movement. Returns the batch
        (empty recordset if points <= 0)."""
        if points <= 0:
            return self.browse()
        company = company or self.env.company
        settings = self.env['bonus.point.settings'].get_settings(company)
        earn_date = fields.Datetime.now()
        expiry_date = earn_date + timedelta(days=settings.expiry_days)
        batch = self.sudo().create({
            'company_id': company.id,
            'partner_id': partner.id,
            'points_earned': points,
            'points_remaining': points,
            'earn_date': earn_date,
            'expiry_date': expiry_date,
            'source_pos_order_id': source_order.id if source_order else False,
        })
        self.env['bonus.point.movement'].sudo().create({
            'company_id': company.id,
            'batch_id': batch.id,
            'movement_type': 'earn',
            'points': points,
            'pos_order_id': source_order.id if source_order else False,
        })
        return batch

    def _available_domain(self):
        return [
            ('points_remaining', '>', 0),
            ('expiry_date', '>', fields.Datetime.now()),
        ]
