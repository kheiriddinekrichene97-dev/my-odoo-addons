import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";

/** Tick the invoices the customer is paying; all of them by default. */
export class InvoicePicker extends Component {
    static template = "di_pos_customer_account.InvoicePicker";
    static components = { Dialog };
    static props = {
        title: { type: String, optional: true },
        invoices: Array,
        getPayload: Function,
        close: Function,
    };

    setup() {
        this.pos = usePos();
        this.state = useState({
            selected: Object.fromEntries(this.props.invoices.map((invoice) => [invoice.id, true])),
        });
    }

    get selectedInvoices() {
        return this.props.invoices.filter((invoice) => this.state.selected[invoice.id]);
    }

    get total() {
        return this.selectedInvoices.reduce((total, invoice) => total + invoice.amount_residual, 0);
    }

    toggle(invoice) {
        this.state.selected[invoice.id] = !this.state.selected[invoice.id];
    }

    confirm() {
        this.props.getPayload(this.selectedInvoices);
        this.props.close();
    }
}
