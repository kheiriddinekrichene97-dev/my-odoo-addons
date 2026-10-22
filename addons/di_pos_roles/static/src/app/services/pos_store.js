import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { PosStore } from "@point_of_sale/app/services/pos_store";

/**
 * The shop's three roles, as Odoo's pos_hr already computes them per
 * employee when the till loads:
 *
 *   "manager"  - Advanced rights (or a POS Manager user)  -> Administrator
 *   "cashier"  - Basic rights, the default bucket           -> Cashier
 *   "minimal"  - Minimal rights                             -> Salesperson
 *
 * Every rule here is off when the register does not use "Log in with
 * Employees": without it there is no role to read.
 */
patch(PosStore.prototype, {
    get diRole() {
        return this.config.module_pos_hr ? this.getCashier()?._role || false : false;
    },

    /** Administrator and cashier may close the till; a salesperson may not. */
    diCanCloseRegister() {
        return ["manager", "cashier"].includes(this.diRole);
    },

    /** The cashier's Orders list is limited to today; the administrator's is not. */
    diSeesTodayOnly() {
        return this.diRole === "cashier";
    },

    /**
     * A salesperson never reaches the Orders screen. Guarded here rather than
     * only by hiding the button, because the screen is also opened from
     * other places (the Refund action, for one).
     */
    navigate(routeName, routeParams = {}) {
        if (routeName === "TicketScreen" && this.diRole === "minimal") {
            this.notification.add(_t("You are not allowed to view orders."), {
                type: "warning",
            });
            return;
        }
        return super.navigate(...arguments);
    },

    /**
     * Opening the till is the cashier's or the administrator's job.
     *
     * Odoo shows the opening popup to whoever logs in first while the session
     * is still waiting to be opened - a salesperson included. Instead, the
     * salesperson is told why and returned to the login screen, so the till
     * stays closed until someone entitled to count the float opens it.
     */
    openOpeningControl() {
        if (this.diRole === "minimal" && this.session.state === "opening_control") {
            this.dialog.add(
                AlertDialog,
                {
                    title: _t("The till is not open yet"),
                    body: _t(
                        "Only a cashier or an administrator can open the till. Ask one of them to log in and open it."
                    ),
                },
                { onClose: () => this.showLoginScreen() }
            );
            return;
        }
        return super.openOpeningControl(...arguments);
    },
});
