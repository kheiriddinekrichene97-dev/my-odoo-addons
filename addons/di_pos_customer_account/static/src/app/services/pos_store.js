import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { makeAwaitable } from "@point_of_sale/app/utils/make_awaitable_dialog";
import { NumberPopup } from "@point_of_sale/app/components/popups/number_popup/number_popup";
import { InvoicePicker } from "@di_pos_customer_account/app/components/invoice_picker/invoice_picker";

patch(PosStore.prototype, {
    async setup() {
        // {partner id: {pos_due, invoices_due, total_due}}, read by the customer list.
        this.diBalances = {};
        return await super.setup(...arguments);
    },

    /** The "Customer Account" (pay later, identified customer) method of this till. */
    get diCustomerAccountMethod() {
        return this.config.payment_method_ids.find(
            (method) => method.type === "pay_later" && method.split_transactions
        );
    },

    async diLoadBalances(partners) {
        const ids = [...new Set((partners || []).map((partner) => partner?.id).filter(Boolean))];
        if (!ids.length || !this.diCustomerAccountMethod) {
            return;
        }
        try {
            const balances = await this.data.call("res.partner", "di_get_customer_balances", [
                ids,
                this.config.id,
            ]);
            Object.assign(this.diBalances, balances || {});
        } catch (error) {
            // A balance that cannot be read must never block the till.
            console.warn("Could not load customer balances", error);
        }
    },

    /**
     * Start a settlement for `partner`: a new order holding one information
     * line at 0 and a NEGATIVE Customer Account payment, then the payment
     * screen, where the cashier takes the same amount in cash or card.
     *
     * @param {"debt"|"invoices"|"deposit"} kind
     */
    async diStartSettlement(partner, kind) {
        const method = this.diCustomerAccountMethod;
        const product = this.models["product.product"].get(this.config._di_customer_account_product_id);
        if (!method || !product) {
            this.dialog.add(AlertDialog, {
                title: _t("Customer Account not available"),
                body: _t(
                    "This point of sale needs a Customer Account payment method (with 'Identify Customer') to settle balances."
                ),
            });
            return;
        }
        await this.diLoadBalances([partner]);
        const balance = this.diBalances[partner.id] || {};
        let amount = 0;
        let label = "";
        let invoiceIds = [];

        if (kind === "debt") {
            amount = balance.pos_due || 0;
            label = _t("Settle debt");
        } else if (kind === "invoices") {
            const invoices = await this.data.call("res.partner", "di_get_open_invoices", [
                partner.id,
                this.config.id,
            ]);
            if (!invoices?.length) {
                this.notification.add(_t("%s has no open invoices.", partner.name), { type: "info" });
                return;
            }
            const selected = await makeAwaitable(this.dialog, InvoicePicker, {
                title: _t("Invoices of %s", partner.name),
                invoices,
            });
            if (!selected?.length) {
                return;
            }
            amount = selected.reduce((total, invoice) => total + invoice.amount_residual, 0);
            invoiceIds = selected.map((invoice) => invoice.id);
            label = _t("Settle invoices: %s", selected.map((invoice) => invoice.name).join(", "));
        } else if (kind === "deposit") {
            const value = await makeAwaitable(this.dialog, NumberPopup, {
                title: _t("Deposit for %s", partner.name),
                startingValue: "",
                isValid: (input) => parseFloat(input) > 0,
            });
            amount = parseFloat(value) || 0;
            label = _t("Deposit");
        }

        amount = this.currency.round(amount);
        if (!(amount > 0)) {
            if (kind === "debt") {
                this.notification.add(_t("%s has nothing to settle.", partner.name), { type: "info" });
            }
            return;
        }

        const order = this.addNewOrder();
        order.setPartner(partner);
        await this.addLineToOrder(
            {
                product_tmpl_id: product.product_tmpl_id,
                product_id: product,
                price_unit: 0,
                full_product_name: label,
            },
            order,
            {},
            false
        );
        order.di_settle_kind = kind;
        order.di_settle_invoice_ids = invoiceIds.join(",");
        const result = order.addPaymentline(method);
        if (!result.status) {
            this.dialog.add(AlertDialog, { title: _t("Error"), body: result.data });
            return;
        }
        result.data.setAmount(-amount);
        this.navigate("PaymentScreen", { orderUuid: order.uuid });
    },

    /** Built-in live refresh: balances move with every Customer Account order. */
    async postSyncAllOrders(orders) {
        const result = await super.postSyncAllOrders(...arguments);
        const partners = (orders || [])
            .filter(
                (order) =>
                    order.partner_id &&
                    (order.payment_ids || []).some(
                        (payment) => payment.payment_method_id?.type === "pay_later"
                    )
            )
            .map((order) => order.partner_id);
        this.diLoadBalances(partners);
        return result;
    },
});
