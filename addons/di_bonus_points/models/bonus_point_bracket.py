from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class BonusPointBracket(models.Model):
    """Configurable price -> points table.

    Each row explicitly states whether its own lower and upper bound are
    inclusive, so the deliberate asymmetry in the business rules (e.g. an
    item at exactly 14.95 gets 0 points, one at exactly 90.00 gets 3) is
    data, not a hardcoded special case in code.
    """
    _name = 'bonus.point.bracket'
    _inherit = ['pos.load.mixin']
    _description = 'Bonus Points - Price Bracket'
    _order = 'sequence, price_from'

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)
    sequence = fields.Integer(default=10)
    name = fields.Char(compute='_compute_name', store=True)
    price_from = fields.Float(string='From (€)', required=True)
    price_from_inclusive = fields.Boolean(string='From Inclusive', default=True)
    price_to = fields.Float(string='To (€)')
    price_to_inclusive = fields.Boolean(string='To Inclusive', default=False)
    no_upper_limit = fields.Boolean(
        string='No Upper Limit',
        help="Check for the top bracket (e.g. 150 € and above); 'To' is "
             "ignored when this is set.")
    points = fields.Integer(required=True)
    active = fields.Boolean(default=True)

    @api.depends('price_from', 'price_from_inclusive', 'price_to',
                 'price_to_inclusive', 'no_upper_limit', 'points')
    def _compute_name(self):
        for rec in self:
            low = '[' if rec.price_from_inclusive else '('
            if rec.no_upper_limit:
                rec.name = "%s%.2f €, ∞) → %d pts" % (low, rec.price_from, rec.points)
            else:
                high = ']' if rec.price_to_inclusive else ')'
                rec.name = "%s%.2f €, %.2f €%s → %d pts" % (
                    low, rec.price_from, rec.price_to, high, rec.points)

    @api.constrains('price_from', 'price_to', 'no_upper_limit')
    def _check_range(self):
        for rec in self:
            if not rec.no_upper_limit and rec.price_to <= rec.price_from:
                raise ValidationError(_(
                    "The 'To' price must be greater than the 'From' price "
                    "(bracket: %s).") % rec.name)

    @api.model
    def _load_pos_data_fields(self, config):
        return [
            'id', 'company_id', 'sequence', 'price_from', 'price_from_inclusive',
            'price_to', 'price_to_inclusive', 'no_upper_limit', 'points', 'active',
        ]

    @api.model
    def get_points_for_price(self, price, company=None):
        """Return the points earned by an item at this (post-discount)
        price, or 0 if no configured bracket matches."""
        company = company or self.env.company
        brackets = self.sudo().search([
            ('company_id', '=', company.id), ('active', '=', True)])
        return brackets._points_for_price(price)

    def _points_for_price(self, price):
        """The same lookup, against brackets that have already been read.

        Split out of get_points_for_price so a caller pricing a whole
        recordset (bonus.point.service.get_ba_snapshot - the BA code on a
        product list, a cartellino print run) reads this table once instead
        of once per product.
        """
        for bracket in self:
            if price < bracket.price_from:
                continue
            if price == bracket.price_from and not bracket.price_from_inclusive:
                continue
            if bracket.no_upper_limit:
                return bracket.points
            if price > bracket.price_to:
                continue
            if price == bracket.price_to and not bracket.price_to_inclusive:
                continue
            return bracket.points
        return 0
