import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { _t } from "@web/core/l10n/translation";
import { moveDownColumn, useColumnOrder } from "@di_stock_replenish/column_order/column_order";

/**
 * Stock comparison grid: products down, the source warehouses and the chosen
 * destinations across, current stock in every cell.
 *
 * Quantities to send are typed into the destination cells; the stock columns
 * around them are read-only. It reuses the styling of the distribution grid in
 * di_stock_arrival, which this module depends on, so the two read as one tool.
 */
export class ReplenishMatrix extends Component {
    static template = "di_stock_replenish.ReplenishMatrix";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");
        this.state = useState({
            loading: false,
            loaded: false,
            sources: [],
            warehouses: [],
            lines: [],
            labels: {},
            seasons: [],
            selectedSeasons: [],
            seasonFilter: "",
            discountBuckets: [],
            selectedDiscountBucket: "",
            prices: [],
            selectedPrice: "",
            positions: [],
            selectedPositions: [],
            // What a destination has to be under for its cell to read as a
            // gap (gapLimit), and the line the rows themselves were chosen
            // at (rowLimit). They are not the same number: under Below a
            // Quantity every cell on screen is already below the threshold,
            // so the highlight keeps its standing meaning - an empty shelf -
            // and rowLimit is only used to say so in the legend. The server
            // owns both; see _gap_limit and _row_limit.
            gapLimit: 1,
            rowLimit: 1,
            // Rows opened to show the style's other colours and sizes, by
            // product id -> the breakdown rows (null while loading). Not
            // carried across a reload: a new page is a new set of rows.
            expanded: {},
            // Ticked variants, held by id so a selection survives paging: a
            // worker picks the gaps across several pages, then prints once.
            picked: [],
            canPrint: false,
            editable: false,
            // Only the cells the worker actually changed are sent back. The
            // comparison is paged, so a payload can never describe rows that
            // are off-screen - sending the whole grid would delete them.
            edits: {},
            saving: false,
            total: 0,
            offset: 0,
            limit: 0,
            rangeLabel: "",
            message: "",
            rowFilter: "",
            // Prices are hidden until they are asked for: this grid is read
            // down the quantity columns, and two more numbers on every row
            // push them off the side. Kept on the component rather than on
            // the record - it is how one person wants to look at the
            // comparison right now, not part of the document.
            showPrices: false,
        });
        // The order the worker dragged the columns into, remembered in this
        // browser - one order for every comparison. See column_order.js.
        this.columnOrder = useColumnOrder("di_stock_replenish.column_order");
        onWillStart(() => this.load());
    }

    get resId() {
        return this.props.record.resId;
    }

    async load(offset = 0, seasons = null, discount = null, price = null, positions = null) {
        if (!this.resId) {
            return;
        }
        this.state.loading = true;
        try {
            const kwargs = { offset };
            if (seasons !== null) {
                kwargs.seasons = seasons;
            }
            if (discount !== null) {
                kwargs.discount = discount;
            }
            if (price !== null) {
                kwargs.price = price;
            }
            if (positions !== null) {
                kwargs.positions = positions;
            }
            const data = await this.orm.call(
                "di.stock.replenish",
                "get_comparison_matrix",
                [this.resId],
                kwargs
            );
            // range_label is built server-side, where _() reaches this
            // module's catalogue; the template only prints it.
            Object.assign(this.state, data, {
                rangeLabel: data.range_label,
                selectedSeasons: data.selected_seasons || [],
                discountBuckets: data.discount_buckets || [],
                selectedDiscountBucket: data.selected_discount_bucket || "",
                prices: data.prices || [],
                selectedPrice: data.selected_price || "",
                positions: data.positions || [],
                selectedPositions: data.selected_positions || [],
                gapLimit: data.gap_limit,
                rowLimit: data.row_limit,
                canPrint: data.can_print,
                editable: data.editable,
                edits: {},
                expanded: {},
            });
            this.state.loaded = true;
        } finally {
            this.state.loading = false;
        }
    }

    /** A different bucket is a different comparison, so this returns to
     * page one - same reasoning as a season or brand change. */
    async onDiscountChange(ev) {
        await this.load(0, null, ev.target.value);
    }

    /** Same as a discount change: a different price is a different
     * comparison, so back to page one. */
    async onPriceChange(ev) {
        await this.load(0, null, null, ev.target.value);
    }

    /** The discount and price selects' own default option. */
    get allDiscountsLabel() {
        return _t("All");
    }

    /**
     * The row filter's placeholder, resolved here rather than written as a
     * plain placeholder="..." on the input.
     *
     * Odoo's extractor collects placeholder and title into the POT
     * (OWL_TRANSLATED_ATTRS in odoo/tools/translate.py), but the web client
     * mounts its OWL app with translatableAttributes: ["data-tooltip"] and
     * nothing else - so a static placeholder is exported, translated by a
     * translator, and then never looked up at runtime. Anything the grid
     * wants translated in an attribute has to come through _t() and t-att-.
     */
    get rowFilterPlaceholder() {
        return _t("Filter this page...");
    }

    // -- paging -----------------------------------------------------------

    get hasPrevious() {
        return this.state.offset > 0;
    }

    get hasNext() {
        return this.state.offset + this.state.limit < this.state.total;
    }

    get isPaged() {
        return this.state.total > this.state.limit;
    }

    async previousPage() {
        if (this.hasPrevious) {
            await this.load(Math.max(0, this.state.offset - this.state.limit));
        }
    }

    async nextPage() {
        if (this.hasNext) {
            await this.load(this.state.offset + this.state.limit);
        }
    }

    /** Filters live on the record, so they have to be saved before reading. */
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
            this.notification.add(_t("Save the comparison first."), {
                type: "warning",
            });
            return;
        }
        await this.load(0);
    }

    // -- planned quantities -----------------------------------------------

    key(productId, warehouseId) {
        return `${productId}-${warehouseId}`;
    }

    /** The pending edit if there is one, otherwise what the server sent. */
    planned(line, warehouseId) {
        const key = this.key(line.id, warehouseId);
        if (key in this.state.edits) {
            return this.state.edits[key];
        }
        return (line.planned || {})[String(warehouseId)] || 0;
    }

    plannedDisplay(line, warehouseId) {
        const value = this.planned(line, warehouseId);
        return value ? this.format(value) : "";
    }

    /** Everything this row sends out, across every destination on screen. */
    plannedTotal(line) {
        return this.state.warehouses.reduce(
            (sum, warehouse) => sum + this.planned(line, warehouse.id),
            0
        );
    }

    /**
     * The shop's own stock, for the cell tooltip. The cell shows one number -
     * what to send - so the stock it is being compared against has to stay
     * reachable once something has been typed over the placeholder.
     */
    stockTitle(line, warehouseId) {
        return `${this.state.labels.dest_hint || ""}: ${this.format(
            this.cell(line, warehouseId)
        )}`;
    }

    /** More planned than the source can actually give. */
    isOverSource(line) {
        return Math.round((this.plannedTotal(line) - line.source_qty) * 1000) > 0;
    }

    get hasOverSource() {
        return this.state.lines.some((line) => this.isOverSource(line));
    }

    get hasEdits() {
        return Object.keys(this.state.edits).length > 0;
    }

    onQuantityChange(line, warehouseId, ev) {
        const raw = (ev.target.value || "").trim().replace(",", ".");
        const key = this.key(line.id, warehouseId);
        let value = 0;
        if (raw !== "") {
            value = Number.parseFloat(raw);
            if (Number.isNaN(value) || value < 0) {
                ev.target.value = this.plannedDisplay(line, warehouseId);
                this.notification.add(_t("Type a quantity of zero or more."), {
                    type: "warning",
                });
                return;
            }
        }
        this.state.edits[key] = value;
        ev.target.value = value ? this.format(value) : "";

        // Told at once, because the worker is looking at the row: the server
        // refuses it again on save, which is what actually guards the stock.
        if (this.isOverSource(line)) {
            this.notification.add(
                _t("More than the source has free."),
                { type: "warning" }
            );
        }
    }

    /** Enter moves down this shop's column - see moveDownColumn. */
    onQuantityKeydown(ev) {
        moveDownColumn(ev, "input.di_replenish_qty");
    }

    async saveQuantities() {
        if (!this.hasEdits || this.state.saving) {
            return;
        }
        this.state.saving = true;
        try {
            const data = await this.orm.call(
                "di.stock.replenish",
                "set_replenish_cells",
                [this.resId, this.state.edits],
                { context: { replenish_offset: this.state.offset } }
            );
            Object.assign(this.state, data, {
                rangeLabel: data.range_label,
                selectedSeasons: data.selected_seasons || [],
                discountBuckets: data.discount_buckets || [],
                selectedDiscountBucket: data.selected_discount_bucket || "",
                prices: data.prices || [],
                selectedPrice: data.selected_price || "",
                positions: data.positions || [],
                selectedPositions: data.selected_positions || [],
                gapLimit: data.gap_limit,
                rowLimit: data.row_limit,
                canPrint: data.can_print,
                editable: data.editable,
                edits: {},
                expanded: {},
            });
            await this.reloadRecord();
            this.notification.add(_t("Quantities saved."), { type: "success" });
        } finally {
            this.state.saving = false;
        }
    }

    discardQuantities() {
        this.state.edits = {};
    }

    /** Bring the form back in step, so the To Transfer tab and totals agree. */
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

    // -- picking products to print ----------------------------------------

    isPicked(productId) {
        return this.state.picked.includes(productId);
    }

    togglePick(productId) {
        this.state.picked = this.isPicked(productId)
            ? this.state.picked.filter((id) => id !== productId)
            : [...this.state.picked, productId];
    }

    /** Whether every row currently on screen is ticked. */
    get allVisiblePicked() {
        const visible = this.visibleLines;
        return visible.length > 0 && visible.every((line) => this.isPicked(line.id));
    }

    /** Ticks or clears the rows on screen, leaving other pages alone. */
    toggleVisible() {
        const visible = this.visibleLines.map((line) => line.id);
        if (this.allVisiblePicked) {
            this.state.picked = this.state.picked.filter(
                (id) => !visible.includes(id)
            );
        } else {
            const merged = new Set([...this.state.picked, ...visible]);
            this.state.picked = [...merged];
        }
    }

    clearPicked() {
        this.state.picked = [];
    }

    async printTags() {
        if (!this.state.picked.length) {
            return;
        }
        const action = await this.orm.call(
            "di.stock.replenish",
            "action_print_tags",
            [this.resId, this.state.picked]
        );
        await this.action.doAction(action);
    }

    // -- seasons ----------------------------------------------------------

    /** Narrowed by the season filter box, like the destination chips. */
    get pickableSeasons() {
        const needle = this.state.seasonFilter.trim().toLowerCase();
        if (!needle) {
            return this.state.seasons;
        }
        return this.state.seasons.filter((season) =>
            season.name.toLowerCase().includes(needle)
        );
    }

    /**
     * Changing a season changes which products qualify, so the result is
     * reloaded from page one rather than paged into a different comparison.
     */
    async toggleSeason(seasonId) {
        const group = this.seasonGroup(seasonId);
        const allSelected = group.every((id) => this.isSeasonSelected(id));
        const selected = allSelected
            ? this.state.selectedSeasons.filter((id) => !group.includes(id))
            : [...new Set([...this.state.selectedSeasons, ...group])];
        await this.load(0, selected);
    }

    /**
     * What one chip stands for. "A22" is itself. "A" - a season whose whole
     * name is a single letter - is the family: A, A17, A18 ... A26, every
     * season starting with that letter. One click picks or drops the lot,
     * which is how the workers actually ask for it ("tutte le A"); the
     * individual chips still toggle one at a time inside the family.
     */
    seasonGroup(seasonId) {
        const season = this.state.seasons.find((s) => s.id === seasonId);
        const name = (season?.name || "").trim();
        if (!/^[A-Za-z]$/.test(name)) {
            return [seasonId];
        }
        const letter = name.toUpperCase();
        return this.state.seasons
            .filter((s) => (s.name || "").trim().toUpperCase().startsWith(letter))
            .map((s) => s.id);
    }

    /** A letter chip lights up once its whole family is picked. */
    isSeasonSelected(seasonId) {
        const group = this.seasonGroup(seasonId);
        return group.length > 0 && group.every((id) =>
            this.state.selectedSeasons.includes(id)
        );
    }

    async clearSeasons() {
        if (this.state.selectedSeasons.length) {
            await this.load(0, []);
        }
    }


    // -- posizionamenti ------------------------------------------------------

    isPositionSelected(positionId) {
        return this.state.selectedPositions.includes(positionId);
    }

    /** Chips like the seasons, and the same reload from page one. */
    async togglePosition(positionId) {
        const selected = this.isPositionSelected(positionId)
            ? this.state.selectedPositions.filter((id) => id !== positionId)
            : [...this.state.selectedPositions, positionId];
        await this.load(0, null, null, null, selected);
    }

    async clearPositions() {
        if (this.state.selectedPositions.length) {
            await this.load(0, null, null, null, []);
        }
    }

    // -- colours and sizes under a row -------------------------------------

    isExpanded(productId) {
        return productId in this.state.expanded;
    }

    /** The breakdown rows, or null while they are still on their way. */
    expandedRows(productId) {
        return this.state.expanded[productId];
    }

    async toggleExpand(line) {
        if (this.isExpanded(line.id)) {
            delete this.state.expanded[line.id];
            return;
        }
        this.state.expanded[line.id] = null;
        try {
            const data = await this.orm.call(
                "di.stock.replenish",
                "get_variant_breakdown",
                [this.resId, line.id]
            );
            // The worker may have closed it again before the answer came.
            if (line.id in this.state.expanded) {
                this.state.expanded[line.id] = data.rows || [];
            }
        } catch (error) {
            delete this.state.expanded[line.id];
            throw error;
        }
    }

    /** A sub-row's on-hand at one destination. Looked up here rather than
     * in the template: OWL templates cannot reach String() to build the
     * key, and cell() below already does the same for the main rows. */
    variantCell(row, warehouseId) {
        return (row.cells || {})[String(warehouseId)] || 0;
    }

    /** "GIALLO / 3XL", or whichever half the variant has. */
    variantLabel(row) {
        return [row.colour, row.size].filter(Boolean).join(" / ");
    }

    get loadingLabel() {
        return _t("Loading...");
    }

    /**
     * What is on screen, in the words of the mode that put it there. Written
     * as whole literals, never concatenated - the extractor takes the first
     * string argument and would miss the rest of a `+` expression.
     *
     * Under Below a Quantity it states the threshold rather than describing
     * the highlight, because there the threshold is what chose the rows and
     * the highlight is still just "this shelf is empty".
     *
     * Below zero there is nothing to say here: a cell under zero is painted
     * red rather than amber, so the sentence beside this one already covers
     * the only highlight on screen.
     */
    get gapLegend() {
        if (this.state.rowLimit > 1) {
            return _t("Every shop on screen holds fewer than %s of the row it is on.",
                this.format(this.state.rowLimit));
        }
        if (this.state.gapLimit <= 0) {
            return "";
        }
        return _t("A highlighted zero is stock the shop has run out of while the source still holds it.");
    }

    // -- reading ----------------------------------------------------------

    cell(line, warehouseId) {
        return line.cells[String(warehouseId)] || 0;
    }

    /** A zero is the whole point of the report, so it is shown, not blanked. */
    cellDisplay(line, warehouseId) {
        return this.format(this.cell(line, warehouseId));
    }

    /** Under the line the current Show mode draws - see _gap_limit. */
    isMissing(line, warehouseId) {
        return this.cell(line, warehouseId) < this.state.gapLimit;
    }

    /**
     * Below zero, not merely empty: the shop has sold stock the system never
     * knew it had. It is the gap the Only Negative mode selects on, and it
     * reads differently from a plain zero, so it is coloured differently.
     */
    isNegative(line, warehouseId) {
        return this.cell(line, warehouseId) < 0;
    }

    /** The gap class for one cell - the sharper of the two wins. */
    cellClass(line, warehouseId) {
        if (this.isNegative(line, warehouseId)) {
            return "di_replenish_negative";
        }
        return this.isMissing(line, warehouseId) ? "di_replenish_gap" : "";
    }

    /** Same two-step for a colour/size sub-row, which carries no input. */
    variantCellClass(row, warehouseId) {
        const quantity = this.variantCell(row, warehouseId);
        if (quantity < 0) {
            return "di_replenish_negative";
        }
        return quantity < this.state.gapLimit ? "di_replenish_gap" : "";
    }

    /** How many of the chosen destinations have none of this product. */
    missingCount(line) {
        return this.state.warehouses.filter((warehouse) =>
            this.isMissing(line, warehouse.id)
        ).length;
    }

    togglePrices() {
        this.state.showPrices = !this.state.showPrices;
    }

    /** Blank rather than 0.00 where no promo rule exists for the product. */
    priceDisplay(value) {
        return value ? value.toFixed(2) : "";
    }

    format(value) {
        return Number.isInteger(value)
            ? String(value)
            : String(Math.round(value * 1000) / 1000);
    }

    get visibleLines() {
        const needle = this.state.rowFilter.trim().toLowerCase();
        if (!needle) {
            return this.state.lines;
        }
        // Barcode, brand and supplier are on screen, so they are searchable:
        // a worker scanning a barcode into the box expects the row to come up.
        return this.state.lines.filter((line) =>
            [
                line.name,
                line.code,
                line.barcode,
                line.brand,
                line.supplier,
                line.season,
                line.position,
                line.category,
            ].some((field) => (field || "").toLowerCase().includes(needle))
        );
    }

    /**
     * Digits and a slash, deliberately - two numbers read the same in either
     * language, and the count belongs beside the Refresh button where a
     * sentence would not fit. (The older note here said _t() could not be
     * trusted; that was i18n/it.po missing its "#. odoo-javascript"
     * comments, which is what Odoo filters web translations on. Fixed.)
     */
    get gapSummary() {
        return `${this.gapCount} / ${this.state.lines.length}`;
    }

    /** Full warehouse name plus the measure, for the header tooltip. */
    sourceTitle(source) {
        return `${source.name || ""} - ${this.state.labels.source_hint || ""}`;
    }

    /** What one source has free of a row - or of a colour/size sub-row,
     * which carries the same source_cells. */
    sourceCell(row, sourceId) {
        return (row.source_cells || {})[String(sourceId)] || 0;
    }

    // -- columns ----------------------------------------------------------

    /**
     * Every column after the frozen product one, in the default order:
     * the product detail, the prices, the sources, then the destinations.
     * The product column stays put - it is the sticky one, and the row
     * would lose its name if it scrolled away.
     */
    get defaultColumns() {
        const labels = this.state.labels;
        const info = (key, extra = {}) => ({ key, type: "info", label: labels[key], ...extra });
        return [
            info("barcode", { cellClass: "di_replenish_barcode" }),
            info("brand"),
            info("season"),
            info("position"),
            info("category", { titled: true }),
            info("supplier"),
            { key: "list_price", type: "price", label: labels.list_price },
            { key: "promo", type: "price", label: labels.promo },
            ...this.state.sources.map((source) => ({
                key: `src-${source.id}`,
                type: "source",
                id: source.id,
                label: source.code,
                title: this.sourceTitle(source),
            })),
            ...this.state.warehouses.map((warehouse) => ({
                key: `dst-${warehouse.id}`,
                type: "dest",
                id: warehouse.id,
                label: warehouse.name,
                title: warehouse.code,
            })),
        ];
    }

    /** The full order, hidden price columns included, so ticking the
     * prices back on puts them where they were dragged to. */
    get columnKeys() {
        return this.columnOrder.keys(this.defaultKeys);
    }

    get defaultKeys() {
        return this.defaultColumns.map((column) => column.key);
    }

    /** The columns on screen, in the order the worker arranged them. Read
     * once per render (t-set in the template), not once per row. */
    get columns() {
        const byKey = Object.fromEntries(
            this.defaultColumns.map((column) => [column.key, column])
        );
        return this.columnKeys
            .map((key) => byKey[key])
            .filter((column) => column.type !== "price" || this.state.showPrices);
    }

    get resetColumnsTitle() {
        return _t("Reset column order");
    }

    infoValue(line, column) {
        if (column.type === "price") {
            return this.priceDisplay(line[column.key]);
        }
        return line[column.key];
    }

    get gapCount() {
        return this.state.lines.filter((line) => this.missingCount(line) > 0)
            .length;
    }
}

registry.category("fields").add("di_replenish_matrix", {
    component: ReplenishMatrix,
    supportedTypes: ["char"],
});
