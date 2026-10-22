from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    bonus_point_batch_ids = fields.One2many(
        'bonus.point.batch', 'partner_id', string='Bonus Point Batches')
    bonus_points_available = fields.Integer(
        string='Bonus Points Available', compute='_compute_bonus_points')
    bonus_points_next_expiry_date = fields.Datetime(
        string='Next Points Expiry', compute='_compute_bonus_points')
    bonus_points_next_expiry_amount = fields.Integer(
        string='Points Expiring Next', compute='_compute_bonus_points')

    @api.model
    def _load_pos_data_fields(self, config):
        fields = super()._load_pos_data_fields(config)
        return fields + ['bonus_points_available']

    @api.depends(
        'bonus_point_batch_ids.points_remaining',
        'bonus_point_batch_ids.expiry_date',
    )
    def _compute_bonus_points(self):
        Batch = self.env['bonus.point.batch'].sudo()
        now = fields.Datetime.now()
        for partner in self:
            batches = Batch.search([
                ('partner_id', '=', partner.id),
                ('points_remaining', '>', 0),
                ('expiry_date', '>', now),
            ], order='expiry_date')
            partner.bonus_points_available = sum(batches.mapped('points_remaining'))
            if batches:
                partner.bonus_points_next_expiry_date = batches[0].expiry_date
                partner.bonus_points_next_expiry_amount = batches[0].points_remaining
            else:
                partner.bonus_points_next_expiry_date = False
                partner.bonus_points_next_expiry_amount = 0
