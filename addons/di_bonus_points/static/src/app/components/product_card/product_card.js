import { patch } from "@web/core/utils/patch";
import { ProductCard } from "@point_of_sale/app/components/product_card/product_card";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { hasBonusPointsPotential } from "@di_bonus_points/app/utils/bonus_points";

patch(ProductCard.prototype, {
    setup() {
        super.setup(...arguments);
        this.pos = usePos();
    },

    get isBonusPointsEligible() {
        return hasBonusPointsPotential(this.pos, this.props.product);
    },
});
