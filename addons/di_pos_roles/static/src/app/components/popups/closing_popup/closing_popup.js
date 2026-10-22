import { patch } from "@web/core/utils/patch";
import { ClosePosPopup } from "@point_of_sale/app/components/popups/closing_popup/closing_popup";

patch(ClosePosPopup.prototype, {
    /**
     * Whether the counted cash may differ from the expected amount.
     *
     * pos_hr grants this only to an employee linked to a POS Manager *user*,
     * or within the shop's allowed difference. The cashiers who now close the
     * till have no user account, so without this they would be stuck the
     * moment the drawer was a cent out. They may close anyway: Odoo still asks
     * them to confirm, and still books the difference against the session,
     * where it shows in the session report.
     */
    hasUserAuthority() {
        return super.hasUserAuthority(...arguments) || this.pos.diCanCloseRegister();
    },
});
