const ICONS = "/my_module/static/img/app_icons";

/**
 * The company's own line icons, by app menu. They are shapes (masks) painted
 * in the brand accent colour on a dark tile, so they follow the brand.
 * Apps not listed keep their original icon.
 */
export const APP_GLYPHS = {
    "contacts.menu_contacts": `${ICONS}/contacts.png`,
    "base.menu_management": `${ICONS}/apps.png`,
    "my_barcode.menu_barcode_root": `${ICONS}/barcode.png`,
    "spreadsheet_dashboard.spreadsheet_dashboard_menu_root": `${ICONS}/dashboards.png`,
    "mail.menu_root_discuss": `${ICONS}/discuss.png`,
    "hr.menu_hr_root": `${ICONS}/employees.png`,
    "stock.menu_stock_root": `${ICONS}/inventory.png`,
    "account.menu_finance": `${ICONS}/invoicing.png`,
    "point_of_sale.menu_point_root": `${ICONS}/point_of_sale.png`,
    "base.menu_administration": `${ICONS}/settings.png`,
    "sale.sale_menu_root": `${ICONS}/sales.svg`,
    "di_bonus_points.menu_bonus_point_root": `${ICONS}/bonus_points.svg`,
};

export const HOME_GLYPH = `${ICONS}/home.png`;

/** Inline style painting `url` as a glyph (see .o_my_app_glyph). */
export function glyphStyle(url) {
    return `--o-my-glyph: url('${url}');`;
}

/**
 * The icon of an app menu: the company's glyph when there is one, otherwise
 * normalized the same way the command palette does: an image (base64
 * png/svg or url), or a font icon on a colored background.
 *
 * @returns {{glyph: string} | {webIconData: string} | {webIcon: {iconClass, color, backgroundColor}}}
 */
export function getAppIcon(app) {
    if (APP_GLYPHS[app.xmlid]) {
        return { glyph: APP_GLYPHS[app.xmlid] };
    }
    if (app.webIconData) {
        if (app.webIconData.startsWith("data:image") || app.webIconData.startsWith("/")) {
            return { webIconData: app.webIconData };
        }
        const prefix = app.webIconData.startsWith("P")
            ? "data:image/svg+xml;base64,"
            : "data:image/png;base64,";
        return { webIconData: prefix + app.webIconData.replace(/\s/g, "") };
    }
    const [iconClass, color, backgroundColor] = (app.webIcon || "").split(",");
    if (backgroundColor !== undefined) {
        return { webIcon: { iconClass, color, backgroundColor } };
    }
    return { webIconData: "/web/static/img/default_icon_app.png" };
}

export function normalizeSearch(text) {
    return (text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}
