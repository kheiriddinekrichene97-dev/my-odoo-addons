import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { OrderSummary } from "@point_of_sale/app/screens/product_screen/order_summary/order_summary";

patch(OrderSummary.prototype, {
    /** The Price key. Cancelling the reason leaves the old price in place. */
    async setLinePrice(line, price) {
        const reason = await this.pos.diAskChangeReason(_t("Why did the price change?"));
        if (reason === undefined) {
            return;
        }
        this.pos.diRecordPriceChange(line, reason);
        return await super.setLinePrice(...arguments);
    },
});
