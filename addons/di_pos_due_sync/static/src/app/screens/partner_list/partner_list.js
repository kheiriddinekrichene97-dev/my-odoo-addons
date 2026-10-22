import { patch } from "@web/core/utils/patch";
import { PartnerList } from "@point_of_sale/app/screens/partner_list/partner_list";

/**
 * Looking a customer up should show what they owe, not just after a sale.
 *
 * pos_settle_due already refreshes total_due on search
 * (searchPartner -> setAllTotalDueOfPartners), but that RPC never returns
 * pos_orders_amount_due - and "Settle orders" is gated on exactly that value
 * (see the note in ../../services/pos_store.js). So a cashier looking up an
 * existing debtor sees "Deposit money" rather than "Settle orders".
 *
 * Fill in the two missing fields for whoever the search turned up. total_due
 * is left to pos_settle_due's own patch on this method - no need to fetch it
 * twice.
 */
patch(PartnerList.prototype, {
    async searchPartner() {
        const partners = await super.searchPartner();
        try {
            await this.pos.loadPosDueFieldsOfPartners(partners || []);
        } catch {
            // Never let a balance lookup break customer search.
        }
        return partners;
    },
});
