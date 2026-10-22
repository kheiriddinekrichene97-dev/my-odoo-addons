import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";

/**
 * Pick who is selling.
 *
 * Two jobs in one popup, kept apart by an explicit toggle rather than by a
 * hidden gesture: normally the choice applies to everything scanned FROM
 * NOW ON, which is how a shop floor actually works - an assistant serves a
 * customer for a stretch, then hands over. Ticking "only this line"
 * corrects a single product instead, without disturbing the running
 * attribution.
 *
 * The toggle is only offered when a line is actually selected, so the
 * common case stays a single tap.
 */
export class SalespersonPopup extends Component {
    static template = "di_pos_salesperson.SalespersonPopup";
    static components = { Dialog };
    static props = {
        title: { type: String, optional: true },
        list: { type: Array, optional: true },
        lineName: { type: [String, Boolean], optional: true },
        getPayload: Function,
        close: Function,
    };
    static defaultProps = {
        title: _t("Who is selling?"),
        list: [],
        lineName: false,
    };

    setup() {
        this.state = useState({ onlyThisLine: false });
    }

    selectItem(item) {
        this.props.getPayload({
            employee: item.item,
            onlyThisLine: Boolean(this.props.lineName) && this.state.onlyThisLine,
        });
        this.props.close();
    }
}
