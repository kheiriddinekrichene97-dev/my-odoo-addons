import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

/**
 * On a return, only the voucher may settle the order.
 *
 * Cassa, Carta and Account cliente all end with the shop handing value
 * back - cash, a card reversal, or credit on the customer's account. None
 * of those are allowed on a return here, so they refuse to be used and
 * say why, rather than being quietly missing: a cashier who taps Cassa
 * out of habit should learn the rule, not wonder where the button went.
 *
 * `isRefund` is the order's own getter (`get isRefund() { return
 * this.is_refund === true; }`), and `di_issues_gift_card` reaches the till
 * through pos.payment.method._load_pos_data_fields.
 *
 * This is the interface half only. What actually issues the card is
 * pos.order._di_issue_refund_voucher on the server, which is also what
 * makes the guarantee hold when the till has been running offline.
 */
patch(PaymentScreen.prototype, {
    /**
     * Show only the methods that make sense for this order.
     *
     * The voucher is meaningless on a sale - a customer buying a coat is
     * not owed anything - and the money methods are forbidden on a return.
     * Hiding rather than refusing means a cashier never reaches for the
     * wrong one at all, and where a return leaves exactly one method Odoo
     * selects it by itself.
     *
     * Keyed on what the order OWES, not on isRefund: a dearer exchange is
     * a refund order whose total is positive, and there the customer pays
     * normally.
     *
     * The click guard below stays as a backstop. This runs in setup, so it
     * reflects the total as the screen was entered; the guard is what holds
     * if anything reaches a method it should not.
     */
    setup() {
        super.setup(...arguments);

        const owesCustomer = (this.currentOrder?.totalDue ?? 0) < 0;

        // Clear payment lines left behind by an earlier trip through this
        // screen. An exchange is built in stages - refund, then Back for the
        // replacement, then here again - and a line entered while the total
        // was still the gross refund survives and no longer fits. That is
        // how an order ends up with a voucher for 14,95 and 9,95 of cash the
        // till never received.
        for (const line of [...(this.currentOrder?.payment_ids ?? [])]) {
            const isVoucher = Boolean(line.payment_method_id?.di_issues_gift_card);
            if (owesCustomer !== isVoucher) {
                try {
                    this.currentOrder.removePaymentline(line);
                } catch {
                    // assertEditable can refuse; leaving the line is
                    // recoverable, throwing here would strand the screen.
                }
            }
        }

        const wanted = this.payment_methods_from_config.filter((method) =>
            owesCustomer ? method.di_issues_gift_card : !method.di_issues_gift_card
        );

        // Never leave the screen with nothing to click. A shop that has not
        // configured the voucher method would otherwise have an unusable
        // payment screen on every return, which is far worse than showing
        // a method the guard will refuse.
        if (wanted.length) {
            this.payment_methods_from_config = wanted;
        }
    },

    async addNewPaymentLine(paymentMethod) {
        // Gate on what the order OWES, not on whether it is a refund.
        // A dearer exchange is a refund order too - refund -34,95 plus a
        // 49,95 replacement - and there the customer owes US 15,00, which
        // any method may take. Blocking every refund would have made that
        // impossible to settle at all.
        const owesCustomer = (this.currentOrder?.totalDue ?? 0) < 0;

        if (owesCustomer && !paymentMethod.di_issues_gift_card) {
            this.dialog.add(AlertDialog, {
                title: _t("Not available on a return"),
                body: _t(
                    "Money is never given back on a return.\n\n" +
                        "Settle this order with “%s” instead — the customer " +
                        "receives a gift card for the same amount.",
                    this.diVoucherMethodName || _t("the gift card method")
                ),
            });
            return;
        }
        return await super.addNewPaymentLine(...arguments);
    },

    /** Name the voucher method in the message, so it is findable on screen. */
    get diVoucherMethodName() {
        const method = this.payment_methods_from_config?.find(
            (m) => m.di_issues_gift_card
        );
        return method?.name;
    },
});
