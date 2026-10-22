import { patch } from "@web/core/utils/patch";
import { PosStore } from "@point_of_sale/app/services/pos_store";

/**
 * Makes a customer's outstanding balance appear/disappear in the same POS
 * session, instead of only after closing and reopening the register.
 *
 * pos_settle_due gates its dropdown on values read straight off the
 * client-side partner record (getPartnerCredit -> CustomDropdownItems):
 *
 *     t-if="partnerInfos.totalDue gt 0"                -> partner.total_due
 *       t-if="partnerInfos.posOrdersAmountDue gt 0"    -> partner.pos_orders_amount_due
 *          => "Settle orders"
 *
 * None of those three are declared fields on the POS res.partner model, so
 * they only exist if something explicitly writes them onto the record. Two
 * things prevent that from happening at the right time:
 *
 * 1. pos_settle_due does refresh after validating a pay-later order, but only
 *    for a partner that already has a value:
 *        if (hasCustomerAccountAsPaymentMethod && partner.total_due !== undefined)
 *    Its only bulk population is setAllTotalDueOfPartners() over the partners
 *    loaded at boot - and POS preloads a limited set (limited_customer_count).
 *    A customer outside that set never gets a total_due, so the guard is never
 *    satisfied and they can never get one: the balance stays invisible.
 *
 * 2. Neither backing RPC (get_total_due / get_all_total_due) actually returns
 *    pos_orders_amount_due or invoices_amount_due - both reply with
 *    _load_pos_data_fields + total_due only. So even a successful refresh
 *    leaves pos_orders_amount_due undefined, and "Settle orders" - which is
 *    gated on exactly that value - can never render.
 *
 * So we refresh all three ourselves after any order that can move a balance:
 * total_due via get_all_total_due (it resolves the account-side amount, which
 * a plain read of that field would refuse for a cashier), and the other two
 * via a plain read, which POS users are allowed.
 */
patch(PosStore.prototype, {
    /**
     * Only orders that can actually move a balance are worth the round-trip:
     * one parking part of the total on a "pay later" method (creating the
     * debt), or one carrying a settle/deposit line (paying it off). Ordinary
     * cash and card sales change nothing here.
     */
    orderAffectsCustomerDue(order) {
        const hasPayLater = (order.payment_ids || []).some(
            (payment) => payment.payment_method_id?.type === "pay_later"
        );
        const hasSettleLine = (order.lines || []).some((line) => line.isAnySettleLine?.());
        return hasPayLater || hasSettleLine;
    },

    /**
     * Resolve where a partner's balance is actually read from: getPartnerCredit
     * looks a child contact's balance up on its parent, so values written for
     * display have to land on the same record it reads back.
     */
    dueTotalsTargetOf(partner) {
        if (partner.parent_name) {
            const parent = this.models["res.partner"].find((p) => p.name === partner.parent_name);
            if (parent) {
                return parent;
            }
        }
        return partner;
    },

    /**
     * The two fields neither get_total_due nor get_all_total_due returns.
     * Read directly - POS users are allowed these, unlike account's total_due.
     */
    async loadPosDueFieldsOfPartners(partners) {
        if (!partners.length) {
            return;
        }
        const rows = await this.data.call("res.partner", "read", [
            partners.map((partner) => partner.id),
            ["pos_orders_amount_due", "invoices_amount_due"],
        ]);
        for (const partner of partners) {
            const due = (rows || []).find((row) => row.id === partner.id);
            if (!due) {
                continue;
            }
            const target = this.dueTotalsTargetOf(partner);
            target.pos_orders_amount_due = due.pos_orders_amount_due;
            target.invoices_amount_due = due.invoices_amount_due;
        }
    },

    async refreshDueTotalsOfPartners(partners) {
        if (!partners.length) {
            return;
        }
        const partnerIds = partners.map((partner) => partner.id);

        const [totalDueResult] = await Promise.all([
            this.data.call("res.partner", "get_all_total_due", [partnerIds, this.config.id]),
            this.loadPosDueFieldsOfPartners(partners),
        ]);

        for (const partner of partners) {
            const totalDueEntry = (totalDueResult || []).find(
                (row) => row["res.partner"]?.[0]?.id === partner.id
            );
            if (totalDueEntry) {
                // Deliberately not calling setAllTotalDueOfPartners, which deletes
                // any partner the RPC doesn't return - that would drop a customer
                // out of the cashier's list on an empty response.
                this.dueTotalsTargetOf(partner).total_due =
                    totalDueEntry["res.partner"][0].total_due;
            }
        }
    },

    async postSyncAllOrders(orders) {
        const result = await super.postSyncAllOrders(...arguments);

        const partners = new Map();
        for (const order of orders || []) {
            if (order.partner_id && this.orderAffectsCustomerDue(order)) {
                partners.set(order.partner_id.id, order.partner_id);
            }
        }

        try {
            await this.refreshDueTotalsOfPartners([...partners.values()]);
        } catch {
            // A due-total refresh must never break order syncing: the sale is
            // already validated and posted by this point, and the worst case
            // without it is the stale-until-reopen behaviour we're fixing.
        }

        return result;
    },
});
