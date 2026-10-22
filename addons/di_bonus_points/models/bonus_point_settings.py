from odoo import api, fields, models


class BonusPointSettings(models.Model):
    """Scalar, per-company configuration for the bonus points engine.

    One record per company, auto-created on first access via get_settings().
    """
    _name = 'bonus.point.settings'
    _inherit = ['pos.load.mixin']
    _description = 'Bonus Points - Settings'

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)
    point_value = fields.Float(
        string='Point Value (€)', default=5.0, required=True,
        help="Monetary reduction granted by spending one point.")
    earn_divisor = fields.Float(
        string='Earning Divisor (€)', default=5.0, required=True,
        help="One point is earned for every this many euros paid "
             "(after all discounts, including points spent).")
    expiry_days = fields.Integer(
        string='Expiry (days)', default=30, required=True,
        help="Number of days after which an earned batch of points expires. "
             "Exactly N days after the earn date, not 'next month'.")
    promo_pricelist_id = fields.Many2one(
        'product.pricelist', string='Promo Pricelist',
        help="Reference pricelist for the bonus-points eligibility check: "
             "an item's discount %% is now (Sales Price - price on this "
             "pricelist) / Sales Price, compared to its category's max "
             "(Donna/Uomo). Falls back to the Sales Price itself (0%% "
             "discount) if left empty.")

    _company_uniq = models.Constraint(
        'unique(company_id)',
        'Only one settings record is allowed per company.',
    )

    @api.model
    def _load_pos_data_fields(self, config):
        return ['id', 'company_id', 'point_value', 'earn_divisor', 'expiry_days']

    @api.model
    def get_settings_readonly(self, company=None):
        """Settings for this company, or an empty recordset - never created.

        get_settings() creates the record on first use, which is right for
        the POS flows that need somewhere to write. It must NOT be called
        from a compute: bonus_ba_code puts one of these computes on the
        product form, so it now runs whenever anybody opens any product,
        and a plain read should never write.
        """
        company = company or self.env.company
        return self.sudo().search([('company_id', '=', company.id)], limit=1)

    @api.model
    def get_settings(self, company=None):
        """Return this company's settings record, creating it with defaults
        on first use."""
        company = company or self.env.company
        settings = self.sudo().search([('company_id', '=', company.id)], limit=1)
        if not settings:
            settings = self.sudo().create({'company_id': company.id})
        return settings
