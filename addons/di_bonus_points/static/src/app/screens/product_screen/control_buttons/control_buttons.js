import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { ControlButtons } from "@point_of_sale/app/screens/product_screen/control_buttons/control_buttons";
import { NumberPopup } from "@point_of_sale/app/components/popups/number_popup/number_popup";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { makeAwaitable } from "@point_of_sale/app/utils/make_awaitable_dialog";
import { getMaxPointsForLine } from "@di_bonus_points/app/utils/bonus_points";

patch(ControlButtons.prototype, {
    get bonusPointsSettings() {
        return this.pos.models["bonus.point.settings"].getFirst();
    },

    displayBonusPointsBtn() {
        const order = this.currentOrder;
        return Boolean(
            order && order.getPartner() && order.getSelectedOrderline() && this.bonusPointsSettings
        );
    },

    async clickBonusPoints() {
        const order = this.currentOrder;
        const partner = order.getPartner();
        const line = order.getSelectedOrderline();
        if (!partner || !line) {
            return;
        }

        const settings = this.bonusPointsSettings;
        const alreadyApplied = line.bonus_points_applied || 0;
        const { eligible, maxPoints } = getMaxPointsForLine(this.pos, line);
        const available = partner.bonus_points_available || 0;
        // Points already on this line still count as "available" for the
        // purpose of raising/lowering the amount used on it.
        const cap = Math.min(maxPoints, alreadyApplied + available);

        if (!eligible || cap <= 0) {
            this.dialog.add(AlertDialog, {
                title: _t("Not eligible"),
                body: _t(
                    "This item isn't eligible for bonus points on this order, or the customer has none available."
                ),
            });
            return;
        }

        const buffer = await makeAwaitable(this.dialog, NumberPopup, {
            title: _t("Use bonus points"),
            subtitle: _t(
                "%(available)s point(s) available - up to %(cap)s can be used on this line",
                { available, cap }
            ),
            startingValue: alreadyApplied,
            isValid: (value) => {
                const n = Number(value);
                return Number.isInteger(n) && n >= 0 && n <= cap;
            },
        });
        if (buffer === undefined) {
            return;
        }

        const points = Math.max(0, Math.trunc(Number(buffer) || 0));
        const delta = points - alreadyApplied;
        if (delta === 0) {
            return;
        }

        const reduction = delta * settings.point_value;
        line.bonus_points_applied = points;
        line.bonus_reduction_amount = (line.bonus_reduction_amount || 0) + reduction;
        line.price_unit = line.price_unit - reduction / (line.qty || 1);
    },
});
