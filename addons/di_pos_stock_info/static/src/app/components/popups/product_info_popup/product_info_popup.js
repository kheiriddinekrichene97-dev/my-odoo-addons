import { patch } from "@web/core/utils/patch";
import { useState } from "@odoo/owl";
import { ProductInfoPopup } from "@point_of_sale/app/components/popups/product_info_popup/product_info_popup";

/**
 * Splits the Inventory section into the shops that hold the item and the ones
 * that don't. Core hands the popup every warehouse in the company - 22 here -
 * and the template prints them all, so the 4 shops that can actually serve the
 * customer arrive buried in 18 zero rows.
 *
 * Only the reading order changes: the empty shops are folded, never dropped.
 */
patch(ProductInfoPopup.prototype, {
    setup() {
        super.setup(...arguments);
        this.stockState = useState({ showEmpty: false });
    },

    get allWarehouses() {
        return this.props.info?.productInfo?.warehouses || [];
    },

    /** Id of the warehouse this register sells from, or undefined.
     *  `stock.warehouse` isn't loaded in the POS, so this field is a bare id
     *  rather than a record - see di_pos_stock_info's stock.picking.type. */
    get ownWarehouseId() {
        const warehouse = this.pos.config.picking_type_id?.warehouse_id;
        return warehouse && typeof warehouse === "object" ? warehouse.id : warehouse;
    },

    isOwnWarehouse(warehouse) {
        const own = this.ownWarehouseId;
        return Boolean(own) && warehouse.id === own;
    },

    hasStock(warehouse) {
        // Not `> 0`: a negative on-hand is still something the shop has to be
        // told about, and hiding it would make a stock error invisible here.
        return Boolean(
            warehouse.free_qty || warehouse.available_quantity || warehouse.forecasted_quantity
        );
    },

    get stockedWarehouses() {
        return this.allWarehouses
            .filter((warehouse) => this.hasStock(warehouse))
            .sort(
                (a, b) =>
                    this.isOwnWarehouse(b) - this.isOwnWarehouse(a) ||
                    b.free_qty - a.free_qty ||
                    a.name.localeCompare(b.name)
            );
    },

    get emptyWarehouses() {
        return this.allWarehouses.filter((warehouse) => !this.hasStock(warehouse));
    },

    get stockedTotal() {
        return this.stockedWarehouses.reduce(
            (total, warehouse) => total + (warehouse.free_qty || 0),
            0
        );
    },

    get stockUom() {
        return this.allWarehouses[0]?.uom || "";
    },

    toggleEmptyWarehouses() {
        this.stockState.showEmpty = !this.stockState.showEmpty;
    },
});
