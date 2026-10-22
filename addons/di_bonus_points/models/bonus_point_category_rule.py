from odoo import api, fields, models


class BonusPointCategoryRule(models.Model):
    """Configurable link between a group of Reparto values and the maximum
    discount % an item can already have and still be eligible for bonus
    point redemption (e.g. Donna -> 50%, Uomo -> 30%).

    New categories (kids, accessories, ...) are added as new records,
    never as code changes.
    """
    _name = 'bonus.point.category.rule'
    _inherit = ['pos.load.mixin']
    _description = 'Bonus Points - Category Eligibility Rule'

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)
    name = fields.Char(required=True)
    reparto_ids = fields.Many2many(
        'x_reparto', string='Reparto',
        help="Department(s) (Reparto) this eligibility rule applies to.")
    max_discount_rate = fields.Float(
        string='Max Discount Rate (%)', required=True,
        help="Maximum discount percentage (vs. list price, before any bonus "
             "points are applied) an item can have and still be eligible "
             "for bonus point redemption. Above this, the item earns 0 "
             "spendable points regardless of price.")
    allow_manager_override = fields.Boolean(
        string='Allow Manager Override', default=False,
        help="Reserved for future use: let a manager bypass this threshold "
             "for a specific sale. Not implemented in this version.")
    active = fields.Boolean(default=True)

    @api.model
    def _load_pos_data_fields(self, config):
        return ['id', 'company_id', 'name', 'reparto_ids', 'max_discount_rate', 'active']

    @api.model
    def get_rule_for_reparto(self, reparto, company=None):
        """Return the (single) active rule covering this Reparto record, or
        an empty recordset if none matches (i.e. not part of any bonus
        campaign)."""
        company = company or self.env.company
        if not reparto:
            return self.browse()
        # There is a handful of these (one per group of Reparto values -
        # Donna, Uomo, ...), so reading them all and matching in Python
        # costs the same as a filtered search, and lets the batch path
        # (bonus.point.service.get_ba_snapshot) reuse the same matching
        # instead of restating it as a domain.
        rules = self.sudo().search([
            ('company_id', '=', company.id),
            ('active', '=', True),
        ])
        return rules._rule_for_reparto(reparto)

    def _rule_for_reparto(self, reparto):
        """First of these already-read rules covering `reparto`, or an
        empty recordset."""
        if not reparto:
            return self.browse()
        for rule in self:
            if reparto.id in rule.reparto_ids.ids:
                return rule
        return self.browse()
