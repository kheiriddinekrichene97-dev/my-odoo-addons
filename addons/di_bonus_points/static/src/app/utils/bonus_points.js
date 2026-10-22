/**
 * Client-side mirror of bonus.point.service's eligibility/bracket logic
 * (models/bonus_point_service.py: check_eligibility / get_bracket_points /
 * get_max_points_for_line). POS must keep working offline, so the price a
 * cashier sees and collects payment for has to be computable without a
 * round-trip - this reads the same bonus.point.bracket / .category.rule /
 * .settings records the backend uses (loaded via pos.session), and must be
 * kept in sync with that Python logic if the rules ever change.
 */

export function getBracketPoints(pos, price) {
    const brackets = [...pos.models["bonus.point.bracket"].filter((b) => b.active !== false)].sort(
        (a, b) => (a.sequence || 0) - (b.sequence || 0) || a.price_from - b.price_from
    );
    for (const bracket of brackets) {
        if (price < bracket.price_from) {
            continue;
        }
        if (price === bracket.price_from && !bracket.price_from_inclusive) {
            continue;
        }
        if (bracket.no_upper_limit) {
            return bracket.points;
        }
        if (price > bracket.price_to) {
            continue;
        }
        if (price === bracket.price_to && !bracket.price_to_inclusive) {
            continue;
        }
        return bracket.points;
    }
    return 0;
}

/**
 * `x_reparto` isn't loaded as its own POS model (Studio ("x_") models get
 * rebuilt from scratch by Odoo on every registry setup, discarding any
 * addon-side Python extension of them - see bonus_point_category_rule's
 * reparto_ids/product.template's x_studio_prodotti2reparto comments), so
 * both sides of this comparison are just raw ids, not resolved records.
 */
export function getCategoryRuleForReparto(pos, repartoId) {
    if (!repartoId) {
        return null;
    }
    return (
        pos.models["bonus.point.category.rule"].find(
            (rule) => rule.active !== false && rule.reparto_ids.includes(repartoId)
        ) || null
    );
}

export function computeCampaignDiscountPct(listPrice, effectivePrice) {
    if (!listPrice) {
        return 0.0;
    }
    return ((listPrice - effectivePrice) / listPrice) * 100.0;
}

/**
 * Whether `product` (a product.product OR product.template - the catalogue
 * grid renders templates, order lines carry variants, both expose the same
 * shaped fields) currently qualifies for bonus points at all, judged
 * against its Promo price - a fixed product property, independent of any
 * order line. Shared by the cart's eligibility gate (getMaxPointsForLine)
 * and the catalogue grid's eligibility badge (product_card.js).
 */
export function isBonusEligible(pos, product) {
    // x_studio_prodotti2reparto only lives on product.template - a
    // product.product (order-line variant) doesn't carry it directly, only
    // via product_tmpl_id. A product.template (catalogue grid) has it
    // straight on itself and has no product_tmpl_id.
    const repartoId =
        product.x_studio_prodotti2reparto ?? product.product_tmpl_id?.x_studio_prodotti2reparto;
    const listPrice = product.list_price ?? product.lst_price ?? 0;
    const discountPct = computeCampaignDiscountPct(listPrice, product.bonus_promo_price || 0);
    const rule = getCategoryRuleForReparto(pos, repartoId);
    return Boolean(rule) && discountPct <= rule.max_discount_rate;
}

/**
 * Whether `product` is worth flagging as eligible OUTSIDE a cart context
 * (the catalogue grid badge, since there's no order line/actual paid price
 * to check yet): passes isBonusEligible AND its Promo Price actually lands
 * in a non-zero bracket. An item priced under the lowest bracket's floor
 * would earn/redeem 0 points regardless of category, so isn't worth
 * flagging. The cart's own gate (getMaxPointsForLine) already handles this
 * correctly via its own bracket lookup on the real price paid - this is
 * only for the no-order-line badge.
 */
export function hasBonusPointsPotential(pos, product) {
    if (!isBonusEligible(pos, product)) {
        return false;
    }
    return getBracketPoints(pos, product.bonus_promo_price || 0) > 0;
}

