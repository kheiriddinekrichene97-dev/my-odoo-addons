from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Template-level twin of product.product's bonus_promo_price, for the
    # POS catalogue grid (which renders one card per template, before a
    # variant is picked) - see di_bonus_points/static/.../product_card.js.
    # Line-level eligibility (in the cart) still uses the variant's own
    # bonus_promo_price, computed identically.
    bonus_promo_price = fields.Float(
        string='Promo Price (Bonus Points)', compute='_compute_bonus_promo_price',
        help="Same as product.product.bonus_promo_price, computed at the "
             "template level for the POS product grid.")

    def _compute_bonus_promo_price(self):
        # Read-only: this runs whenever a product form is opened, and a read
        # must not create a settings row (see get_settings_readonly).
        settings = self.env['bonus.point.settings'].get_settings_readonly()
        pricelist = settings.promo_pricelist_id
        prices = {}
        if pricelist:
            try:
                # Price the whole recordset in one call. Doing it per record
                # costs ~1.7ms each, which is invisible on a form and very
                # visible everywhere else: POS preloads thousands of products
                # and reads this field for every one of them.
                prices = pricelist._get_products_price(self, 1.0)
            except Exception:
                # Pricing against a pricelist can fail on its own terms
                # (another company's currency with no rate, for one). Falling
                # back to the Sales Price yields a 0% discount - simply "no
                # campaign" - never a broken product form.
                prices = {}
        for template in self:
            template.bonus_promo_price = prices.get(template.id, template.list_price)

    # ------------------------------------------------------------------
    # "Which products currently earn points" backend report
    # ------------------------------------------------------------------
    bonus_discount_pct = fields.Float(
        string='Bonus Discount %', compute='_compute_bonus_eligibility',
        search='_search_bonus_points_eligible_noop',
        help="(Sales Price - Promo Price) / Sales Price - the same "
             "calculation the bonus-points eligibility gate uses.")
    bonus_ba_code = fields.Char(
        string='Bonus Points (BA)', compute='_compute_bonus_eligibility',
        help="How many bonus points this item is worth, in the form printed "
             "on the price tag (0BA ... 4BA). Taken from the price bracket "
             "matching its Promo Price, or 0BA when the item isn't eligible "
             "at all (too discounted for its Reparto, per the category rule).")
    bonus_points_eligible = fields.Boolean(
        string='Bonus Points Eligible', compute='_compute_bonus_eligibility',
        search='_search_bonus_points_eligible',
        help="Whether this product currently qualifies for bonus points: "
             "within its Reparto's Donna/Uomo discount cap AND its Promo "
             "Price actually lands in a non-zero price bracket (an item "
             "under the lowest bracket's floor would earn/redeem 0 points "
             "either way, so isn't worth flagging as eligible here).")

    def _bonus_sales_price(self):
        """The Sales Price the bonus rules compare the Promo Price against.

        product.product overrides this with lst_price, which adds the
        variant's attribute price extra - see there.
        """
        self.ensure_one()
        return self.list_price or 0.0

    def _compute_bonus_eligibility(self):
        Service = self.env['bonus.point.service']
        try:
            # One read of the bracket table and the category rules for the
            # whole recordset, not two searches per product.
            snapshot = Service.get_ba_snapshot(self)
        except Exception:
            # bonus_ba_code sits on the product form, so this compute runs
            # for every user who opens any product - including ones with no
            # business near the bonus points configuration. Show "no points"
            # rather than making the product form unopenable.
            snapshot = {}
        for template in self:
            if template.id in snapshot:
                discount_pct, eligible, points = snapshot[template.id]
            else:
                # No usable configuration, but the discount % is pure
                # arithmetic on the product's own prices, so still show it.
                discount_pct = Service.compute_campaign_discount_pct(
                    template._bonus_sales_price(),
                    template.bonus_promo_price or 0.0)
                eligible, points = False, 0
            template.bonus_discount_pct = discount_pct
            template.bonus_points_eligible = bool(eligible and points)
            template.bonus_ba_code = '%dBA' % points

    @api.model
    def _search_bonus_points_eligible(self, operator, value):
        """Not stored (the Promo pricelist can change independently of the
        product record, which would leave a stored value silently stale) -
        so filtering/grouping by it in a list view instead re-evaluates
        eligibility for every product and returns matching ids explicitly.
        """
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise NotImplementedError(
                "bonus_points_eligible only supports '=' / '!=' True/False.")
        want_eligible = value if operator == '=' else not value
        matching_ids = [
            template.id for template in self.search([])
            if template.bonus_points_eligible == want_eligible
        ]
        return [('id', 'in', matching_ids)]

    @api.model
    def _search_bonus_points_eligible_noop(self, operator, value):
        raise NotImplementedError(
            "bonus_discount_pct can't be searched/filtered directly - "
            "filter on bonus_points_eligible instead.")

    @api.model
    def _load_pos_data_fields(self, config):
        fields_list = super()._load_pos_data_fields(config)
        return fields_list + ['x_studio_prodotti2reparto', 'bonus_promo_price']
