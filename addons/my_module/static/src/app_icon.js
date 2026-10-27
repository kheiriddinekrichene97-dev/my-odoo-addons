/**
 * Normalizes an app menu's icon the same way the command palette does: either
 * an image (base64 png/svg or url), or a font icon on a colored background.
 *
 * @returns {{webIconData: string} | {webIcon: {iconClass, color, backgroundColor}}}
 */
export function getAppIcon(app) {
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
