import { Component, onWillStart, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { useDebounced } from "@web/core/utils/timing";
import { playError, playSuccess } from "../utils";

/**
 * Pick a product then scan its barcode, or scan first then pick the product.
 */
export class AssignBarcode extends Component {
    static template = "my_barcode.AssignBarcode";
    static props = ["api", "params"];

    setup() {
        this.notification = useService("notification");
        this.state = useState({
            search: "",
            onlyMissing: true,
            products: [],
            selected: null,
            pendingBarcode: this.props.params.barcode || "",
        });
        this.debouncedLoad = useDebounced(() => this.load(), 300);
        this.props.api.onScan((barcode) => this.onScan(barcode));
        onWillStart(async () => {
            const productId = this.props.params.productId;
            if (productId) {
                const product = await this.props.api.call("product_info", [productId]);
                if (product) {
                    this.state.selected = product;
                }
            }
            await this.load();
        });
    }

    async load() {
        const products = await this.props.api.call(
            "assign_candidates",
            [this.state.search, this.state.onlyMissing]
        );
        if (products) {
            this.state.products = products;
        }
    }

    onSearchInput(ev) {
        this.state.search = ev.target.value;
        this.debouncedLoad();
    }

    toggleOnlyMissing(ev) {
        this.state.onlyMissing = ev.target.checked;
        this.load();
    }

    select(product) {
        if (this.state.pendingBarcode) {
            return this.assign(product, this.state.pendingBarcode);
        }
        this.state.selected = product;
    }

    onScan(barcode) {
        if (this.state.selected) {
            return this.assign(this.state.selected, barcode);
        }
        playSuccess();
        this.state.pendingBarcode = barcode;
    }

    async assign(product, barcode) {
        const result = await this.props.api.call("assign_barcode", [product.id, barcode]);
        if (!result) {
            return;
        }
        playSuccess();
        this.notification.add(
            _t("Barcode %(barcode)s assigned to %(product)s.", {
                barcode: result.barcode,
                product: result.display_name,
            }),
            { type: "success" }
        );
        this.state.pendingBarcode = "";
        this.state.selected = null;
        await this.load();
        // Chain to the next product still missing a barcode.
        if (this.state.onlyMissing && this.state.products.length) {
            this.state.selected = this.state.products[0];
        }
    }

    cancel() {
        if (!this.state.selected && !this.state.pendingBarcode) {
            return;
        }
        this.state.selected = null;
        this.state.pendingBarcode = "";
        playError();
    }
}
