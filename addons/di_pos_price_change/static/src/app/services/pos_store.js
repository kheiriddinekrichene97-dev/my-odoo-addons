import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { TextInputPopup } from "@point_of_sale/app/components/popups/text_input_popup/text_input_popup";
import { makeAwaitable } from "@point_of_sale/app/utils/make_awaitable_dialog";

patch(PosStore.prototype, {
    /**
     * Odoo's own hooks for exactly this. By default the Price and % keys edit
     * the line keystroke by keystroke - typing 25,50 sets 2, then 25, then
     * 25,5 - so there is no single moment to ask why. Returning true makes
     * each key open one "Set the new price / discount" popup instead, and the
     * reason is asked once, after it.
     */
    restrictLinePriceChange() {
        return true;
    },

    restrictLineDiscountChange() {
        return true;
    },

    /**
     * Ask why. Resolves to the reason, or undefined when the cashier cancels
     * - in which case the caller must leave the line untouched.
     *
     * A blank reason is refused rather than accepted: the whole point is
     * that no change goes through without one.
     */
    async diAskChangeReason(title) {
        while (true) {
            const reason = await makeAwaitable(this.dialog, TextInputPopup, {
                title,
                rows: 3,
                placeholder: _t("Write the reason for this change..."),
            });
            if (reason === undefined) {
                return undefined;
            }
            if (reason.trim()) {
                return reason.trim();
            }
            this.notification.add(_t("A reason is required to change the price."), {
                type: "warning",
            });
        }
    },

    /**
     * Record the change on the line, BEFORE it is applied: the first change
     * captures what the line was, so the Price Changes list can show the
     * price the customer would otherwise have paid.
     */
    diRecordPriceChange(line, reason) {
        if (!line.di_price_note) {
            line.di_original_price_unit = line.price_unit;
            line.di_original_discount = line.discount || 0;
        }
        line.di_price_note = line.di_price_note ? `${line.di_price_note}\n${reason}` : reason;
        const cashier = this.getCashier();
        if (cashier?.model?.name === "hr.employee") {
            line.di_price_changed_by_id = cashier;
        }
    },

    /** The % key. Only the order summary's numpad reaches this. */
    async setDiscountFromUI(line, val) {
        const reason = await this.diAskChangeReason(_t("Why this discount?"));
        if (reason === undefined) {
            return;
        }
        this.diRecordPriceChange(line, reason);
        return await super.setDiscountFromUI(...arguments);
    },
});
