import { Component, onWillStart, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { deserializeDateTime, formatDateTime } from "@web/core/l10n/dates";
import { useService } from "@web/core/utils/hooks";
import { playError, playSuccess } from "../utils";

export const PICKING_KINDS = [
    { code: "incoming", icon: "fa-download", label: _t("Receipts") },
    { code: "outgoing", icon: "fa-truck", label: _t("Deliveries") },
    { code: "internal", icon: "fa-exchange", label: _t("Internal Transfers") },
];

export class PickingList extends Component {
    static template = "my_barcode.PickingList";
    static props = ["api", "params"];

    setup() {
        this.notification = useService("notification");
        this.state = useState({ pickings: [], loaded: false });
        this.props.api.onScan((barcode) => this.onScan(barcode));
        onWillStart(() => this.load());
    }

    async load() {
        const pickings = await this.props.api.call("picking_list", [this.props.params.code || false]);
        if (pickings) {
            this.state.pickings = pickings;
        }
        this.state.loaded = true;
    }

    formatDate(value) {
        return value ? formatDateTime(deserializeDateTime(value)) : "";
    }

    open(pickingId) {
        this.props.api.navigate("picking", { pickingId, code: this.props.params.code });
    }

    async onScan(barcode) {
        const result = await this.props.api.call("scan", [barcode]);
        if (!result) {
            return;
        }
        if (result.type === "picking") {
            playSuccess();
            this.open(result.id);
        } else if (result.type === "unknown") {
            this.props.api.notifyUnknown(barcode);
        } else {
            playError();
            this.notification.add(_t("Open a transfer first, then scan its products."), {
                type: "warning",
            });
        }
    }
}
