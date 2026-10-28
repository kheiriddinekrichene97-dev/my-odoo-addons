import { Component, useExternalListener, useRef, useState } from "@odoo/owl";
import { useBus, useService } from "@web/core/utils/hooks";
import { getAppIcon, glyphStyle, HOME_GLYPH, normalizeSearch } from "../app_icon";
import { HOME_MENU_ACTION } from "../home_menu/home_menu";

/**
 * A ☰ button in the top bar opening a left drawer with Home, a search box and
 * every app; each app expands to its menus. Picking an entry opens it and
 * closes the drawer, as do Esc and a click outside it.
 */
export class SideMenu extends Component {
    static template = "my_module.SideMenu";
    static props = {};

    setup() {
        this.menuService = useService("menu");
        this.action = useService("action");
        this.glyphStyle = glyphStyle;
        this.homeGlyph = HOME_GLYPH;
        this.searchRef = useRef("search");
        this.state = useState({ open: false, query: "", expanded: {} });
        useBus(this.env.bus, "MENUS:APP-CHANGED", () => this.render());
        useExternalListener(window, "keydown", (ev) => {
            if (ev.key === "Escape" && this.state.open) {
                this.close();
            }
        });
    }

    toggle() {
        if (this.state.open) {
            this.close();
            return;
        }
        // Open on the app the user is in.
        const current = this.menuService.getCurrentApp();
        this.state.expanded = current ? { [current.id]: true } : {};
        this.state.query = "";
        this.state.open = true;
        setTimeout(() => this.searchRef.el?.focus(), 250);
    }

    close() {
        this.state.open = false;
    }

    get apps() {
        const query = normalizeSearch(this.state.query.trim());
        return this.menuService
            .getApps()
            .filter((app) => !query || normalizeSearch(app.name).includes(query))
            .map((app) => ({ app, ...getAppIcon(app) }));
    }

    children(menu) {
        return this.menuService.getMenuAsTree(menu.id).childrenTree || [];
    }

    href(menu) {
        return menu.actionID ? `/odoo/${menu.actionPath || "action-" + menu.actionID}` : "#";
    }

    isCurrentApp(app) {
        return this.menuService.getCurrentApp()?.id === app.id;
    }

    isExpanded(menu) {
        return Boolean(this.state.expanded[menu.id]);
    }

    toggleExpand(menu) {
        this.state.expanded[menu.id] = !this.state.expanded[menu.id];
    }

    onMenuClick(menu) {
        if (this.children(menu).length) {
            this.toggleExpand(menu);
        } else {
            this.select(menu);
        }
    }

    onSearchKeydown(ev) {
        const apps = this.apps;
        if (ev.key === "Enter" && apps.length) {
            ev.preventDefault();
            this.select(apps[0].app);
        }
    }

    select(menu) {
        this.close();
        this.menuService.selectMenu(menu);
    }

    goHome() {
        this.close();
        this.action.doAction(HOME_MENU_ACTION, { clearBreadcrumbs: true });
    }
}
