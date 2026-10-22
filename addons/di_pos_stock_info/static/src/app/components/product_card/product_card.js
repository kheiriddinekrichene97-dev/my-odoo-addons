import { patch } from "@web/core/utils/patch";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";
import { ProductCard } from "@point_of_sale/app/components/product_card/product_card";

/**
 * A visible way into the product info popup.
 *
 * The popup itself is already reachable: product_screen.js registers
 * `useLongPress((product) => this.pos.onProductInfoClick(product))` on every
 * card, for mouse and touch, at 500ms. That gesture is invisible, and a shop
 * floor does not discover invisible gestures - so this only labels it. The
 * badge calls the same store method the long press does.
 */
patch(ProductCard.prototype, {
    setup() {
        super.setup(...arguments);
        this.pos = usePos();
    },

    openStockInfo() {
        this.pos.onProductInfoClick(this.props.product);
    },

    /** Swallows mousedown/touchstart on the badge so core's long-press timer
     *  never starts from it: without this, holding the badge would open the
     *  popup twice - once from the timer, once from the badge's own click. */
    swallowPress() {},
});
