import { patch } from "@web/core/utils/patch";
import { PartnerList } from "@point_of_sale/app/screens/partner_list/partner_list";
import { PartnerLine } from "@point_of_sale/app/screens/partner_list/partner_line/partner_line";

/**
 * Fills the balance column point_of_sale leaves empty for this purpose.
 * Balances are fetched every time the list opens and for every customer a
 * search or scroll loads, so they are always current (no reopening the till).
 */
patch(PartnerList.prototype, {
    setup() {
        super.setup(...arguments);
        this.pos.diLoadBalances(this.state.initialPartners);
    },

    get isBalanceDisplayed() {
        return Boolean(this.pos.diCustomerAccountMethod);
    },

    async getNewPartners() {
        const partners = await super.getNewPartners(...arguments);
        this.pos.diLoadBalances(partners || []);
        return partners;
    },
});

patch(PartnerLine.prototype, {
    get diBalance() {
        return this.pos.diBalances[this.props.partner.id];
    },

    diSettle(kind) {
        this.props.close();
        this.pos.diStartSettlement(this.props.partner, kind);
    },
});
