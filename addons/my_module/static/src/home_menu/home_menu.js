import { Component, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useBus, useService } from "@web/core/utils/hooks";

export const HOME_MENU_ACTION = "my_module.action_home_menu";

// Shared with the navbar patch so it can hide the previous app's menus
// while the home page is displayed.
export const homeMenuState = { isActive: false };

export class HomeMenu extends Component {
    static template = "my_module.HomeMenu";
    static props = ["*"];

    setup() {
        this.menuService = useService("menu");
        useBus(this.env.bus, "MENUS:APP-CHANGED", () => this.render());
        onMounted(() => this.setActive(true));
        onWillUnmount(() => this.setActive(false));
    }

    setActive(isActive) {
        homeMenuState.isActive = isActive;
        this.env.bus.trigger("MENUS:APP-CHANGED");
    }

    get apps() {
        return this.menuService.getApps().map((app) => ({
            id: app.id,
            name: app.name,
            xmlid: app.xmlid,
            href: `/odoo/${app.actionPath || "action-" + app.actionID}`,
            menu: app,
            ...this.getIcon(app),
        }));
    }

    /**
     * Normalizes the app icon the same way the command palette does: either an
     * image (base64 png/svg or url), or a font icon on a colored background.
     */
    getIcon(app) {
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

    onAppClick(ev, app) {
        ev.preventDefault();
        this.menuService.selectMenu(app.menu);
    }
}

registry.category("actions").add("my_module.home_menu", HomeMenu);
