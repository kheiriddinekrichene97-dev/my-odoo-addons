import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { SalespersonPopup } from "@di_pos_salesperson/app/components/popups/salesperson_popup/salesperson_popup";
import { makeAwaitable } from "@point_of_sale/app/utils/make_awaitable_dialog";

patch(ControlButtons.prototype, {
    get eligibleSalespeople() {
        return this.pos.models["hr.employee"].filter((employee) => employee.is_pos_salesperson);
    },

    displaySalespersonBtn() {
        return Boolean(this.currentOrder && this.eligibleSalespeople.length);
    },

    /** The button shows who is currently being credited, so the cashier can
     * see at a glance whether it still needs changing. */
    getSalespersonLabel() {
        const employee = this.pos.diGetSalesperson(this.currentOrder);
        return employee ? employee.name : _t("Salesperson");
    },

    async clickSalesperson() {
        const order = this.currentOrder;
        const line = order.getSelectedOrderline();
        const running = this.pos.diGetSalesperson(order);
        // What the popup shows as ticked: the line's own salesperson when
        // correcting one, otherwise whoever is currently running.
        const current = line?.salesperson_id || running;

        const list = [
            { id: 0, label: _t("None"), isSelected: !current, item: false },
            ...this.eligibleSalespeople.map((employee) => ({
                id: employee.id,
                label: employee.name,
                isSelected: Boolean(current && current.id === employee.id),
                item: employee,
            })),
        ];

        const payload = await makeAwaitable(this.dialog, SalespersonPopup, {
            title: _t("Who is selling?"),
            list,
            lineName: line ? line.getFullProductName() : false,
        });

        if (payload === undefined) {
            return;
        }

        const employee = payload.employee || false;
        if (payload.onlyThisLine) {
            line.salesperson_id = employee;
            return;
        }
        // From now on - and the line in hand counts as "now", so it is
        // tagged too. Products already on the order keep whoever sold them.
        this.pos.diSetSalesperson(employee, order);
        if (line && !line.salesperson_id) {
            line.salesperson_id = employee;
        }
    },
});
