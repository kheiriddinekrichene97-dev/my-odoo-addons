import { patch } from "@web/core/utils/patch";
import { ManageGiftCardPopup } from "@pos_loyalty/app/components/popups/manage_giftcard_popup/manage_giftcard_popup";
import { onWillStart } from "@odoo/owl";

/**
 * Pre-fill the gift card number so a cashier can open this popup purely to
 * set the Amount and Expiration, without having to invent a card number.
 *
 * Why pre-fill rather than just drop the field: pos_loyalty's
 * PosOrder.processGiftCard only records the coupon id, customer and
 * expiration date inside its `if (newGiftCardCode)` branch -
 *
 *     if (newGiftCardCode) {
 *         couponId = applicableCouponIds.shift() || loyaltyIdsGenerator();
 *         couponData.coupon_id = couponId;
 *         couponData.code = newGiftCardCode;
 *         couponData.partner_id = partner_id;
 *         couponData.expiration_date = expirationDate;
 *     }
 *     this.uiState.couponPointChanges[couponId] = couponData;
 *
 * - so an empty number silently discards the expiry (the very thing the
 * popup is being opened to set) and leaves the change keyed on `undefined`.
 * Supplying the number the existing logic already expects leaves every rule
 * about how cards are created untouched.
 *
 * The field stays visible and editable, so selling a pre-printed physical
 * card still works: type or scan its number over the suggested one, and
 * pos_loyalty's own checkGiftCard() handles it exactly as before.
 */
patch(ManageGiftCardPopup.prototype, {
    setup() {
        super.setup(...arguments);

        onWillStart(async () => {
            if (this.state.inputValue?.trim()) {
                return;
            }
            try {
                this.state.inputValue = await this.pos.data.call(
                    "loyalty.card",
                    "di_generate_gift_card_code",
                    []
                );
            } catch {
                // Leave it blank rather than blocking the popup: the cashier
                // can still type a number, which is the stock behaviour.
            }
        });
    },
});
