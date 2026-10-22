import { Component, onWillStart, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { formatQty, playError, playSuccess } from "../utils";

export class PickingDetail extends Component {
    static template = "my_barcode.PickingDetail";
    static props = ["api", "params"];

    setup() {
        this.notification = useService("notification");
        this.formatQty = formatQty;
        this.state = useState({ picking: null, lastMoveId: false });
        this.props.api.onScan((barcode) => this.onScan(barcode));
        onWillStart(() => this.load());
    }

    get api() {
        return this.props.api;
    }

    get progress() {
        const lines = this.state.picking.lines;
        const demand = lines.reduce((total, line) => total + line.demand, 0);
        const scanned = lines.reduce((total, line) => total + Math.min(line.scanned, line.demand), 0);
        return demand ? Math.round((scanned / demand) * 100) : 0;
    }

    lineStatus(line) {
        if (line.scanned > line.demand) {
            return "over";
        }
        return line.scanned >= line.demand ? "done" : "todo";
    }

    async load() {
        const picking = await this.api.call("picking_data", [this.props.params.pickingId]);
        if (picking) {
            this.state.picking = picking;
        }
        return picking;
    }

    async onScan(barcode) {
        if (!this.state.picking?.is_open) {
            playError();
            this.notification.add(_t("This transfer is not open anymore."), { type: "warning" });
            return;
        }
        const result = await this.api.call("picking_scan", [this.state.picking.id, barcode]);
        if (!result) {
            return;
        }
        if (result.switch_picking) {
            playSuccess();
            this.api.navigate("picking", { pickingId: result.switch_picking, code: this.props.params.code });
            return;
        }
        this.state.picking = result.picking;
        this.state.lastMoveId = result.move_id;
        if (result.extra) {
            playError();
            this.notification.add(
                _t("%s is not part of this transfer: it was added as an extra line.", result.product_name),
                { type: "warning" }
            );
        } else if (result.over) {
            playError();
            this.notification.add(_t("More %s scanned than expected.", result.product_name), {
                type: "warning",
            });
        } else {
            playSuccess();
        }
    }

    async setQty(line, quantity) {
        if (quantity < 0) {
            return;
        }
        this.state.lastMoveId = line.move_id;
        const picking = await this.api.call("picking_set_qty", [line.move_id, quantity]);
        if (picking) {
            this.state.picking = picking;
        }
    }

    onQtyChange(line, ev) {
        const quantity = parseFloat(ev.target.value);
        if (isNaN(quantity) || quantity < 0) {
            ev.target.value = line.scanned;
            return;
        }
        return this.setQty(line, quantity);
    }

    async validate() {
        const result = await this.api.call("picking_validate", [this.state.picking.id]);
        if (!result) {
            return;
        }
        if (typeof result === "object") {
            // e.g. "Create Backorder?" wizard
            await this.api.doAction(result, { onClose: () => this.afterValidate() });
        } else {
            await this.afterValidate();
        }
    }

    async afterValidate() {
        const picking = await this.load();
        if (picking && picking.state === "done") {
            playSuccess();
            this.notification.add(_t("%s validated.", picking.name), { type: "success" });
            this.api.navigate("pickings", { code: this.props.params.code });
        }
    }
}
