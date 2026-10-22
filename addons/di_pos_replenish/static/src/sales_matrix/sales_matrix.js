import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";
import { moveDownColumn, useColumnOrder } from "@di_stock_replenish/column_order/column_order";

/**
 * The daily restock grid: one table per point of sale, showing what that shop
 * sold over the period and what the source warehouses can put back.
 *
 * It borrows di_stock_arrival's grid styling through the shared .di_matrix
 * classes, so the three replenishment screens read as one tool.
 *
 * Rows arrive already grouped and already ordered - posizionamento, then
 * category, then barcode - because none of that ordering can be expressed in
 * JavaScript without shipping the taxonomy to the browser. The component
 * draws what the server sends and sends back only what was typed.
 */
export class PosSalesMatrix extends Component {
    static template = "di_pos_replenish.PosSalesMatrix";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");
        this.state = useState({
            loading: false,
            loaded: false,
            saving: false,
            configs: [],
            activeConfig: 0,
            warehouses: [],
            groups: [],
            seasons: [],
            excludedSeasons: [],
            labels: {},
            canPrint: false,
            editable: false,
            message: "",
            // Only what the worker actually changed is sent back, keyed
            // "<line id>-<warehouse id>". A key that is absent is left alone
            // server-side, so a table the worker never scrolled to cannot be
            // wiped by saving the one they did.
            edits: {},
            rowFilter: "",
            // Rows sold with no free stock at any source, left off the
            // active table - see _unstocked_line_ids.
            hiddenUnstocked: 0,
        });
        // The order the worker dragged the columns into, remembered in this
        // browser - the same mechanism as the stock comparison's, under a
        // key of its own. See di_stock_replenish's column_order.js.
        this.columnOrder = useColumnOrder("di_pos_replenish.column_order");
        onWillStart(() => this.load());
    }

    get resId() {
        return this.props.record.resId;
    }

    async load(configId = null, excludedSeasons = null) {
        if (!this.resId) {
            return;
        }
        this.state.loading = true;
        try {
            const kwargs = {};
            if (configId !== null) {
                kwargs.config_id = configId;
            }
            if (excludedSeasons !== null) {
                kwargs.excluded_seasons = excludedSeasons;
            }
            const data = await this.orm.call(
                "di.pos.replenish",
                "get_sales_matrix",
                [this.resId],
                kwargs
            );
            this.apply(data);
            this.state.loaded = true;
        } finally {
            this.state.loading = false;
        }
    }

    apply(data) {
        Object.assign(this.state, data, {
            activeConfig: data.active_config,
            excludedSeasons: data.excluded_seasons || [],
            canPrint: data.can_print,
            editable: data.editable,
            hiddenUnstocked: data.hidden_unstocked || 0,
            edits: {},
        });
    }

    // -- points of sale ----------------------------------------------------

    /** Switching shops discards nothing: unsaved edits belong to the table
     * that is leaving the screen, so they are saved first rather than lost. */
    async selectConfig(configId) {
        if (configId === this.state.activeConfig || this.state.loading) {
            return;
        }
        if (this.hasEdits && !(await this.saveQuantities(configId))) {
            return;
        }
        await this.load(configId);
    }

    isActiveConfig(configId) {
        return configId === this.state.activeConfig;
    }

    get activeConfigName() {
        const config = this.state.configs.find(
            (entry) => entry.id === this.state.activeConfig
        );
        return config ? config.name : "";
    }

    // -- seasons -----------------------------------------------------------

    isSeasonDropped(seasonId) {
        return this.state.excludedSeasons.includes(seasonId);
    }

    /** Dropping a season hides its rows everywhere and keeps them out of both
     * the cartellini and the transfers. It is a filter, not a delete: the
     * rows and any quantity already adjusted come straight back if it is
     * picked up again. */
    async toggleSeason(seasonId) {
        if (this.state.loading) {
            return;
        }
        if (this.hasEdits && !(await this.saveQuantities())) {
            return;
        }
        const dropped = this.isSeasonDropped(seasonId)
            ? this.state.excludedSeasons.filter((id) => id !== seasonId)
            : [...this.state.excludedSeasons, seasonId];
        await this.load(this.state.activeConfig, dropped);
    }

    async restoreAllSeasons() {
        if (!this.state.excludedSeasons.length || this.state.loading) {
            return;
        }
        await this.load(this.state.activeConfig, []);
    }

    // -- quantities --------------------------------------------------------

    key(rowId, warehouseId) {
        return `${rowId}-${warehouseId}`;
    }

    /** The pending edit if there is one, otherwise what the server sent. */
    take(row, warehouseId) {
        const key = this.key(row.id, warehouseId);
        if (key in this.state.edits) {
            return this.state.edits[key];
        }
        return (row.take || {})[String(warehouseId)] || 0;
    }

    takeDisplay(row, warehouseId) {
        const value = this.take(row, warehouseId);
        return value ? this.format(value) : "";
    }

    available(row, warehouseId) {
        return (row.available || {})[String(warehouseId)] || 0;
    }

    /** Everything this row sends, across every source on screen. */
    rowTotal(row) {
        return this.state.warehouses.reduce(
            (sum, warehouse) => sum + this.take(row, warehouse.id),
            0
        );
    }

    /** Sold more than the sources can replace, so the shop stays short. */
    isShort(row) {
        return Math.round((row.net - this.rowTotal(row)) * 1000) > 0;
    }

    /** Asked for more of this row than that one warehouse actually holds.
     * The server refuses it again on save, across the whole run rather than
     * one row - this only tells the worker while they are looking at it. */
    isOverAvailable(row, warehouseId) {
        return (
            Math.round(
                (this.take(row, warehouseId) - this.available(row, warehouseId)) * 1000
            ) > 0
        );
    }

    get hasEdits() {
        return Object.keys(this.state.edits).length > 0;
    }

    /** Pieces of clothing are whole things: a quantity that happens to be
     * integral prints without the decimal noise a raw Number would carry. */
    format(value) {
        return Number.isInteger(value) ? String(value) : String(Math.round(value * 1000) / 1000);
    }

    onQuantityChange(row, warehouseId, ev) {
        const raw = (ev.target.value || "").trim().replace(",", ".");
        let value = 0;
        if (raw !== "") {
            value = Number.parseFloat(raw);
            if (Number.isNaN(value) || value < 0) {
                ev.target.value = this.takeDisplay(row, warehouseId);
                this.notification.add(_t("Type a quantity of zero or more."), {
                    type: "warning",
                });
                return;
            }
        }
        this.state.edits[this.key(row.id, warehouseId)] = value;
        ev.target.value = value ? this.format(value) : "";

        if (Math.round((this.rowTotal(row) - row.net) * 1000) > 0) {
            this.notification.add(
                _t("More than this shop sold."),
                { type: "warning" }
            );
        } else if (this.isOverAvailable(row, warehouseId)) {
            this.notification.add(
                _t("More than that warehouse has free."),
                { type: "warning" }
            );
        }
    }

    /** Fill the row from the sources, as the proposal did. Saves typing when
     * a worker has cleared a row and wants it back. */
    resetRow(row) {
        if (!this.state.editable) {
            return;
        }
        let left = row.net;
        for (const warehouse of this.state.warehouses) {
            const take = Math.max(Math.min(left, this.available(row, warehouse.id)), 0);
            this.state.edits[this.key(row.id, warehouse.id)] = take;
            left -= take;
        }
    }

    clearRow(row) {
        if (!this.state.editable) {
            return;
        }
        for (const warehouse of this.state.warehouses) {
            this.state.edits[this.key(row.id, warehouse.id)] = 0;
        }
    }

    /** Enter moves down this warehouse's column - see moveDownColumn. */
    onQuantityKeydown(ev) {
        moveDownColumn(ev, "input.di_sales_input");
    }

    async saveQuantities(configId = null) {
        if (!this.hasEdits || this.state.saving) {
            return true;
        }
        this.state.saving = true;
        try {
            const data = await this.orm.call(
                "di.pos.replenish",
                "set_take_cells",
                [this.resId, this.state.edits],
                { config_id: configId === null ? this.state.activeConfig : configId }
            );
            this.apply(data);
            await this.reloadRecord();
            this.notification.add(_t("Quantities saved."), { type: "success" });
            return true;
        } finally {
            // A refusal from the server - over the free stock, most likely -
            // propagates to the usual error dialog with the edits still on
            // screen, so the worker corrects the number rather than retyping
            // the lot.
            this.state.saving = false;
        }
    }

    discardQuantities() {
        this.state.edits = {};
    }

    /** Bring the form back in step, so the All Rows tab and the footer totals
     * agree with the grid. */
    async reloadRecord() {
        const record = this.props.record;
        try {
            if (record.model && record.model.root && record.model.root.load) {
                await record.model.root.load();
            } else if (record.load) {
                await record.load();
            }
            if (record.model && record.model.notify) {
                record.model.notify();
            }
        } catch {
            // The grid already shows what was written; the tab catches up on
            // the next manual reload.
        }
    }

    // -- printing ----------------------------------------------------------

    /** Cartellini for the shop on screen: one label per piece, in barcode
     * order, which is the order the warehouse picks in. */
    async printTags() {
        if (this.hasEdits && !(await this.saveQuantities())) {
            return;
        }
        const action = await this.orm.call(
            "di.pos.replenish",
            "action_print_tags",
            [this.resId],
            { config_id: this.state.activeConfig }
        );
        if (action) {
            await this.action.doAction(action);
        }
    }

    // -- filtering and refresh ---------------------------------------------

    /** Typed filter over the rows on screen, applied per group so a heading
     * with nothing left under it disappears with its rows. */
    get filteredGroups() {
        const needle = (this.state.rowFilter || "").trim().toLowerCase();
        if (!needle) {
            return this.state.groups;
        }
        const groups = [];
        for (const group of this.state.groups) {
            const rows = group.rows.filter(
                (row) =>
                    (row.name || "").toLowerCase().includes(needle) ||
                    (row.code || "").toLowerCase().includes(needle) ||
                    (row.barcode || "").toLowerCase().includes(needle) ||
                    (row.season || "").toLowerCase().includes(needle)
            );
            if (rows.length) {
                groups.push({ ...group, rows });
            }
        }
        return groups;
    }

    /** Pieces sold, not rows: the figure the shop compares against its till.
     * Unfiltered it is the tab's own total, which also nets the returns that
     * have no row; while a search narrows the table it is what the rows left
     * on screen add up to. */
    get visibleSold() {
        const config = this.state.configs.find((entry) =>
            this.isActiveConfig(entry.id)
        );
        if (config && !(this.state.rowFilter || "").trim()) {
            return this.format(config.sold);
        }
        return this.format(
            this.filteredGroups.reduce(
                (total, group) =>
                    total + group.rows.reduce((sum, row) => sum + (row.net || 0), 0),
                0
            )
        );
    }

    // -- columns -----------------------------------------------------------

    /**
     * Every column after the frozen product one, in the default order. The
     * product column stays put: it is the sticky one, and a row would lose
     * its name if it scrolled away.
     */
    get defaultColumns() {
        const labels = this.state.labels;
        return [
            { key: "barcode", label: labels.barcode },
            { key: "season", label: labels.season },
            { key: "net", label: labels.net },
            ...this.state.warehouses.map((warehouse) => ({
                key: `src-${warehouse.id}`,
                type: "source",
                id: warehouse.id,
                label: warehouse.name,
                title: warehouse.code,
            })),
            { key: "to_send", label: labels.to_send },
        ];
    }

    get defaultKeys() {
        return this.defaultColumns.map((column) => column.key);
    }

    /** The columns in the order the worker arranged them. Read once per
     * render (t-set in the template), not once per row. */
    get columns() {
        const byKey = Object.fromEntries(
            this.defaultColumns.map((column) => [column.key, column])
        );
        return this.columnOrder.keys(this.defaultKeys).map((key) => byKey[key]);
    }

    get resetColumnsTitle() {
        return _t("Reset column order");
    }

    /** Said rather than silently dropped, so nobody wonders why the table is
     * shorter than the till receipt. */
    get hiddenUnstockedLabel() {
        return _t("%s sold with no free stock at the sources: not shown.",
            this.state.hiddenUnstocked);
    }

    /** The period and the scope live on the record, so they have to be saved
     * before the server can be asked to read them back. */
    async refresh() {
        const record = this.props.record;
        try {
            if (record.dirty && record.save) {
                const saved = await record.save();
                if (saved === false) {
                    return;
                }
            }
        } catch {
            return;
        }
        if (!this.resId) {
            this.notification.add(_t("Save the run first."), {
                type: "warning",
            });
            return;
        }
        await this.load(this.state.activeConfig);
    }
}

export const posSalesMatrixField = {
    component: PosSalesMatrix,
    supportedTypes: ["char"],
};

registry.category("fields").add("di_pos_sales_matrix", posSalesMatrixField);
