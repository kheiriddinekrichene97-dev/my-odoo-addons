import { patch } from "@web/core/utils/patch";
import OrderPaymentValidation from "@point_of_sale/app/utils/order_payment_validation";

/**
 * Print the voucher on the same paper a sold gift card comes out on.
 *
 * The shop has no blank cards in the drawer - the "card" is the PDF Odoo
 * prints when one is sold, driven by the loyalty programme's own
 * communication plan (trigger "create" -> report "Gift Card"). Selling a
 * card reaches that print through pos_loyalty's coupon payload:
 *
 *     for (const [actionId, active_ids] of Object.entries(payload.coupon_report)) {
 *         await this.env.services.report.doAction(actionId, active_ids);
 *     }
 *
 * A refund voucher is created server-side rather than through that coupon
 * flow, so it never enters that payload. This fires the identical report
 * for it, with the report and card ids the server put on the order.
 *
 * `this.order` is the class's own getter and `this.pos.env.services` is how
 * it reaches services - it has no `this.env` of its own.
 */
patch(OrderPaymentValidation.prototype, {
    async afterOrderValidation() {
        const result = await super.afterOrderValidation(...arguments);

        const order = this.order;
        const reportId = order?.di_refund_voucher_report_id;
        const cardId = order?.di_refund_voucher_card_id;
        if (!reportId || !cardId) {
            return result;
        }

        try {
            await this.pos.env.services.report.doAction(reportId, [cardId]);
        } catch (error) {
            // The card exists and is spendable whatever happens here; only
            // its printout failed. Losing the order over a print would be
            // far worse than telling the cashier to reprint it.
            this.pos.notification?.add?.(
                "Il buono e stato creato ma non e stato stampato. " +
                    "Ristamparlo dai Coupon.",
                { type: "warning" }
            );
        }
        return result;
    },
});