/**
 * price_unit may already be reduced by a PREVIOUS apply_points call on this
 * same line (e.g. a cashier adding points incrementally). Reconstruct the
 * true pre-points unit price so the bracket lookup (how much was actually
 * paid) is always evaluated against the same baseline, never a
 * self-referential one. Shared by getMaxPointsForLine and
 * lineHasBonusPointsPotential so they can never drift apart.
 */
function computeEffectiveUnitPrice(line, qty) {
    const alreadyReducedPerUnit = (line.bonus_reduction_amount || 0) / qty;
    const prePointsPriceUnit = line.price_unit + alreadyReducedPerUnit;
    return prePointsPriceUnit * (1 - (line.discount || 0) / 100);
}

/**
 * Whether the cashier changed this line's price by hand: a typed price
 * (Odoo marks it price_type "manual") or a % discount on the line.
 *
 * Prices that come from a pricelist are NOT included - a Promo pricelist
 * price is already judged through bonus_promo_price, and counting it twice
 * would change nothing but cost a lookup.
 */
function isPriceChangedByHand(line) {
    return line.price_type === "manual" || (line.discount || 0) > 0;
}

/**
 * isBonusEligible for an order line rather than a bare product.
 *
 * A price changed by hand is a discount like any other, so it counts against
 * the category limit (Donna 50%, Uomo 30%) exactly as a Promo price does:
 * whichever of the two is deeper decides. A dress listed at 100 and typed in
 * at 45 is 55% off - over the Donna limit, so no points on it.
 *
 * Measured on the pre-points price (computeEffectiveUnitPrice), so points
 * already applied to the line - which also lower price_unit - are never
 * mistaken for a discount the cashier gave.
 *
 * Mirrored in bonus_point_service.get_max_points_for_line: the server
 * re-checks this when the order is validated, and the two must agree or the
 * till would accept points the server then refuses.
 */
export function isLineBonusEligible(pos, line) {
    const product = line.product_id;
    if (!isBonusEligible(pos, product)) {
        return false;
    }
    if (!isPriceChangedByHand(line)) {
        return true;
    }
    const repartoId =
        product.x_studio_prodotti2reparto ?? product.product_tmpl_id?.x_studio_prodotti2reparto;
    const rule = getCategoryRuleForReparto(pos, repartoId);
    const qty = Math.max(1, Math.trunc(line.qty || 1));
    const manualPct = computeCampaignDiscountPct(
        product.lst_price || 0,
        computeEffectiveUnitPrice(line, qty)
    );
    return manualPct <= rule.max_discount_rate;
}

/**
 * Read-only twin of getMaxPointsForLine, safe to call from a render-time
 * getter (e.g. the cart's Orderline badge, orderline.js): getMaxPointsForLine
 * itself snapshots values onto the line as a side effect (by design, for
 * the redeem flow in control_buttons.js) and must never be called just to
 * decide whether to show a badge - that would write on every re-render.
 */
export function lineHasBonusPointsPotential(pos, line) {
    if (!isLineBonusEligible(pos, line)) {
        return false;
    }
    const qty = Math.max(1, Math.trunc(line.qty || 1));
    return getBracketPoints(pos, computeEffectiveUnitPrice(line, qty)) > 0;
}

/**
 * Mirrors get_max_points_for_line: returns the max points (already x qty)
 * this line can still absorb, given whatever has already been applied to
 * it (line.bonus_reduction_amount).
 */
export function getMaxPointsForLine(pos, line) {
    const product = line.product_id;
    const listPrice = product.lst_price || 0;
    const qty = Math.max(1, Math.trunc(line.qty || 1));

    const discountPct = computeCampaignDiscountPct(listPrice, product.bonus_promo_price || 0);
    const eligible = isLineBonusEligible(pos, line);
    const effectiveUnitPrice = computeEffectiveUnitPrice(line, qty);

    if (!eligible) {
        return { eligible: false, discountPct, pointsPerUnit: 0, maxPoints: 0 };
    }
    const pointsPerUnit = getBracketPoints(pos, effectiveUnitPrice);
    return { eligible: true, discountPct, pointsPerUnit, maxPoints: pointsPerUnit * qty };
}
