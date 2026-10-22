import { patch } from "@web/core/utils/patch";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";

const PRICE_KEYS = ["price", "discount"];

patch(ProductScreen.prototype, {
    /**
     * At a till where price changes are not allowed, both keys are greyed
     * out for everyone. Where they are allowed, Odoo's own rules still apply
     * on top: Minimal rights (the salesperson) never get them, and the % key
     * still follows the shop's Line Discounts setting.
     */
    getNumpadButtons() {
        const buttons = super.getNumpadButtons(...arguments);
        if (this.pos.config.di_allow_price_change) {
            return buttons;
        }
        return buttons.map((button) =>
            PRICE_KEYS.includes(button.value) ? { ...button, disabled: true } : button
        );
    },
});
