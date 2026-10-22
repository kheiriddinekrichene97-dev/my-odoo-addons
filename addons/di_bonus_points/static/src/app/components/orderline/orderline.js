import { patch } from "@web/core/utils/patch";
import { Orderline } from "@point_of_sale/app/components/orderline/orderline";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { lineHasBonusPointsPotential } from "@di_bonus_points/app/utils/bonus_points";

patch(Orderline.prototype, {
    setup() {
        super.setup(...arguments);
        this.pos = usePos();
    },

    get isBonusPointsEligible() {
        if (!this.line.order_id || !this.line.product_id) {
            return false;
        }
        return lineHasBonusPointsPotential(this.pos, this.line);
    },
});
