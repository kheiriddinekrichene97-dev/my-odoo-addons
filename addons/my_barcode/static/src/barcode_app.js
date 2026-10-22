import { Component, useState } from "@odoo/owl";
import { scanBarcode } from "@web/core/barcode/barcode_dialog";
import { isBarcodeScannerSupported } from "@web/core/barcode/barcode_video_scanner";
import { _t } from "@web/core/l10n/translation";
import { RPCError } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { useBus, useService } from "@web/core/utils/hooks";
import { AssignBarcode } from "./screens/assign_barcode";
import { InventoryCount } from "./screens/inventory_count";
import { PickingDetail } from "./screens/picking_detail";
import { PickingList, PICKING_KINDS } from "./screens/picking_list";
import { ProductLookup } from "./screens/product_lookup";
import { playError, playSuccess } from "./utils";

const SCREENS = {
    lookup: { Component: ProductLookup, title: _t("Product Lookup") },
    count: { Component: InventoryCount, title: _t("Inventory Count") },
    pickings: { Component: PickingList, title: _t("Transfers") },
    picking: { Component: PickingDetail, title: _t("Transfer") },
    assign: { Component: AssignBarcode, title: _t("Assign Barcodes") },
};

// Screen to reopen when coming back (breadcrumb) from a record opened in the scanner.
let screenToRestore = null;

export class BarcodeApp extends Component {
    static template = "my_barcode.BarcodeApp";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.cameraSupported = isBarcodeScannerSupported();
        this.state = useState({ screen: "home", params: {}, screenKey: 0, manualCode: "" });
        if (screenToRestore) {
            Object.assign(this.state, screenToRestore);
            screenToRestore = null;
        }
        this.scanHandler = null;
        this.scanQueue = Promise.resolve();
        // API handed to every screen.
        this.api = {
            call: this.call.bind(this),
            navigate: this.navigate.bind(this),
            onScan: (handler) => (this.scanHandler = handler),
            handleScanResult: this.handleScanResult.bind(this),
            notifyUnknown: this.notifyUnknown.bind(this),
            openRecord: this.openRecord.bind(this),
            doAction: (action, options) => this.action.doAction(action, options),
        };
        this.tiles = [
            { screen: "lookup", icon: "fa-search", label: _t("Product Lookup") },
            { screen: "count", icon: "fa-cubes", label: _t("Inventory Count") },
            ...PICKING_KINDS.map((kind) => ({
                screen: "pickings",
                params: { code: kind.code },
                icon: kind.icon,
                label: kind.label,
            })),
            { screen: "assign", icon: "fa-barcode", label: _t("Assign Barcodes") },
        ];
        useBus(useService("barcode").bus, "barcode_scanned", (ev) => this.onBarcode(ev.detail.barcode));
    }

    get screen() {
        return SCREENS[this.state.screen];
    }

    get title() {
        if (this.state.screen === "pickings") {
            const kind = PICKING_KINDS.find((k) => k.code === this.state.params.code);
            return kind ? kind.label : this.screen.title;
        }
        return this.screen ? this.screen.title : _t("Barcode");
    }

    navigate(screen, params = {}) {
        this.scanHandler = null;
        this.state.screen = screen;
        this.state.params = params;
        this.state.screenKey++;
    }

    goBack() {
        if (this.state.screen === "picking") {
            this.navigate("pickings", { code: this.state.params.code });
        } else {
            this.navigate("home");
        }
    }

    /**
     * Calls the scanner API. Business errors (UserError, ValidationError, ...)
     * are shown as a notification with an error sound, and resolve to undefined.
     */
    async call(method, args = [], kwargs = {}) {
        try {
            return await this.orm.call("my_barcode.scanner", method, args, kwargs);
        } catch (error) {
            if (error instanceof RPCError && error.exceptionName?.startsWith("odoo.exceptions.")) {
                playError();
                this.notification.add(error.data.message, { type: "danger" });
                return undefined;
            }
            throw error;
        }
    }

    /** Scans are processed one at a time, in order, even when they arrive quickly. */
    onBarcode(barcode) {
        barcode = (barcode || "").trim();
        if (!barcode) {
            return;
        }
        const run = this.scanQueue.then(() =>
            this.scanHandler ? this.scanHandler(barcode) : this.globalScan(barcode)
        );
        this.scanQueue = run.catch(() => {});
        return run;
    }

    async globalScan(barcode) {
        const result = await this.call("scan", [barcode]);
        if (result) {
            this.handleScanResult(result);
        }
    }

    handleScanResult(result) {
        const routes = {
            product: () => this.navigate("lookup", { productId: result.id }),
            location: () => this.navigate("count", { locationId: result.id }),
            picking: () => this.navigate("picking", { pickingId: result.id }),
        };
        if (routes[result.type]) {
            playSuccess();
            routes[result.type]();
        } else {
            this.notifyUnknown(result.barcode);
        }
    }

    notifyUnknown(barcode) {
        playError();
        const close = this.notification.add(_t("Nothing matches the barcode %s.", barcode), {
            type: "warning",
            buttons: [
                {
                    name: _t("Assign it to a product"),
                    primary: true,
                    onClick: () => {
                        close();
                        this.navigate("assign", { barcode });
                    },
                },
            ],
        });
    }

    openRecord(resModel, resId) {
        screenToRestore = {
            screen: this.state.screen,
            params: this.state.params,
            screenKey: this.state.screenKey + 1,
        };
        return this.action.doAction({
            type: "ir.actions.act_window",
            res_model: resModel,
            res_id: resId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    onManualSubmit() {
        const code = this.state.manualCode;
        this.state.manualCode = "";
        this.onBarcode(code);
    }

    async openCamera() {
        let barcode;
        try {
            barcode = await scanBarcode(this.env);
        } catch (error) {
            this.notification.add(error?.message || _t("Could not access the camera."), {
                type: "danger",
            });
            return;
        }
        this.onBarcode(barcode);
    }
}

registry.category("actions").add("my_barcode.app", BarcodeApp);
