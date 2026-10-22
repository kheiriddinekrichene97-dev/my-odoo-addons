import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";

/**
 * "Esistenza per colori articolo" - the shop's previous management
 * software's stock screen, embedded on the product form instead of standing
 * alone: every colour down the side, every size across the top, and - under
 * each colour - every warehouse, with a zero row for the shops that carry
 * none of it.
 *
 * A field widget bound to di_color_size_matrix, which stores nothing - same
 * anchor pattern as this codebase's other grids (di_stock_arrival,
 * di_stock_replenish). Unlike a standalone screen this needs no product
 * picker: the template is already the record being viewed, so the grid is
 * keyed on props.record.resId from the start.
 *
 * UI chrome text goes through _t() and is looked up by its literal English
 * source string, then supplied by i18n/it.po - not hardcoded Italian. Data
 * (colour names, size names, warehouse names) is never translated; it is
 * whatever this database's own records are called, in whichever language
 * they were entered.
 */
export class ColorSizeMatrix extends Component {
    static template = "di_stock_locations.ColorSizeMatrix";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            loading: true,
            collapsed: false,
            onlyInStock: false,
            product: null,
            warehouses: [],
            warehouseId: null,
            colours: [],
            sizes: [],
            totals: {},
            breakdown: {},
            sizeTotals: {},
            colourLabel: "",
            sizeLabel: "",
        });
        onWillStart(() => this.load());
    }

    get resId() {
        return this.props.record.resId;
    }

    async onWarehouseChange(ev) {
        this.state.warehouseId = ev.target.value ? Number(ev.target.value) : null;
        await this.load();
    }

    /** The old screen's own title bar carried a collapse arrow; the grid
     * gets long once every warehouse is listed under every colour, so it is
     * worth keeping. */
    toggleCollapsed() {
        this.state.collapsed = !this.state.collapsed;
    }

    /** Hide every warehouse row whose Totali is zero. The full grid answers
     * "which shops carry this colour at all" - zero rows included, by
     * design - but "where are the pieces right now" is the more frequent
     * question, and 20 dimmed zero rows per colour bury the two that
     * matter. Pure client-side filter over data already loaded: the colour
     * total rows and the footer stay, they are totals either way. */
    toggleOnlyInStock() {
        this.state.onlyInStock = !this.state.onlyInStock;
    }

    /** The warehouse rows to draw under one colour, after the filter. */
    visibleRows(colour) {
        const rows = this.state.breakdown[colour] || [];
        return this.state.onlyInStock ? rows.filter((row) => !this.isEmptyRow(row)) : rows;
    }

    async load() {
        if (!this.resId) {
            this.state.loading = false;
            return;
        }
        this.state.loading = true;
        try {
            const [warehouses, data] = await Promise.all([
                this.state.warehouses.length
                    ? Promise.resolve(this.state.warehouses)
                    : this.orm.call(
                          "product.template", "di_matrix_available_warehouses", []
                      ),
                this.orm.call(
                    "product.template", "get_color_size_matrix", [this.resId],
                    { warehouse_id: this.state.warehouseId }
                ),
            ]);
            this.state.warehouses = warehouses;
            this.state.product = data.product;
            this.state.colours = data.colours;
            this.state.sizes = data.sizes;
            this.state.totals = data.totals;
            this.state.breakdown = data.breakdown;
            this.state.sizeTotals = data.size_totals;
            this.state.colourLabel = data.labels.colour;
            this.state.sizeLabel = data.labels.size;
        } finally {
            this.state.loading = false;
        }
    }

    // -- chrome text (translated) ------------------------------------------

    get titlePrefix() {
        return _t("Stock by colour:");
    }

    get warehouseFilterLabel() {
        return _t("Warehouse");
    }

    get allWarehousesLabel() {
        return _t("All");
    }

    get totalLabel() {
        return _t("Total");
    }

    get loadingLabel() {
        return _t("Loading...");
    }

    get emptyLabel() {
        return _t("This product has no Colour/Size variants to show.");
    }

    get collapseLabel() {
        return this.state.collapsed ? _t("Expand") : _t("Collapse");
    }

    get onlyInStockLabel() {
        return _t("In stock only");
    }

    // -- reading (data - never translated) ----------------------------------

    // The colour row is a total, and a total says "zero" plainly - that is
    // what the old screen's own bold "BIANCO 4 0 0 17 8 29" row did too.
    colourCell(colour, size) {
        return this.format((this.state.totals[colour] || {})[size] || 0);
    }

    colourTotal(colour) {
        return this.format((this.state.totals[colour] || {}).__total__ || 0);
    }

    // A location row is not a total - it is one shop's own shelf - and the
    // old screen left an empty cell there rather than printing a zero,
    // reserving "0" for the row's own Totali column. Reproduced here: a
    // filled cell is stock actually on that shelf, a blank one is not.
    rowCell(row, size) {
        const value = row.cells[size] || 0;
        return value ? this.format(value) : "";
    }

    sizeColumnTotal(size) {
        return this.format(this.state.sizeTotals[size] || 0);
    }

    get grandTotal() {
        return this.format(this.state.sizeTotals.__total__ || 0);
    }

    /** A row where every size is empty still prints its own zero Totali - a
     * shop carrying none of a colour is itself the answer - but dimmed, so
     * an all-zero shelf does not compete for attention with one that
     * actually holds stock. */
    isEmptyRow(row) {
        return row.total === 0;
    }

    format(value) {
        return Number.isInteger(value)
            ? String(value)
            : String(Math.round(value * 100) / 100);
    }
}

registry.category("fields").add("di_color_size_matrix", {
    component: ColorSizeMatrix,
    supportedTypes: ["char"],
});
