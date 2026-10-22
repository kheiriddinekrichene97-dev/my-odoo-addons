import { Component, onWillStart, useState } from "@odoo/owl";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { formatQty, playSuccess } from "../utils";

export class InventoryCount extends Component {
    static template = "my_barcode.InventoryCount";
    static props = ["api", "params"];

    setup() {
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.formatQty = formatQty;
        this.state = useState({ locations: [], locationId: false, lines: [], lastProductId: false });
        this.props.api.onScan((barcode) => this.onScan(barcode));
        onWillStart(async () => {
            const result = await this.props.api.call("count_locations");
            if (result) {
                this.state.locations = result.locations;
                this.state.locationId = this.props.params.locationId || result.default_id;
                await this.loadLines();
            }
        });
    }

    get api() {
        return this.props.api;
    }

    get locationName() {
        return this.state.locations.find((l) => l.id === this.state.locationId)?.name || "";
    }

    /** Runs a count RPC returning the updated lines, and refreshes the list. */
    async update(method, args) {
        const lines = await this.api.call(method, [this.state.locationId, ...args]);
        if (lines) {
            this.state.lines = lines;
        }
        return lines;
    }

    loadLines() {
        return this.update("count_lines", []);
    }

    async switchLocation(locationId) {
        this.state.locationId = locationId;
        this.state.lastProductId = false;
        await this.loadLines();
    }

    async onScan(barcode) {
        const result = await this.api.call("scan", [barcode]);
        if (!result) {
            return;
        }
        if (result.type === "product") {
            if (await this.update("count_add", [result.id, 1])) {
                this.state.lastProductId = result.id;
                playSuccess();
            }
        } else if (result.type === "location") {
            playSuccess();
            await this.switchLocation(result.id);
        } else if (result.type === "unknown") {
            this.api.notifyUnknown(barcode);
        } else {
            this.api.handleScanResult(result);
        }
    }

    onLocationChange(ev) {
        this.switchLocation(parseInt(ev.target.value));
    }

    increment(line, delta) {
        this.state.lastProductId = line.product_id;
        return this.update("count_add", [line.product_id, delta]);
    }

    onQtyChange(line, ev) {
        const quantity = parseFloat(ev.target.value);
        if (isNaN(quantity) || quantity < 0) {
            ev.target.value = line.counted;
            return;
        }
        this.state.lastProductId = line.product_id;
        return this.update("count_add", [line.product_id, quantity, true]);
    }

    remove(line) {
        return this.update("count_remove", [line.quant_id]);
    }

    discard() {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Discard count"),
            body: _t("Forget all the quantities counted in %s?", this.locationName),
            confirmLabel: _t("Discard"),
            confirm: () => this.update("count_discard", []),
            cancel: () => {},
        });
    }

    apply() {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Apply count"),
            body: _t(
                "Update the stock of %(count)s product(s) in %(location)s to the counted quantities?",
                { count: this.state.lines.length, location: this.locationName }
            ),
            confirmLabel: _t("Apply"),
            confirm: () => this.doApply(),
            cancel: () => {},
        });
    }

    async doApply() {
        const result = await this.api.call("count_apply", [this.state.locationId]);
        if (!result) {
            return;
        }
        if (typeof result === "object") {
            // e.g. the conflict wizard when stock moved since the count started
            await this.api.doAction(result, { onClose: () => this.loadLines() });
            return;
        }
        this.notification.add(_t("Inventory updated."), { type: "success" });
        await this.loadLines();
    }
}
