from odoo import _, api, models
from odoo.exceptions import UserError


class BonusPointService(models.AbstractModel):
    """POS-agnostic bonus points business logic.

    Any integration layer (POS now, Sales/eCommerce later) must call into
    these methods rather than re-implementing the rules described in the
    module specification. Nothing here depends on pos.order/pos.order.line
    existing as concrete models except get_max_points_for_line/apply_points/
    clear_points, whose "order_line" argument only needs to duck-type:
    product_id, qty, price_unit, discount, bonus_* fields.
    """
    _name = 'bonus.point.service'
    _description = 'Bonus Points - Service Layer'

    # ------------------------------------------------------------------
    # Balance / expiry
    # ------------------------------------------------------------------
    @api.model
    def get_available_balance(self, partner):
        return partner.bonus_points_available

    @api.model
    def get_next_expiry(self, partner):
        """Return (expiry_datetime, amount) for the soonest-expiring batch,
        or (False, 0) if the partner has no points."""
        return (
            partner.bonus_points_next_expiry_date,
            partner.bonus_points_next_expiry_amount,
        )

    # ------------------------------------------------------------------
    # Eligibility / bracket
    # ------------------------------------------------------------------
    @api.model
    def compute_campaign_discount_pct(self, list_price, reference_price):
        """% discount of reference_price vs. list_price."""
        if not list_price:
            return 0.0
        return (list_price - reference_price) / list_price * 100.0

    @api.model
    def check_eligibility(self, product_tmpl, discount_pct, company=None):
        """Return (eligible: bool, rule: bonus.point.category.rule).

        An item whose Reparto isn't covered by any active rule is not
        eligible (rule is an empty recordset). An item discounted MORE than
        its rule's threshold is not eligible either.
        """
        company = company or self.env.company
        rule = self.env['bonus.point.category.rule'].get_rule_for_reparto(
            self._get_reparto(product_tmpl), company)
        return self._check_against_rule(rule, discount_pct)

    @api.model
    def _check_against_rule(self, rule, discount_pct):
        """The verdict for an already-resolved category rule, so
        check_eligibility and the batch path (get_ba_snapshot) can't drift
        apart on what "eligible" means."""
        if not rule:
            return False, rule
        return discount_pct <= rule.max_discount_rate, rule

    @api.model
    def _get_reparto(self, product):
        """Reparto of a product.template, or of a product.product (which
        reaches it by delegation). Read defensively: it is a Studio field,
        absent on a database where it was never created."""
        return getattr(product, 'x_studio_prodotti2reparto', False)

    @api.model
    def get_bracket_points(self, price, company=None):
        return self.env['bonus.point.bracket'].get_points_for_price(price, company)

    @api.model
    def get_ba_snapshot(self, products, company=None):
        """The bonus-points verdict for a whole recordset at once.

        `products` is a product.template or product.product recordset; each
        record must expose bonus_promo_price and _bonus_sales_price(), and
        reach a Reparto (directly, or by delegation from its template).

        Returns {record id: (discount_pct, eligible, points)} - the same
        numbers compute_campaign_discount_pct / check_eligibility /
        get_bracket_points give per record, but reading the bracket table
        and the category rules ONCE for the whole recordset rather than
        twice per record. That per-record cost is what made the BA code
        expensive to show on a product list or a cartellino print run;
        the per-record methods above stay as they are for the POS flows,
        which only ever look at one line at a time.
        """
        company = company or self.env.company
        brackets = self.env['bonus.point.bracket'].sudo().search([
            ('company_id', '=', company.id), ('active', '=', True)])
        rules = self.env['bonus.point.category.rule'].sudo().search([
            ('company_id', '=', company.id), ('active', '=', True)])

        snapshot = {}
        for product in products:
            promo_price = product.bonus_promo_price or 0.0
            discount_pct = self.compute_campaign_discount_pct(
                product._bonus_sales_price(), promo_price)
            rule = rules._rule_for_reparto(self._get_reparto(product))
            eligible, _rule = self._check_against_rule(rule, discount_pct)
            points = brackets._points_for_price(promo_price) if eligible else 0
            snapshot[product.id] = (discount_pct, eligible, points)
        return snapshot

    @api.model
    def _is_price_changed_by_hand(self, order_line):
        """A typed price (Odoo marks it price_type 'manual') or a % discount.

        Read defensively: get_max_points_for_line's contract only promises
        product_id, qty, price_unit and discount, and price_type exists on
        pos.order.line alone. Pricelist prices are deliberately excluded - a
        Promo pricelist price is already judged through bonus_promo_price.
        """
        return (getattr(order_line, 'price_type', None) == 'manual'
                or (order_line.discount or 0.0) > 0)

    @api.model
    def get_max_points_for_line(self, order_line, company=None):
        """Compute and STORE the eligibility/bracket snapshot on the line,
        and return the max points (already x qty) it can absorb.

        order_line must expose: product_id, qty, price_unit, discount, and
        the bonus_campaign_discount_pct / bonus_eligible /
        bonus_max_points_per_unit fields to write the snapshot into.
        """
        company = company or self.env.company
        product = order_line.product_id
        tmpl = product.product_tmpl_id
        list_price = product.lst_price or 0.0

        # Eligibility is judged against the product's Promo price (a fixed
        # property of the product, via product.bonus_promo_price), NOT
        # whatever is actually being charged on this line - see
        # bonus.point.settings.promo_pricelist_id. This also means it can
        # never be circular with points already applied on the line, unlike
        # the bracket lookup below.
        discount_pct = self.compute_campaign_discount_pct(
            list_price, product.bonus_promo_price)
        eligible, rule = self.check_eligibility(tmpl, discount_pct, company)

        # price_unit may already be reduced by a PREVIOUS apply_points call
        # on this same line (e.g. a cashier adding points incrementally).
        # Reconstruct the true pre-points unit price so the bracket lookup
        # (how much was actually paid) is always evaluated against the same
        # baseline, never a self-referential one.
        qty = int(order_line.qty or 1)
        already_reduced_per_unit = (
            (order_line.bonus_reduction_amount or 0.0) / qty)
        pre_points_price_unit = order_line.price_unit + already_reduced_per_unit
        effective_unit_price = pre_points_price_unit * (
            1 - (order_line.discount or 0.0) / 100.0)

        # A price the cashier changed by hand - typed in, or a % on the line
        # - is a discount like any other, so it counts against the category
        # limit exactly as the Promo price does: whichever is deeper decides.
        # Measured on the pre-points price, so points already applied to the
        # line are never mistaken for a discount the cashier gave.
        # Mirrored in static/src/app/utils/bonus_points.js
        # (isLineBonusEligible): the till decides offline, this re-checks at
        # validation, and the two must agree.
        if eligible and self._is_price_changed_by_hand(order_line):
            manual_pct = self.compute_campaign_discount_pct(
                list_price, effective_unit_price)
            eligible = manual_pct <= rule.max_discount_rate

        order_line.bonus_campaign_discount_pct = discount_pct
        order_line.bonus_eligible = eligible

        if not eligible:
            order_line.bonus_max_points_per_unit = 0
            return 0

        points_per_unit = self.get_bracket_points(effective_unit_price, company)
        order_line.bonus_max_points_per_unit = points_per_unit
        return points_per_unit * qty

    # ------------------------------------------------------------------
    # Redemption
    # ------------------------------------------------------------------
    @api.model
    def _consume_from_batches(self, partner, points, order_line, company=None):
        """Consume `points` from partner's available batches FIFO (soonest
        expiry first), creating one 'spend' movement per batch touched.
        Ledger-only: never touches order_line.price_unit, so callers that
        already reflected the reduction elsewhere (e.g. the POS frontend,
        which must compute prices offline) don't get double-discounted.
        Raises UserError if the partner's balance can't cover it.
        """
        company = company or self.env.company
        available = self.get_available_balance(partner)
        if points > available:
            raise UserError(_(
                "Not enough bonus points: %(available)d available, "
                "%(requested)d requested.",
                available=available, requested=points))

        Batch = self.env['bonus.point.batch'].sudo()
        Movement = self.env['bonus.point.movement'].sudo()
        batches = Batch.search(
            [('partner_id', '=', partner.id)] + Batch._available_domain(),
            order='expiry_date',
        )
        remaining_to_consume = points
        for batch in batches:
            if remaining_to_consume <= 0:
                break
            take = min(batch.points_remaining, remaining_to_consume)
            batch.points_remaining -= take
            Movement.create({
                'company_id': company.id,
                'batch_id': batch.id,
                'movement_type': 'spend',
                'points': take,
                'pos_order_id': order_line.order_id.id,
                'pos_order_line_id': order_line.id,
            })
            remaining_to_consume -= take

    @api.model
    def _return_to_batches(self, order_line, points, company=None):
        """Return `points` previously consumed for order_line back to their
        source batches (most recent spend first), creating one
        'refund_return' movement per batch touched. Ledger-only, mirrors
        _consume_from_batches."""
        Movement = self.env['bonus.point.movement'].sudo()
        spend_movements = Movement.search([
            ('pos_order_line_id', '=', order_line.id),
            ('movement_type', '=', 'spend'),
        ], order='id desc')
        remaining = points
        for movement in spend_movements:
            if remaining <= 0:
                break
            take = min(movement.points, remaining)
            batch = movement.batch_id
            batch.points_remaining += take
            Movement.create({
                'company_id': movement.company_id.id,
                'batch_id': batch.id,
                'movement_type': 'refund_return',
                'points': take,
                'pos_order_id': order_line.order_id.id,
                'pos_order_line_id': order_line.id,
            })
            remaining -= take

    @api.model
    def apply_points(self, order_line, points_to_apply, company=None):
        """Validate and apply `points_to_apply` ADDITIONAL points to
        `order_line`, on top of whatever is already applied on it.

        - Re-derives the max cap via get_max_points_for_line (never trusts
          a caller-supplied cap) and checks it against the REMAINING
          allowance (cap minus what's already applied on this line), since
          this call is additive, not an absolute total.
        - Consumes the partner's available batches FIFO (soonest expiry
          first), writing points_remaining down and creating one 'spend'
          movement per batch touched, linked to this order_line.
        - Reduces the line's price directly by points_to_apply * point_value
          (embedded on the same line - see spec Q3).
        Raises UserError if points_to_apply exceeds the item's remaining
        cap or the partner's available balance.
        """
        company = company or self.env.company
        partner = order_line.order_id.partner_id
        if not partner:
            raise UserError(_(
                "A fidelity card (customer) must be set on the order before "
                "bonus points can be applied."))

        max_for_line = self.get_max_points_for_line(order_line, company)
        already_applied = order_line.bonus_points_applied or 0
        remaining_cap = max_for_line - already_applied
        if points_to_apply > remaining_cap:
            raise UserError(_(
                "Only %(remaining)d more bonus point(s) can be applied to "
                "this line (%(already)d already applied, cap is %(max)d; "
                "requested %(requested)d more).",
                remaining=remaining_cap, already=already_applied,
                max=max_for_line, requested=points_to_apply))

        available = self.get_available_balance(partner)
        if points_to_apply > available:
            raise UserError(_(
                "Not enough bonus points: %(available)d available, "
                "%(requested)d requested.",
                available=available, requested=points_to_apply))

        if points_to_apply <= 0:
            return

        settings = self.env['bonus.point.settings'].get_settings(company)
        self._consume_from_batches(partner, points_to_apply, order_line, company)

        reduction = points_to_apply * settings.point_value
        order_line.bonus_points_applied += points_to_apply
        order_line.bonus_reduction_amount += reduction
        order_line.price_unit = order_line.price_unit - (
            reduction / (order_line.qty or 1))

    @api.model
    def clear_points(self, order_line):
        """Reverse whatever points are currently applied on order_line,
        returning them to their original batches (same expiry date,
        batches never get a new expiry). Used both when a cashier edits a
        line after applying points, and as the building block for refunds.
        """
        if order_line.bonus_points_applied <= 0:
            return

        Movement = self.env['bonus.point.movement'].sudo()
        spend_movements = Movement.search([
            ('pos_order_line_id', '=', order_line.id),
            ('movement_type', '=', 'spend'),
        ])
        for movement in spend_movements:
            batch = movement.batch_id
            batch.points_remaining += movement.points
            Movement.create({
                'company_id': movement.company_id.id,
                'batch_id': batch.id,
                'movement_type': 'refund_return',
                'points': movement.points,
                'pos_order_id': order_line.order_id.id,
                'pos_order_line_id': order_line.id,
            })

        order_line.price_unit = order_line.price_unit + (
            order_line.bonus_reduction_amount / (order_line.qty or 1))
        order_line.bonus_points_applied = 0
        order_line.bonus_reduction_amount = 0.0

    @api.model
    def reconcile_line_points(self, order_line, company=None):
        """Bring the real points ledger in line with what the POS frontend
        already decided and reflected in order_line's price
        (bonus_points_applied / bonus_reduction_amount / price_unit).

        The frontend is the source of truth for price and eligibility (POS
        must work offline, so it mirrors get_max_points_for_line/bracket
        logic locally) - this method only ever moves real points between
        batches to match that decision, and never touches price_unit again
        (that would double-discount). Called once per line when a POS order
        is validated (see pos.order._process_saved_order override).

        Still re-derives and enforces the true cap/balance server-side
        (never trusts the frontend blindly): raises UserError if the
        frontend's number turns out to be more than the line/partner can
        actually support (e.g. points spent concurrently on another till).
        """
        company = company or self.env.company
        requested = order_line.bonus_points_applied or 0
        consumed = order_line.bonus_points_ledger_consumed or 0
        delta = requested - consumed
        if delta == 0:
            return

        if delta > 0:
            partner = order_line.order_id.partner_id
            if not partner:
                raise UserError(_(
                    "A fidelity card (customer) must be set on the order "
                    "before bonus points can be applied."))
            max_for_line = self.get_max_points_for_line(order_line, company)
            if requested > max_for_line:
                raise UserError(_(
                    "Only %(max)d bonus point(s) can be applied to this "
                    "line (requested %(requested)d).",
                    max=max_for_line, requested=requested))
            self._consume_from_batches(partner, delta, order_line, company)
        else:
            self._return_to_batches(order_line, -delta, company)

        order_line.bonus_points_ledger_consumed = requested

    # ------------------------------------------------------------------
    # Earn base (which part of an order actually earns points)
    # ------------------------------------------------------------------
    @api.model
    def _get_gift_card_trigger_products(self):
        """Products whose SALE issues a gift card / e-wallet top-up.

        Read off the loyalty programs themselves rather than configured
        separately here, so adding a second gift-card program - or changing
        which product sells one - needs no bonus-points configuration.

        Empty when `loyalty` isn't installed. This module depends only on
        point_of_sale/product/stock, so it must run on a database with no
        loyalty engine at all - where there are no gift cards to leave out
        of the earn base in the first place. The guard belongs HERE rather
        than in _is_gift_card_line: self.env['loyalty.program'] resolves
        through the registry and raises before any per-line check runs,
        and credit_points reaches this on EVERY order save.
        """
        if 'loyalty.program' not in self.env:
            return self.env['product.product'].browse()
        programs = self.env['loyalty.program'].sudo().search([
            ('program_type', 'in', ('gift_card', 'ewallet')),
        ])
        return programs.trigger_product_ids

    @api.model
    def _is_gift_card_line(self, line, gift_products=None):
        """True for BOTH halves of a gift card: the line that sells one, and
        the reward line pos_loyalty adds when one pays for an order."""
        if 'reward_id' not in line._fields:
            # pos_loyalty not installed - there are no gift cards.
            return False
        if gift_products is None:
            gift_products = self._get_gift_card_trigger_products()
        if line.product_id in gift_products:
            return True
        return line.reward_id.program_id.program_type in ('gift_card', 'ewallet')

    @api.model
    def _line_value(self, line):
        """A line's value, signed the way the order's total is signed.

        price_subtotal_incl cannot be trusted for the sign on a refund
        order: Odoo stores every line there inverted relative to
        amount_total. A returned 14,95 item reads +14,95 and its 26,97
        replacement reads -26,97, while the order totals +12,02. Reading
        the field directly made the earn base come out negative and no
        points were awarded at all.

        The magnitude is taken from the subtotal, which already carries
        discounts and taxes; the direction from qty and price_unit
        together. Both matter: a returned item has a negative qty, while
        pos_loyalty's redemption reward has a positive qty and a negative
        price.
        """
        magnitude = abs(line.price_subtotal_incl)
        goes_out = (line.qty < 0) != (line.price_unit < 0)
        return -magnitude if goes_out else magnitude

    @api.model
    def get_earn_base(self, order):
        """The part of `order` that earns bonus points.

        Selling a gift card is not a purchase - it converts cash into stored
        value. The points belong to whoever eventually SPENDS the card, not
        to whoever bought it, so both halves of a gift card leave the base:

        - the SALE line (positive) is subtracted, so buying a 150 EUR card
          earns nothing;
        - the REDEMPTION line (negative, added by pos_loyalty when a card
          pays for the order) is subtracted too - and being negative, that
          ADDS the merchandise value back, so a customer paying entirely
          with a card still earns points on what they actually took home.

        Refund lines stay IN the base. There is no refund in this shop -
        a return is a substitution, and what the customer's points follow
        is the value that actually moved. Returning a 14,95 item for a
        26,97 one and paying the 12,02 difference is, for points, the same
        as buying 12,02 of goods: +2. Taking a 42,90 gift card instead is
        the same as handing 42,90 of goods back: -8.

        Ordinary promotion/discount reward lines stay in the base on
        purpose: a discount genuinely reduces what the customer spent.
        """
        gift_products = self._get_gift_card_trigger_products()
        excluded = sum(
            self._line_value(line) for line in order.lines
            if self._is_gift_card_line(line, gift_products))
        return order.amount_total - excluded

    @api.model
    def credit_points(self, order):
        """Move the customer's points by the value this order moved.

        One rule covers every case, because this shop never refunds - a
        return is a substitution, and points follow the merchandise:

            buys 75,60 of clothes            +15
            pays 12,02 to trade up            +2
            takes a 42,90 gift card           -8
            trades down, 5,00 onto a card     -1

        A gift card is merchandise that earns nothing (get_earn_base drops
        both its halves), so swapping goods for one removes the points
        those goods carried - and the customer earns them back the day
        they spend the card on something real.

        int() truncates toward zero where // floors. On a negative base
        that difference is a whole point taken from the customer: a 42,90
        return is -8,58, which floors to -9 and truncates to -8. Eight is
        what the sale earned, so eight is what comes back off.
        """
        if not order.partner_id:
            return self.env['bonus.point.batch'].browse()
        settings = self.env['bonus.point.settings'].get_settings(order.company_id)
        if not settings.earn_divisor:
            return self.env['bonus.point.batch'].browse()

        delta = int(self.get_earn_base(order) / settings.earn_divisor)
        if delta > 0:
            batch = self._batch_being_corrected(order)
            if batch:
                return self._add_to_batch(batch, delta, order)
            return self.env['bonus.point.batch'].create_batch(
                order.partner_id, delta, source_order=order,
                company=order.company_id)
        if delta < 0:
            self._reverse_points(order, -delta)
        return self.env['bonus.point.batch'].browse()

    @api.model
    def _batch_being_corrected(self, order):
        """The batch an exchange's points belong to, if there is one.

        An exchange is not a new purchase - it is a correction to an
        earlier one, so its points belong to that sale's batch and must
        live exactly as long as it does. Opening a fresh batch would hand
        the customer a new expiry clock: swap something a fortnight after
        buying it and the extra points would outlive, by two weeks, the
        ones the purchase itself earned.

        Empty for an ordinary sale, which has no earlier order behind it
        and gets a batch of its own as before.
        """
        if not order.partner_id:
            return self.env['bonus.point.batch'].browse()
        originals = order.lines.refunded_orderline_id.order_id
        if not originals:
            return self.env['bonus.point.batch'].browse()
        return self.env['bonus.point.batch'].sudo().search([
            ('source_pos_order_id', 'in', originals.ids),
            ('partner_id', '=', order.partner_id.id),
        ], order='earn_date desc', limit=1)

    @api.model
    def _add_to_batch(self, batch, points, order):
        """Grow an existing batch, leaving its dates alone.

        earn_date and expiry_date are deliberately untouched: inheriting
        them is the whole point, and writing them would give the added
        points the longer life this exists to prevent.
        """
        batch.sudo().write({
            'points_earned': batch.points_earned + points,
            'points_remaining': batch.points_remaining + points,
        })
        self.env['bonus.point.movement'].sudo().create({
            'company_id': batch.company_id.id,
            'batch_id': batch.id,
            'movement_type': 'earn',
            'points': points,
            'pos_order_id': order.id,
        })
        return batch

    @api.model
    def _reverse_points(self, order, points):
        """Take `points` back off the customer.

        Batches from the sale being returned against go first, so the
        movement reads against the purchase it undoes. Anything still
        outstanding comes off the rest oldest-first - the same order
        spending consumes them, so the soonest to expire go first and the
        customer keeps the points with the most life left.
        """
        Batch = self.env['bonus.point.batch'].sudo()
        originals = order.lines.refunded_orderline_id.order_id
        preferred = Batch.browse()
        if originals:
            preferred = Batch.search([
                ('source_pos_order_id', 'in', originals.ids),
                ('points_remaining', '>', 0),
            ], order='earn_date')
        others = Batch.search([
            ('partner_id', '=', order.partner_id.id),
            ('points_remaining', '>', 0),
            ('id', 'not in', preferred.ids),
        ], order='earn_date')

        remaining = points
        for batch in list(preferred) + list(others):
            if remaining <= 0:
                break
            take = min(remaining, batch.points_remaining)
            self.reverse_earned_points(batch, take, order=order)
            remaining -= take

    @api.model
    def reverse_earned_points(self, batch, points_to_reverse, order=None):
        """Reverse (cancel) up to points_to_reverse points from a specific
        earn batch, used when the order that generated it is refunded.
        Only reduces points_remaining down to 0, never negative (some of
        the earned points may already have been spent).
        """
        if points_to_reverse <= 0 or not batch:
            return
        actual = min(points_to_reverse, batch.points_remaining)
        if actual <= 0:
            return
        batch.points_remaining -= actual
        self.env['bonus.point.movement'].sudo().create({
            'company_id': batch.company_id.id,
            'batch_id': batch.id,
            'movement_type': 'refund_reverse_earn',
            'points': actual,
            'pos_order_id': order.id if order else False,
        })

    @api.model
    def process_refund(self, order):
        """Bonus-points side of a refund: called once per order from
        pos.order._process_saved_order (guarded there by
        bonus_points_refund_processed). No-op unless `order` actually
        contains refund lines (order_line.refunded_orderline_id set).

        Only one thing happens here now: points that were SPENT on a
        returned line go back to the batch they came from, with their
        original expiry, prorated by quantity for a partial return. A
        customer who used 5 points as a discount and brings the item back
        has those 5 points again.

        Reversing what the sale EARNED used to live here too, and no
        longer does - credit_points moves the balance in both directions
        from the order's net value, so doing it here as well would take
        the points off twice. On an exchange the two even fought each
        other: the reversal took 3 while the net earned 1, and a customer
        who traded up and paid the difference ended up worse off.
        """
        refund_lines = order.lines.filtered(lambda l: l.refunded_orderline_id)
        if not refund_lines:
            return

        # 1. Return spent points to their original batches.
        for line in refund_lines:
            original_line = line.refunded_orderline_id
            if original_line.qty and original_line.bonus_points_applied > 0:
                qty_fraction = min(1.0, abs(line.qty) / original_line.qty)
                points_to_return = round(
                    original_line.bonus_points_applied * qty_fraction)
                if points_to_return > 0:
                    self._return_to_batches(original_line, points_to_return)
