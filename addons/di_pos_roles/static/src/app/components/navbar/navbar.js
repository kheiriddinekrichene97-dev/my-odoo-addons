import { patch } from "@web/core/utils/patch";
import { Navbar } from "@point_of_sale/app/components/navbar/navbar";

patch(Navbar.prototype, {
    /**
     * pos_hr shows "Close Register" to managers, or to the employee whose own
     * Odoo user opened the session. Shop cashiers have no Odoo user, so on
     * its own that rule left closing to the administrator alone.
     */
    get showCloseSession() {
        return super.showCloseSession || this.pos.diCanCloseRegister();
    },
});
