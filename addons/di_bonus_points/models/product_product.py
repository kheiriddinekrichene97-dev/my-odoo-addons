from odoo import api, fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    bonus_promo_price = fields.Float(
        string='Promo Price (Bonus Points)', compute='_compute_bonus_promo_price',
        help="This variant's price under bonus.point.settings."
             "promo_pricelist_id - the reference price the bonus-points "
             "eligibility gate compares against Sales Price, instead of "
             "whatever is actually charged on the order line. Falls back "
             "to Sales Price itself (0% discount) if no Promo pricelist "
             "is configured.")

    def _compute_bonus_promo_price(self):
        settings = self.env['bonus.point.settings'].get_settings_readonly()
        pricelist = settings.promo_pricelist_id
        prices = {}
        if pricelist:
            try:
                # One call for the whole recordset: POS loads this field for
                # every product it preloads (thousands), where per-record
                # pricing added seconds to session start.
                prices = pricelist._get_products_price(self, 1.0)
            except Exception:
                prices = {}
        for product in self:
            product.bonus_promo_price = prices.get(product.id, product.lst_price)

    # ------------------------------------------------------------------
    # BA code
    # ------------------------------------------------------------------
    bonus_ba_code = fields.Char(
        string='Bonus Points (BA)', compute='_compute_bonus_ba_code',
        help="How many bonus points this variant is worth, in the form "
             "printed on the cartellino (0BA ... 4BA). Computed on the "
             "variant's own price rather than the template's: a size or "
             "colour carrying an attribute price extra can land in a "
             "different price bracket than its siblings, so this - not "
             "product.template.bonus_ba_code - is the number a tag must "
             "print. Deliberately not stored: the value also depends on "
             "the Promo pricelist, the brackets and the category rules, "
             "none of which can be expressed as an @api.depends on the "
             "product, so a stored copy would go stale unnoticed.")

    def _bonus_sales_price(self):
        """The Sales Price the bonus rules compare the Promo Price against.

        lst_price, not the template's list_price: it includes this
        variant's attribute price extra.
        """
        self.ensure_one()
        return self.lst_price or 0.0

    def _compute_bonus_ba_code(self):
        try:
            # One read of the bracket table and the category rules for the
            # whole recordset, not two searches per variant - a cartellino
            # run prints hundreds of these at a time.
            snapshot = self.env['bonus.point.service'].get_ba_snapshot(self)
        except Exception:
            # Same reasoning as product.template._compute_bonus_eligibility:
            # a variant form, and a tag print, must both survive the bonus
            # points configuration being missing or unreadable.
            snapshot = {}
        for product in self:
            _discount_pct, _eligible, points = snapshot.get(
                product.id, (0.0, False, 0))
            product.bonus_ba_code = '%dBA' % points

    @api.model
    def _load_pos_data_fields(self, config):
        fields_list = super()._load_pos_data_fields(config)
        return fields_list + ['bonus_promo_price']
