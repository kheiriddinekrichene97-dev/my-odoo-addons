import { Component, onWillStart, useEffect, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";

/**
 * Distribution grid: one row per arrival line, one column per destination
 * warehouse. It replaces a list where a hundred-product arrival meant a
 * hundred rows to scroll for every shop.
 *
 * The component owns no business rules. It reads through
 * `get_distribution_matrix` and writes through `set_distribution_matrix`, so
 * the allocation constraints on the server stay the only thing deciding what
 * is legal; over-allocation shown here in red is a courtesy, not the guard.
 */
export class DistributionMatrix extends Component {
    static template = "di_stock_arrival.DistributionMatrix";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({
            loading: true,
            saving: false,
            dirty: false,
            editable: false,
            lines: [],
            warehouses: [],
            selected: [],
            cells: {},
            rowFilter: "",
            colFilter: "",
            fillValue: "",
            arrivalWarehouse: "",
        });
        // Set for real by applyMatrix; an empty map keeps a save before the
        // first load from reading undefined.
        this.serverReceived = new Map();
        onWillStart(() => this.loadMatrix());
        // Follow the quantities in the Products Received tab. Recomputing the
        // fingerprint on every render is what subscribes this grid to the
        // form's own record; the effect then runs only when a quantity there
        // actually moved.
        useEffect(
            () => {
                this.syncReceivedFromRecord();
            },
            () => [this.receivedFingerprint]
        );
    }

    // -- the other tab ----------------------------------------------------

    /**
     * The received quantities as the form record currently holds them.
     *
     * A string rather than the list itself: reloading the record builds new
     * objects every time, so comparing identity would re-run the effect on
     * every save, while comparing the values re-runs it only when a quantity
     * really changed.
     */
    get receivedFingerprint() {
        const lines = this.props.record.data.line_ids;
        const records = (lines && lines.records) || [];
        return records.map((line) => `${line.resId}:${line.data.quantity}`).join("|");
    }

    /**
     * Carry a correction made in Products Received into the Received column.
     *
     * The grid reads its numbers once, through `get_distribution_matrix`, so
     * a quantity corrected in the other tab used to sit here stale until the
     * page was reloaded - and a distribution typed against the old number
     * came back refused by the server. The form's record already carries
     * those quantities, so the column follows them with no round trip, and
     * without touching cells that have not been saved yet. It follows them
     * before they are saved, too, which is right: `save()` writes the form
     * first, so what the grid is showing is what the server will check.
     */
    syncReceivedFromRecord() {
        if (this.state.loading || !this.resId) {
            return;
        }
        const lines = this.props.record.data.line_ids;
        const records = (lines && lines.records) || [];
        const known = new Map(this.state.lines.map((line) => [line.id, line]));
        let unknown = false;
        for (const record of records) {
            if (!record.resId) {
                // A row still being typed has no id to match a column on.
                continue;
            }
            const line = known.get(record.resId);
            if (!line) {
                unknown = true;
                continue;
            }
            const received = record.data.quantity;
            if (typeof received === "number" && received !== line.received) {
                line.received = received;
            }
        }
        // A line the grid has never seen means the document changed shape,
        // which only a reload can give a row to. Never while there are
        // unsaved cells: reloading would throw them away.
        if (unknown && !this.state.dirty) {
            this.loadMatrix();
        }
    }

    get resId() {
        return this.props.record.resId;
    }

    async loadMatrix() {
        if (!this.resId) {
            this.state.loading = false;
            return;
        }
        const data = await this.orm.call(
            "di.stock.arrival", "get_distribution_matrix", [this.resId]
        );
        this.applyMatrix(data);
    }

    applyMatrix(data) {
        this.state.lines = data.lines;
        // What the server last told us each line received. Anything that
        // differs from this is a correction the worker has made in the other
        // tab and not saved, which the next save has to carry with it.
        this.serverReceived = new Map(data.lines.map((line) => [line.id, line.received]));
        this.state.warehouses = data.warehouses;
        this.state.cells = data.cells || {};
        // Start on the destinations this arrival already sends to. Opening
        // thirty empty columns is what made the grid unusable; a worker picks
        // the handful of shops they are packing for today.
        const used = new Set(
            Object.keys(this.state.cells).map((key) => Number(key.split("-")[1]))
        );
        this.state.selected = data.warehouses
            .filter((warehouse) => used.has(warehouse.id))
            .map((warehouse) => warehouse.id);
        this.state.editable = data.editable;
        this.state.arrivalWarehouse = data.arrival_warehouse || "";
        this.state.loading = false;
        this.state.dirty = false;
    }

    // -- cells ------------------------------------------------------------

    key(lineId, warehouseId) {
        return `${lineId}-${warehouseId}`;
    }

    cell(lineId, warehouseId) {
        return this.state.cells[this.key(lineId, warehouseId)] || 0;
    }

    /** Blank rather than a column of zeroes: only the filled cells matter. */
    cellDisplay(lineId, warehouseId) {
        const value = this.cell(lineId, warehouseId);
        return value ? this.format(value) : "";
    }

    format(value) {
        return Number.isInteger(value) ? String(value) : String(Math.round(value * 1000) / 1000);
    }

    onCellChange(lineId, warehouseId, ev) {
        // Workers type on an Italian keyboard, where the decimal key is a comma.
        const raw = (ev.target.value || "").trim().replace(",", ".");
        const key = this.key(lineId, warehouseId);
        if (raw === "") {
            delete this.state.cells[key];
        } else {
            const value = Number.parseFloat(raw);
            if (Number.isNaN(value) || value < 0) {
                ev.target.value = this.cellDisplay(lineId, warehouseId);
                this.notification.add(
                    _t("Type a quantity of zero or more."),
                    { type: "warning" }
                );
                return;
            }
            if (value === 0) {
                delete this.state.cells[key];
            } else {
                this.state.cells[key] = value;
            }
        }
        this.state.dirty = true;
        ev.target.value = this.cellDisplay(lineId, warehouseId);
    }

    // -- bulk fill --------------------------------------------------------

    /**
     * Same quantity into every cell on screen. A shop taking one piece of each
     * of twenty products meant twenty identical numbers typed by hand, per
     * shop; here the worker picks the destinations, narrows the rows with the
     * product filter if they want, types the quantity once and presses OK.
     *
     * It writes over what is already in those cells rather than only filling
     * the blanks, so the result is exactly what the worker typed and not a
     * mixture of the two. Nothing reaches the database until Save
     * Distribution, so Discard undoes the whole fill.
     */
    applyFill() {
        if (this.fill(this.visibleWarehouses, this.state.fillValue)) {
            this.state.fillValue = "";
        }
    }

    /**
     * The same, down a single destination.
     *
     * A shop rarely takes the same number as its neighbours: one wants two of
     * each, the next five. Filling the whole grid and correcting a column
     * afterwards is the long way round, so each destination heading carries
     * its own box, which writes that column and leaves every other one as it
     * stands.
     */
    applyColumnFill(warehouseId, input) {
        const warehouse = this.state.warehouses.find((w) => w.id === warehouseId);
        // The box keeps its own value in the DOM rather than in state: it is
        // emptied the moment it is used, and nothing else ever reads it.
        if (warehouse && this.fill([warehouse], input.value)) {
            input.value = "";
        }
    }

    /**
     * Write one quantity into every visible row of the given destinations.
     *
     * Returns true when something was written, so the box that asked can
     * clear itself. It writes over what is already in those cells rather than
     * only filling the blanks, so the result is exactly what the worker typed
     * and not a mixture of the two. Nothing reaches the database until Save
     * Distribution, so Discard undoes the whole fill.
     */
    fill(warehouses, rawValue) {
        if (!this.state.editable) {
            return false;
        }
        const lines = this.visibleLines;
        if (!lines.length || !warehouses.length) {
            this.notification.add(
                _t("Pick at least one destination first."),
                { type: "warning" }
            );
            return false;
        }
        // Italian keyboard again: the decimal key is a comma.
        const raw = (rawValue || "").trim().replace(",", ".");
        const value = raw === "" ? NaN : Number.parseFloat(raw);
        if (Number.isNaN(value) || value < 0) {
            this.notification.add(
                _t("Type a quantity of zero or more."),
                { type: "warning" }
            );
            return false;
        }
        for (const line of lines) {
            for (const warehouse of warehouses) {
                const key = this.key(line.id, warehouse.id);
                // Zero is how the grid spells "send nothing", so filling with
                // zero clears the block instead of writing a wall of zeroes.
                if (value === 0) {
                    delete this.state.cells[key];
                } else {
                    this.state.cells[key] = value;
                }
            }
        }
        this.state.dirty = true;
        // One destination reads differently from all of them, and a worker
        // filling column by column needs to see which one just moved.
        const whole = warehouses.length === this.visibleWarehouses.length;
        let message;
        if (value === 0) {
            message = whole
                ? _t("Cleared every cell shown.")
                : _t("Cleared that destination.");
        } else {
            message = whole
                ? _t("Quantity copied into every cell shown.")
                : _t("Quantity copied down that destination.");
        }
        this.notification.add(message, { type: "success" });
        return true;
    }

    onFillKeydown(ev) {
        if (ev.key === "Enter") {
            // Enter in a field inside the form would otherwise be the form's.
            ev.preventDefault();
            ev.stopPropagation();
            this.applyFill();
        }
    }

    onColumnFillKeydown(warehouseId, ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            ev.stopPropagation();
            this.applyColumnFill(warehouseId, ev.target);
        }
    }

    /** Clicking away after typing applies it too, so the box cannot look ignored. */
    onColumnFillChange(warehouseId, ev) {
        if ((ev.target.value || "").trim() === "") {
            // Emptied by the Enter that just used it, or never filled in.
            return;
        }
        this.applyColumnFill(warehouseId, ev.target);
    }

    // -- totals -----------------------------------------------------------

    allocated(line) {
        let total = 0;
        for (const warehouse of this.state.warehouses) {
            total += this.cell(line.id, warehouse.id);
        }
        return total;
    }

    remaining(line) {
        return line.received - this.allocated(line);
    }

    /** Rounded before comparing: 0.1 + 0.2 must not read as over-allocated. */
    isOver(line) {
        return Math.round((this.allocated(line) - line.received) * 1000) > 0;
    }

    /** Blank rather than a row of zeroes under columns nothing was sent to. */
    totalDisplay(warehouseId) {
        const total = this.warehouseTotal(warehouseId);
        return total ? this.format(total) : "";
    }

    warehouseTotal(warehouseId) {
        let total = 0;
        for (const line of this.state.lines) {
            total += this.cell(line.id, warehouseId);
        }
        return total;
    }

    get totalReceived() {
        return this.state.lines.reduce((sum, line) => sum + line.received, 0);
    }

    get totalAllocated() {
        return this.state.lines.reduce((sum, line) => sum + this.allocated(line), 0);
    }

    get hasOverAllocation() {
        return this.state.lines.some((line) => this.isOver(line));
    }

    // -- destinations -----------------------------------------------------

    isSelected(warehouseId) {
        return this.state.selected.includes(warehouseId);
    }

    toggleWarehouse(warehouseId) {
        const index = this.state.selected.indexOf(warehouseId);
        if (index === -1) {
            // Keep columns in the order the server listed them, so adding a
            // destination does not reshuffle the grid under the worker.
            const order = this.state.warehouses.map((w) => w.id);
            this.state.selected = [...this.state.selected, warehouseId].sort(
                (a, b) => order.indexOf(a) - order.indexOf(b)
            );
        } else {
            this.state.selected = this.state.selected.filter(
                (id) => id !== warehouseId
            );
        }
    }

    selectAll() {
        this.state.selected = this.pickableWarehouses.map((w) => w.id);
    }

    clearSelection() {
        // Only drop what is on screen, so a filtered view cannot silently
        // hide columns that stay selected.
        const dropped = this.pickableWarehouses.map((w) => w.id);
        this.state.selected = this.state.selected.filter(
            (id) => !dropped.includes(id)
        );
    }

    /** True when a destination holds quantities but is not shown. */
    hasHiddenValues(warehouseId) {
        return !this.isSelected(warehouseId) && this.warehouseTotal(warehouseId) > 0;
    }

    // -- filters ----------------------------------------------------------

    get visibleLines() {
        const needle = this.state.rowFilter.trim().toLowerCase();
        if (!needle) {
            return this.state.lines;
        }
        return this.state.lines.filter(
            (line) =>
                line.name.toLowerCase().includes(needle) ||
                (line.code || "").toLowerCase().includes(needle)
        );
    }

    /** The destinations offered as chips: everything, narrowed by the filter. */
    get pickableWarehouses() {
        const needle = this.state.colFilter.trim().toLowerCase();
        if (!needle) {
            return this.state.warehouses;
        }
        return this.state.warehouses.filter(
            (warehouse) =>
                warehouse.code.toLowerCase().includes(needle) ||
                warehouse.name.toLowerCase().includes(needle)
        );
    }

    /** The columns actually drawn: the ones picked, in server order. */
    get visibleWarehouses() {
        return this.state.warehouses.filter((warehouse) =>
            this.state.selected.includes(warehouse.id)
        );
    }

    // -- saving -----------------------------------------------------------

    /**
     * Received quantities the worker has changed and not saved yet.
     *
     * Sent with the cells so both land in one transaction. Only the lines
     * that actually differ: the grid must not write back a quantity nobody
     * touched, which would overwrite whatever someone else has done to it
     * since this grid was loaded.
     */
    pendingQuantities() {
        const pending = {};
        for (const line of this.state.lines) {
            const saved = this.serverReceived.get(line.id);
            if (saved !== undefined && line.received !== saved) {
                pending[line.id] = line.received;
            }
        }
        return pending;
    }

    /**
     * Save the rest of the form, after the grid has written.
     *
     * Afterwards, not before. The grid used to save the form first, so
     * correcting a line to zero and clearing its cells in one go wrote the
     * zero while the old allocations were still there and the arrival was
     * refused for distributing more than arrived. The quantities now travel
     * with the cells, and this only persists whatever else the form is
     * holding - the line it writes again carries the value just written.
     */
    async saveRecord() {
        const record = this.props.record;
        try {
            if (record.dirty && record.save) {
                await record.save();
            }
        } catch {
            // The grid's own write is already committed and consistent; the
            // form keeps its unsaved fields and says so.
        }
    }

    /**
     * Pull the form back in step after the grid has written. The reload API
     * has moved between versions, so each shape is tried in turn and the grid
     * refreshes itself either way.
     */
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
            // The grid below is authoritative for what was just written; the
            // footer totals catch up on the next manual reload.
        }
    }

    async save() {
        if (this.state.saving) {
            return;
        }
        if (!this.resId) {
            this.notification.add(
                _t("Save the arrival before distributing it."),
                { type: "warning" }
            );
            return;
        }
        this.state.saving = true;
        try {
            const data = await this.orm.call(
                "di.stock.arrival",
                "set_distribution_matrix",
                [this.resId, this.state.cells, this.pendingQuantities()]
            );
            this.applyMatrix(data);
            await this.saveRecord();
            await this.reloadRecord();
            this.notification.add(_t("Distribution saved."), { type: "success" });
        } finally {
            this.state.saving = false;
        }
    }

    async discard() {
        this.state.loading = true;
        await this.loadMatrix();
    }
}

registry.category("fields").add("di_distribution_matrix", {
    component: DistributionMatrix,
    supportedTypes: ["one2many"],
});
