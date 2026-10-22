import { patch } from "@web/core/utils/patch";
import { PosOrder } from "@point_of_sale/app/models/pos_order";

/**
 * Let an exchange be one order.
 *
 * Stock Odoo refuses to put new merchandise on an order that already
 * carries a refund line:
 *
 *     isSaleDisallowed(values, options) {
 *         return this.isRefund && (!values.qty || values.qty > 0);
 *     }
 *
 * - which is the "Ensure you validate the refund before taking another
 * order" the cashier hits the moment they reach for the replacement. The
 * guard assumes a refund ends with money going back, so a refund and a
 * sale are necessarily two transactions.
 *
 * Under this policy they are one. A customer swapping a 34,95 EUR top for
 * something else is making a single exchange: the order carries both
 * lines, and settles either with a voucher for the difference or with the
 * customer paying it. Splitting it in two would mean refunding 34,95 in
 * cash first, which is the one thing that must not happen.
 *
 * What the guard protects is still protected: an order that owes the
 * customer money can only be settled by the voucher method - see
 * payment_screen.js - so this opens the exchange without opening a way to
 * hand cash back.
 */
patch(PosOrder.prototype, {
    isSaleDisallowed(values, options) {
        return false;
    },
});
