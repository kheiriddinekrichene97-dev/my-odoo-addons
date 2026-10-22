import { patch } from "@web/core/utils/patch";
import { PosStore } from "@point_of_sale/app/services/pos_store";

/**
 * The running salesperson: who gets credited for what is scanned next.
 *
 * Held per order in uiState - it is a till-side working value, not
 * something the order stores. Attribution itself is recorded on each line
 * as it is created, so changing the running name later never rewrites
 * products another assistant already sold.
 */
patch(PosStore.prototype, {
    diGetSalesperson(order = this.getOrder()) {
        const id = order?.uiState?.diSalespersonId;
        return id ? this.models["hr.employee"].get(id) : false;
    },

    diSetSalesperson(employee, order = this.getOrder()) {
        if (order) {
            order.uiState.diSalespersonId = employee ? employee.id : false;
        }
    },

    /**
     * Every product added to an order funnels through here - the scanner,
     * the product grid and addLineToCurrentOrder alike - which is why the
     * tag is applied at this point rather than in the button.
     *
     * A refund line is left alone: it is built by copying the original
     * line record directly (not through this path in every case), and its
     * salesperson is the one who made the original sale, filled in
     * server-side from refunded_orderline_id.
     */
    async addLineToOrder(vals, order, opts = {}, configure = true) {
        const line = await super.addLineToOrder(...arguments);
        if (line && !line.refunded_orderline_id && !line.salesperson_id) {
            const employee = this.diGetSalesperson(order);
            if (employee) {
                line.salesperson_id = employee;
            }
        }
        return line;
    },
});
