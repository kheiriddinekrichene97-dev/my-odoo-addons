import { Component, onWillStart, useState } from "@odoo/owl";
import { formatCurrency } from "@web/core/currency";
import { formatQty, playSuccess } from "../utils";

export class ProductLookup extends Component {
    static template = "my_barcode.ProductLookup";
    static props = ["api", "params"];

    setup() {
        this.state = useState({ product: null });
        this.formatQty = formatQty;
        this.props.api.onScan((barcode) => this.onScan(barcode));
        onWillStart(async () => {
            if (this.props.params.productId) {
                await this.load(this.props.params.productId);
            }
        });
    }

    get price() {
        const { list_price, currency_id } = this.state.product;
        return formatCurrency(list_price, currency_id);
    }

    get imageUrl() {
        return `/web/image/product.product/${this.state.product.id}/image_256`;
    }

    async load(productId) {
        const product = await this.props.api.call("product_info", [productId]);
        if (product) {
            this.state.product = product;
        }
        return product;
    }

    async onScan(barcode) {
        const result = await this.props.api.call("scan", [barcode]);
        if (!result) {
            return;
        }
        if (result.type === "product") {
            if (await this.load(result.id)) {
                playSuccess();
            }
        } else {
            this.props.api.handleScanResult(result);
        }
    }
}
