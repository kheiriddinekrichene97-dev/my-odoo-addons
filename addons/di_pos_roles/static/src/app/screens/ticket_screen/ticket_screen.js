import { patch } from "@web/core/utils/patch";
import { onMounted } from "@odoo/owl";
import { TicketScreen } from "@point_of_sale/app/screens/ticket_screen/ticket_screen";

/**
 * Searches that name ONE specific receipt. A cashier may use them to reach an
 * order from an earlier day - that is how a return of last week's purchase is
 * found. Searching by date, customer or cardholder, or leaving the search
 * empty, would be browsing history, so those stay limited to today.
 *
 * "Reference" is left out on purpose: it is the short per-day tracking number
 * (e.g. 4001), which repeats across days and would page through old orders.
 */
const SINGLE_RECEIPT_SEARCHES = ["RECEIPT_NUMBER", "INVOICE_NUMBER"];
// A fragment this short matches many receipts at once; ask for enough of the
// number to point at one.
const MIN_RECEIPT_SEARCH_LENGTH = 5;

patch(TicketScreen.prototype, {
    setup() {
        super.setup(...arguments);
        // Belt and braces for the navigate() guard: the screen can also be
        // restored as-is when a salesperson logs in after someone else left
        // the till on Orders.
        onMounted(() => {
            if (this.pos.diRole === "minimal") {
                const order = this.pos.getOrder() || this.pos.addNewOrder();
                this.pos.navigateToOrderScreen(order);
            }
        });
    },

    _computeSyncedOrdersDomain() {
        const domain = super._computeSyncedOrdersDomain(...arguments);
        if (!this.pos.diSeesTodayOnly()) {
            return domain;
        }
        const { fieldName, searchTerm } = this.state.search;
        const term = (searchTerm || "").trim();
        if (
            SINGLE_RECEIPT_SEARCHES.includes(fieldName) &&
            term.length >= MIN_RECEIPT_SEARCH_LENGTH
        ) {
            return domain;
        }
        const startOfToday = luxon.DateTime.local()
            .startOf("day")
            .toUTC()
            .toFormat("yyyy-MM-dd HH:mm:ss");
        // Appended as one more top-level term: Odoo ANDs the top level of a
        // domain, so this narrows whatever the search already built.
        return [...domain, ["date_order", ">=", startOfToday]];
    },
});
